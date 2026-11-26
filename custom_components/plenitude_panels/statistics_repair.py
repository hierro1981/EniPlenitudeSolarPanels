"""Hourly replacement previews and public-API-only undo plans."""

from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite

from .manual_recovery import RecoveryError
from .recovery import number
from .recovery_history import read_audits
from .recovery_statistics import counted_spike, import_step, metadata, read_series


def _series(rows, start, end):
    anchors = [row for row in rows if row["start"] < start and row.get("sum") is not None]
    anchor = anchors[-1] if anchors else None
    baseline = anchor["sum"] if anchor else None
    # Only the latest earlier reference influences this preview. Do not retain
    # the entity's entire pre-day lifetime in the durable backup.
    rows = ([anchor] if anchor else []) + [r for r in rows if r["start"] >= start]
    day = [row for row in rows if start <= row["start"] < end and row.get("sum") is not None]
    last = day[-1]["sum"] if day else baseline
    previous, negative = baseline, []
    for row in rows:
        if row["start"] < start or row.get("sum") is None:
            continue
        if previous is not None and row["sum"] < previous - 1e-7:
            negative.append(row["start"])
        previous = row["sum"]
        if row["start"] >= end:
            break
    return {"anchor": anchor, "rows": rows, "baseline": baseline, "end_sum": last,
            "total": last - baseline if last is not None and baseline is not None else 0,
            "negative": negative, "has_day": bool(day)}


def snapshot(instance, entry_id, entities, bounds, pending):
    """Public API reads: no private schema, metadata manager or DB markers."""
    start, end = bounds
    if start % 3600 or end % 3600:
        raise RecoveryError("repair_timezone")
    result = {"panels": {}, "bounds": list(bounds)}
    audits = read_audits(instance, entry_id, entities, entities.values())
    for panel, entity in entities.items():
        meta = metadata(instance, entity)
        if not meta:
            continue
        factor = {"Wh": 1, "kWh": 1000, "MWh": 1000000}.get(meta["unit_of_measurement"])
        if factor is None or meta.get("source") != "recorder":
            raise RecoveryError("repair_unit")
        data = {"entity": entity, "metadata": meta, "factor": factor,
                "unit": meta["unit_of_measurement"], "pending": [],
                "hour": _series(read_series(instance, entity, 0), start, end),
                "short": _series(read_series(instance, entity, end, period="5minute"), end, end)}
        for record in pending:
            if record["panel"] != panel:
                continue
            stamp = datetime.fromisoformat(record["day"]).replace(tzinfo=UTC).timestamp()
            applied = any(t == stamp and target >= record["target"] - 1e-6 for t, target in audits.get(panel, []))
            data["pending"].append({
                "record": record, "applied": applied,
                "spike": None if applied else counted_spike(instance, entity, record, meta),
            })
        result["panels"][panel] = data
    return result


