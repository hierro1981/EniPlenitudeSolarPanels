"""Read original panel production and legacy recovery audits from Recorder."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from homeassistant.components.recorder.db_schema import (
    StateAttributes,
    States,
    StatesMeta,
    Statistics,
)
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.helpers.recorder import session_scope

from .const import DOMAIN
from .recovery import day_bounds, number


def read_history(instance, entry_id, entities, day, zone, audit_panels=(), pending=()):
    """Read only recorded sampling IDs; never forward-fill missing telemetry."""
    start, end = day_bounds(day, zone)
    samples = {}
    powers = {}
    daily = {}
    audits = {}
    uncertain = set()
    # Use the same normalized change calculation as the Energy Dashboard. Missing
    # midnight/nighttime rows must not discard a valid daytime production total.
    daily_rows = statistics_during_period(
        instance.hass,
        datetime.fromtimestamp(start, UTC),
        datetime.fromtimestamp(end, UTC) - timedelta(microseconds=1),
        set(entities.values()), "day", {"energy": "Wh"}, {"change"},
    ) if entities else {}
    for panel, entity_id in entities.items():
        rows = daily_rows.get(entity_id, [])
        values = [number(row.get("change")) for row in rows]
        if rows and all(value is not None for value in values):
            daily[panel] = sum(values)
    audit_ids = {
        panel: f"{DOMAIN}:recovery_audit_"
        + sha256(f"{entry_id}:{panel}".encode()).hexdigest()[:32]
        for panel in set(entities) | set(audit_panels)
    }
    with session_scope(session=instance.get_session(), read_only=True) as session:
        metadata = instance.statistics_meta_manager.get_many(
            session, statistic_ids=set(entities.values()) | set(audit_ids.values()),
        ) if entities or audit_ids else {}
        for panel, entity_id in entities.items():
            query = session.query(
                States.last_updated_ts, States.state, StateAttributes.shared_attrs,
            ).join(StatesMeta, States.metadata_id == StatesMeta.metadata_id).outerjoin(
                StateAttributes, States.attributes_id == StateAttributes.attributes_id,
            ).filter(StatesMeta.entity_id == entity_id)
            baseline = query.filter(States.last_updated_ts < start).order_by(
                States.last_updated_ts.desc(),
            ).first()
            rows = query.filter(
                States.last_updated_ts >= start, States.last_updated_ts < end + 300,
            ).order_by(States.last_updated_ts).all()
            previous = None
            for _, state, attributes in ([baseline] if baseline else []) + rows:
                attrs = json.loads(attributes or "{}")
                value = number(state)
                factor = {"Wh": 1, "kWh": 1000}.get(attrs.get("unit_of_measurement"))
                sample_id = attrs.get("last_sample_id")
                if value is None or factor is None or not isinstance(sample_id, int):
                    previous = None
                    continue
                value = value * factor - (number(attrs.get("recovered_energy_wh", 0)) or 0)
                interval = (sample_id - 1) * 300
                if previous is not None and sample_id == previous[0] + 1 and start <= interval < end:
                    delta = value - previous[1]
                    if delta >= -1e-6:
                        samples.setdefault(str(sample_id - 1), {})[panel] = max(0, delta)
                previous = (sample_id, value)
                if "power" in attrs:
                    powers[panel] = attrs["power"]
        for panel, audit_id in audit_ids.items():
            if audit_id in metadata:
                audit_rows = session.query(Statistics.start_ts, Statistics.state).filter_by(
                    metadata_id=metadata[audit_id][0],
                ).all()
                audits[panel] = [(stamp, target) for stamp, target in audit_rows if target is not None]
        for record in pending:
            panel = record["panel"]
            if panel not in entities or record["day"] == day.isoformat() or record["created"] >= end:
                continue
            marker_time = datetime.fromisoformat(record["day"]).replace(tzinfo=UTC).timestamp()
            if any(stamp == marker_time and target >= record["target"] - 1e-6
                   for stamp, target in audits.get(panel, [])):
                continue
            # Another day's still-unmoved recovery may appear as an increase in
            # this day. Do not call it original production or hide other panels.
            records = session.query(States.last_updated_ts, States.state, StateAttributes.shared_attrs).join(
                StatesMeta, States.metadata_id == StatesMeta.metadata_id,
            ).join(StateAttributes, States.attributes_id == StateAttributes.attributes_id).filter(
                StatesMeta.entity_id == entities[panel],
                States.last_updated_ts >= record["created"], States.last_updated_ts < end,
            ).order_by(States.last_updated_ts)
            for stamp, state, attributes in records:
                attrs = json.loads(attributes or "{}")
                if number(state) is not None and attrs.get("recovered_energy_wh", 0) >= record["target"] - 1e-6:
                    if stamp >= start:
                        uncertain.add(panel)
                    break
    return {
        "samples": samples, "powers": powers, "daily": daily,
        "audits": audits, "uncertain_panels": uncertain,
    }
