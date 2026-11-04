"""Transactional snapshots, replacement plans and undo for production statistics.

Only normalized sums are replaced. Sensor states, live counters and recovery
amounts are never rewritten. All operations run on Recorder's serialized queue.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from math import isfinite
from time import time

from homeassistant.components.recorder.db_schema import (
    StateAttributes,
    States,
    StatesMeta,
    Statistics,
    StatisticsMeta,
    StatisticsShortTerm,
)
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.helpers.recorder import session_scope

from .const import DOMAIN
from .manual_recovery import RecoveryError
from .recovery import number

TABLES = {"hour": Statistics, "short": StatisticsShortTerm}
_MARKER_TIME = datetime(2000, 1, 1, tzinfo=UTC).timestamp()


def marker_id(entry_id):
    return f"{DOMAIN}:repair_audit_{sha256(entry_id.encode()).hexdigest()[:32]}"


def recovery_marker(entry_id, panel):
    digest = sha256(f"{entry_id}:{panel}".encode()).hexdigest()[:32]
    return f"{DOMAIN}:recovery_audit_{digest}"


def _metadata(session, instance, statistic_id):
    return instance.statistics_meta_manager.get_many(session, statistic_ids={statistic_id}).get(statistic_id)


def _marker(session, instance, statistic_id, stamp):
    meta = _metadata(session, instance, statistic_id)
    row = session.query(Statistics).filter_by(metadata_id=meta[0], start_ts=stamp).first() if meta else None
    return row.state if row is not None else None


def _set_marker(session, instance, statistic_id, stamp, value):
    meta = _metadata(session, instance, statistic_id)
    if meta is None:
        data = {
            "statistic_id": statistic_id, "source": DOMAIN,
            "name": "Statistics repair audit (not an energy source)",
            "unit_of_measurement": None, "mean_type": StatisticMeanType.NONE, "has_sum": False,
        }
        if hasattr(StatisticsMeta, "unit_class"):
            data["unit_class"] = None
        _, metadata_id = instance.statistics_meta_manager.update_or_add(session, data, {})
    else:
        metadata_id = meta[0]
    row = session.query(Statistics).filter_by(metadata_id=metadata_id, start_ts=stamp).first()
    if value is None:
        if row is not None:
            session.delete(row)
    elif row is not None:
        row.state = value
    else:
        session.add(Statistics.from_stats(metadata_id, {
            "start": datetime.fromtimestamp(stamp, UTC), "state": value,
        }, time()))


def repair_status(instance, entry_id):
    with session_scope(session=instance.get_session(), read_only=True) as session:
        return _marker(session, instance, marker_id(entry_id), _MARKER_TIME)


def _point(row):
    return {"start": row.start_ts, "sum": row.sum, "state": row.state} if row else None


def _series(session, table, metadata_id, start, end, all_rows=False):
    query = session.query(table).filter(table.metadata_id == metadata_id)
    anchor = query.filter(table.start_ts < start, table.sum.is_not(None)).order_by(table.start_ts.desc()).first()
    rows = query.order_by(table.start_ts).all() if all_rows else query.filter(
        table.start_ts >= start - 3600,
    ).order_by(table.start_ts).all()
    baseline = anchor.sum if anchor else 0.0
    day_rows = [row for row in rows if start <= row.start_ts < end and row.sum is not None]
    last = day_rows[-1].sum if day_rows else baseline
    previous = baseline
    negative = []
    for row in rows:
        if row.start_ts < start or row.sum is None:
            continue
        if row.sum < previous - 1e-7:
            negative.append(row.start_ts)
        previous = row.sum
        if row.start_ts >= end:
            break  # Include the join to the next day, not unrelated later days.
    if not isfinite(baseline) or not isfinite(last):
        raise RecoveryError("repair_invalid_statistics")
    return {
        "anchor": _point(anchor), "rows": [_point(row) for row in rows],
        "baseline": baseline, "end_sum": last, "total": last - baseline,
        "negative": negative, "has_day": bool(day_rows),
    }


def _pending_increase(session, entity, metadata_id, record):
    """Describe a recorded recovery without moving any historical sum."""
    query = session.query(States.last_updated_ts, States.state, StateAttributes.shared_attrs).join(
        StatesMeta, States.metadata_id == StatesMeta.metadata_id,
    ).join(StateAttributes, States.attributes_id == StateAttributes.attributes_id).filter(StatesMeta.entity_id == entity)
    first = next((row for row in query.filter(States.last_updated_ts >= record["created"]).order_by(
        States.last_updated_ts,
    ) if number(row[1]) is not None and json.loads(row[2] or "{}").get(
        "recovered_energy_wh", 0,
    ) >= record["target"] - 1e-6), None)
    if first is None:
        return None
    spike = int(first[0] // 300) * 300
    latest = session.query(StatisticsShortTerm.start_ts).filter_by(metadata_id=metadata_id).order_by(
        StatisticsShortTerm.start_ts.desc(),
    ).first()
    if latest is None or latest[0] < spike:
        return None
    prior = session.query(StatisticsShortTerm).filter(
        StatisticsShortTerm.metadata_id == metadata_id, StatisticsShortTerm.start_ts < spike,
        StatisticsShortTerm.state.is_not(None),
    ).first()
    counted = True
    if prior is None:
        initial = session.query(StatisticsShortTerm.start_ts).filter(
            StatisticsShortTerm.metadata_id == metadata_id, StatisticsShortTerm.state.is_not(None),
        ).order_by(StatisticsShortTerm.start_ts).first()
        if initial is None:
            return None
        baseline = query.filter(States.last_updated_ts <= initial[0]).order_by(States.last_updated_ts.desc()).first()
        if baseline is None or number(baseline[1]) is None:
            baseline = next((row for row in query.filter(
                States.last_updated_ts >= initial[0], States.last_updated_ts < initial[0] + 300,
            ).order_by(States.last_updated_ts) if number(row[1]) is not None), None)
        if baseline is None:
            return None
        counted = json.loads(baseline[2] or "{}").get("recovered_energy_wh", 0) < record["target"] - 1e-6
    return {"spike": spike, "counted": counted}


def snapshot(instance, entry_id, entities, bounds, pending):
    """Capture rows and audit states together on the Recorder queue."""
    start, end = bounds
    if start % 3600 or end % 3600:
        raise RecoveryError("repair_timezone")
    result = {"panels": {}, "bounds": list(bounds), "marker": None}
    with session_scope(session=instance.get_session(), read_only=True) as session:
        result["marker"] = _marker(session, instance, marker_id(entry_id), _MARKER_TIME)
        for panel, entity in entities.items():
            meta = _metadata(session, instance, entity)
            if meta is None:
                continue
            metadata_id, details = meta
            factor = {"Wh": 1.0, "kWh": 1000.0, "MWh": 1000000.0}.get(details["unit_of_measurement"])
            if factor is None:
                raise RecoveryError("repair_unit")
            data = {
                "entity": entity, "metadata_id": metadata_id, "factor": factor,
                "unit": details["unit_of_measurement"], "pending": [],
                "hour": _series(session, Statistics, metadata_id, start, end),
                "short": _series(session, StatisticsShortTerm, metadata_id, start, end, all_rows=True),
                "retention": time() - getattr(instance, "keep_days", 10) * 86400,
            }
            for record in pending:
                if record["panel"] != panel:
                    continue
                stamp = datetime.fromisoformat(record["day"]).replace(tzinfo=UTC).timestamp()
                audit_id = recovery_marker(entry_id, panel)
                audit = _marker(session, instance, audit_id, stamp)
                applied = audit is not None and audit >= record["target"] - 1e-6
                data["pending"].append({
                    "record": record, "audit_id": audit_id, "stamp": stamp, "audit": audit,
                    "applied": applied,
                    "increase": None if applied else _pending_increase(session, entity, metadata_id, record),
                })
            result["panels"][panel] = data
    return result


def _removal(data, stamp, duration):
    return sum(sum(item["record"]["intervals"].values()) for item in data["pending"]
               if not item["applied"] and item["increase"]["counted"]
               and stamp >= item["increase"]["spike"] // duration * duration)


def _alignment(data):
    """Match five-minute sums to the same completed hourly endpoints."""
    short = {row["start"]: row["sum"] for row in data["short"]["rows"] if row["sum"] is not None}
    for row in reversed(data["hour"]["rows"]):
        stamp = row["start"] + 3300
        if row["sum"] is not None and stamp in short:
            return row["sum"] - short[stamp]
    if data["short"]["anchor"] is not None:
        return data["hour"]["baseline"] - data["short"]["baseline"]
    if not short:
        return 0.0
    raise RecoveryError("repair_no_alignment")


def replacement_plan(analysis, selected):
    """Replace selected days; preserve nonselected panels and subsequent increments."""
    snap = analysis["snapshot"]
    if not selected or len(set(selected)) != len(selected) or set(selected) - set(snap["panels"]):
        raise RecoveryError("invalid_panels")
    start, end = snap["bounds"]
    aggregate = analysis["aggregate"]
    total = sum(aggregate.values())
    kept = 0.0
    for panel, data in snap["panels"].items():
        if panel in selected:
            continue
        if data["hour"]["negative"] or data["hour"]["total"] < 0 or any(
            not item["applied"] for item in data["pending"]
        ):
            raise RecoveryError("repair_select_anomalous")
        kept += data["hour"]["total"] * data["factor"]
    minima = {panel: analysis["prior"].get(panel, {}).get("total", 0) for panel in selected}
    remaining = total - kept - sum(minima.values())
    if remaining < -0.1:
        raise RecoveryError("energy_exceeds_aggregate")
    weights = {panel: number(analysis["panels"][panel].get("power")) for panel in selected}
    if not all(weights.values()):
        weights = dict.fromkeys(selected, 1.0)
    targets = {panel: minima[panel] + max(0, remaining) * weights[panel] / sum(weights.values()) for panel in selected}
    operations = []
    tails = []
    audits = {}
    acknowledgements = []
    for panel in selected:
        data = snap["panels"][panel]
        factor = data["factor"]
        for item in data["pending"]:
            if item["record"]["day"] != analysis["day"]:
                if not item["applied"]:
                    raise RecoveryError("repair_pending")
                continue
            if not item["applied"] and (item["record"]["day"] != analysis["day"] or item["increase"] is None):
                raise RecoveryError("repair_pending")
            acknowledgements.append(item["record"])
            key = item["audit_id"]
            audits[key] = {
                "statistic_id": key, "stamp": item["stamp"], "before": item["audit"],
                "after": max(item["record"]["target"], item["audit"] or 0, audits.get(key, {}).get("after", 0)),
            }
        desired = [(int(stamp), value * targets[panel] / total / factor if total else 0)
                   for stamp, value in aggregate.items()]
        baseline = data["hour"]["baseline"]
        new_end = baseline + targets[panel] / factor
        hourly_offset = new_end - data["hour"]["end_sum"]
        alignment = _alignment(data)
        for name, duration in (("hour", 3600), ("short", 300)):
            series = data[name]
            existing = {row["start"]: row for row in series["rows"]}
            result = {stamp: row["sum"] for stamp, row in existing.items()}
            delta = hourly_offset + (alignment if name == "short" else 0)
            if name == "short" and series["anchor"] is not None:
                before_shift = baseline - series["baseline"]
                for stamp, row in existing.items():
                    if stamp < start and row["sum"] is not None:
                        result[stamp] = row["sum"] + before_shift
            first = start - duration
            if name == "short":
                first = max(first, int(data["retention"] // duration) * duration)
            for stamp in range(first, end, duration):
                energy = sum(value * min(3, max(0, (stamp + duration - quarter) // 300)) / 3
                             for quarter, value in desired)
                result[stamp] = baseline + energy
            for stamp, row in existing.items():
                if stamp >= end and row["sum"] is not None:
                    result[stamp] = row["sum"] + delta - _removal(data, stamp, duration) / factor
            # Verify the whole reconstructed day and both joins. Existing later
            # anomalies retain their increments and require their own dated repair.
            points = sorted((stamp, value) for stamp, value in result.items() if value is not None)
            last = None
            for stamp, value in points:
                if last is not None and start - duration <= stamp <= end and value < last - 1e-7:
                    raise RecoveryError("repair_boundary")
                if not isfinite(value):
                    raise RecoveryError("repair_invalid_statistics")
                last = value
            for stamp, value in result.items():
                before = existing.get(stamp)
                if before is None or before["sum"] is None or value is None or abs(before["sum"] - value) > 1e-7:
                    operations.append({
                        "table": name, "metadata_id": data["metadata_id"], "start": stamp,
                        "before": before["sum"] if before else None, "existed": before is not None,
                        "after": value,
                    })
            latest = max(existing, default=end - duration)
            tails.append({
                "table": name, "metadata_id": data["metadata_id"], "latest": latest,
                "offset": delta - _removal(data, float("inf"), duration) / factor,
            })
    return {
        **analysis, "selected": list(selected), "targets": targets, "operations": operations,
        "tails": tails, "audits": list(audits.values()), "acknowledgements": acknowledgements,
    }


def apply_plan(instance, entry_id, plan, token):
    """Compare the preview, write sums and acknowledge pending recoveries atomically."""
    with session_scope(session=instance.get_session()) as session:
        if _marker(session, instance, marker_id(entry_id), _MARKER_TIME) == token:
            return
        # Read-only snapshot comparison runs on the same queue before this job;
        # compare again inside this transaction to reject stale previews.
        # The proposed total also depends on every unselected panel's current
        # daily energy; reject a preview if any of those statistics changed.
        for panel in plan["snapshot"]["panels"]:
            data = plan["snapshot"]["panels"][panel]
            meta = _metadata(session, instance, data["entity"])
            if meta is None or meta[0] != data["metadata_id"] or meta[1]["unit_of_measurement"] != data["unit"]:
                raise RecoveryError("preview_stale")
            for name, table in TABLES.items():
                actual = _series(session, table, data["metadata_id"], *plan["snapshot"]["bounds"], all_rows=name == "short")
                if actual != data[name]:
                    raise RecoveryError("preview_stale")
        if _marker(session, instance, marker_id(entry_id), _MARKER_TIME) != plan["snapshot"]["marker"]:
            raise RecoveryError("preview_stale")
        for audit in plan["audits"]:
            if _marker(session, instance, audit["statistic_id"], audit["stamp"]) != audit["before"]:
                raise RecoveryError("preview_stale")
        for operation in plan["operations"]:
            table = TABLES[operation["table"]]
            row = session.query(table).filter_by(metadata_id=operation["metadata_id"], start_ts=operation["start"]).first()
            if row is not None:
                row.sum = operation["after"]
            else:
                session.add(table.from_stats(operation["metadata_id"], {
                    "start": datetime.fromtimestamp(operation["start"], UTC), "sum": operation["after"],
                }, time()))
        for audit in plan["audits"]:
            _set_marker(session, instance, audit["statistic_id"], audit["stamp"], audit["after"])
        _set_marker(session, instance, marker_id(entry_id), _MARKER_TIME, token)


def undo_plan(instance, entry_id, backup):
    """Restore saved sums; later appended rows lose the repair's final offset."""
    plan = backup["plan"]
    with session_scope(session=instance.get_session()) as session:
        current = _marker(session, instance, marker_id(entry_id), _MARKER_TIME)
        if current == plan["snapshot"]["marker"]:
            return
        if current != backup["token"]:
            raise RecoveryError("repair_undo_conflict")
        for data in (plan["snapshot"]["panels"][panel] for panel in plan["selected"]):
            meta = _metadata(session, instance, data["entity"])
            if meta is None or meta[0] != data["metadata_id"] or meta[1]["unit_of_measurement"] != data["unit"]:
                raise RecoveryError("repair_undo_conflict")
        for operation in plan["operations"]:
            table = TABLES[operation["table"]]
            row = session.query(table).filter_by(metadata_id=operation["metadata_id"], start_ts=operation["start"]).first()
            if row is None and operation["table"] == "short" and operation["start"] < time() - getattr(instance, "keep_days", 10) * 86400:
                continue  # Do not resurrect data already purged by Recorder.
            if row is None or row.sum is None or abs(row.sum - operation["after"]) > 1e-7:
                raise RecoveryError("repair_undo_conflict")
            if operation["existed"]:
                row.sum = operation["before"]
            else:
                session.delete(row)
        for tail in plan["tails"]:
            table = TABLES[tail["table"]]
            for row in session.query(table).filter(
                table.metadata_id == tail["metadata_id"], table.start_ts > tail["latest"], table.sum.is_not(None),
            ):
                row.sum -= tail["offset"]
        for audit in plan["audits"]:
            if _marker(session, instance, audit["statistic_id"], audit["stamp"]) != audit["after"]:
                raise RecoveryError("repair_undo_conflict")
            _set_marker(session, instance, audit["statistic_id"], audit["stamp"], audit["before"])
        _set_marker(session, instance, marker_id(entry_id), _MARKER_TIME, plan["snapshot"]["marker"])