def _delta_step(data, start, delta, previous_steps):
    """Capture stable hour AND five-minute witnesses; appended rows may advance."""
    witnesses = []
    for name, period in (("hour", "hour"), ("short", "5minute")):
        cutoff = start // 3600 * 3600 if name == "hour" else start
        rows = [r for r in data[name]["rows"] if r["start"] >= cutoff and r.get("sum") is not None]
        if not rows:
            raise RecoveryError("repair_pending")
        row = rows[-1]
        before = row["sum"] + sum(s["delta"] for s in previous_steps
                                  if s["kind"] == "delta" and s["entity"] == data["entity"]
                                  and row["start"] >= (s["start"] // 3600 * 3600 if name == "hour" else s["start"]))
        witnesses.append({"period": period, "start": row["start"], "before": before, "after": before + delta})
    return {"kind": "delta", "entity": data["entity"], "start": start, "delta": delta,
            "unit": data["unit"], "witnesses": witnesses, "phase": "prepared"}


def replacement_plan(analysis, selected):
    snap = analysis["snapshot"]
    if not selected or len(set(selected)) != len(selected) or set(selected) - set(snap["panels"]):
        raise RecoveryError("invalid_panels")
    start, end = snap["bounds"]
    total = sum(analysis["aggregate"].values())
    kept = 0
    for panel, data in snap["panels"].items():
        if panel not in selected:
            if (data["hour"]["baseline"] is None or not data["hour"]["has_day"]
                or data["hour"]["negative"] or data["hour"]["total"] < 0
                or any(not item["applied"] for item in data["pending"])):
                raise RecoveryError("repair_select_anomalous")
            kept += data["hour"]["total"] * data["factor"]
    minima = {p: analysis["prior"].get(p, {}).get("total", 0) for p in selected}
    remaining = total - kept - sum(minima.values())
    if remaining < -.1:
        raise RecoveryError("energy_exceeds_aggregate")
    weights = {p: number(analysis["panels"][p].get("power")) for p in selected}
    if not all(weights.values()):
        weights = dict.fromkeys(selected, 1.)
    targets = {p: minima[p] + max(0, remaining) * weights[p] / sum(weights.values()) for p in selected}
    steps, operations, acknowledgements = [], [], []
    for panel in selected:
        data = snap["panels"][panel]
        series = data["hour"]
        tolerance = 1e-6 / data["factor"]
        if series["baseline"] is None or series["end_sum"] is None:
            raise RecoveryError("repair_no_statistics")  # Never invent a first baseline.
        following = next((r for r in series["rows"] if r["start"] >= end and r.get("sum") is not None), None)
        if following is not None and following["sum"] < series["end_sum"] - tolerance:
            raise RecoveryError("repair_boundary")
        # One common completed future endpoint must agree. A public delta moves
        # both series equally and cannot fix an existing offset between them.
        short = {r["start"]: r["sum"] for r in data["short"]["rows"] if r.get("sum") is not None}
        common = [r for r in series["rows"] if r["start"] >= end
                  and r["start"] + 3300 in short and r.get("sum") is not None]
        if not common or abs(common[-1]["sum"] - short[common[-1]["start"] + 3300]) > tolerance:
            raise RecoveryError("repair_no_alignment")
        for item in data["pending"]:
            record = item["record"]
            if not item["applied"]:
                if (record["day"] != analysis["day"] or item["spike"] is None
                    or record.get("statistics_journal")):
                    raise RecoveryError("repair_pending")
            if record["day"] == analysis["day"]:
                acknowledgements.append(record)
        offset = series["baseline"] + targets[panel] / data["factor"] - series["end_sum"]
        if abs(offset) > tolerance:
            steps.append(_delta_step(data, end, offset, steps))
        for item in data["pending"]:
            if not item["applied"]:
                delta = -sum(item["record"]["intervals"].values()) / data["factor"]
                steps.append(_delta_step(data, item["spike"], delta, steps))
        existing = {row["start"]: row for row in series["rows"]}
        points = []
        for stamp in range(start - 3600, end, 3600):
            row = existing.get(stamp)
            energy = sum(value for quarter, value in analysis["aggregate"].items() if int(quarter) < stamp + 3600)
            after = series["baseline"] + (energy * targets[panel] / total / data["factor"] if total else 0)
            if not isfinite(after):
                raise RecoveryError("repair_invalid_statistics")
            point = {"start": stamp, "before": row.get("sum") if row else None,
                     "state": row.get("state") if row else None,
                     "after": after, "existed": row is not None}
            if row is None or row.get("sum") is None or abs(row["sum"] - after) > tolerance:
                points.append(point)
        if points:
            steps.append(import_step(data["metadata"], points))
            operations.extend(points)
    return {**analysis, "selected": list(selected), "targets": targets, "operations": operations,
            "acknowledgements": acknowledgements, "journal": {"steps": steps}}


def undo_journal(plan):
    """Inverse deltas and replacement sums, but never delete imported buckets."""
    if any(not p["existed"] for s in plan["journal"]["steps"] if s["kind"] == "import" for p in s["points"]):
        raise RecoveryError("repair_undo_conflict")
    steps = []
    for original in reversed(plan["journal"]["steps"]):
        if original["kind"] == "import":
            points = [{**p, "before": p["after"], "after": p["before"]} for p in original["points"]]
            steps.append(import_step(original["metadata"], points))
        else:
            steps.append({**original, "phase": "prepared", "delta": -original["delta"],
                          "witnesses": [{**p, "before": p["after"], "after": p["before"]}
                                        for p in original["witnesses"]]})
    return {"steps": steps}
