"""Read original telemetry and legacy audits through public Recorder APIs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256

from homeassistant.components.recorder.history import get_significant_states
from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period

from .const import DOMAIN
from .recovery import day_bounds, number


def read_audits(instance, entry_id, panels, entities=()):
    """Legacy DB markers are immutable; new journals live in integration Store."""
    ids = {panel: f"{DOMAIN}:recovery_audit_"
           + sha256(f"{entry_id}:{panel}".encode()).hexdigest()[:32] for panel in panels}
    existing = get_metadata(instance.hass, statistic_ids=set(ids.values())) if ids else {}
    if not existing:
        return {}
    rows = statistics_during_period(
        instance.hass, datetime(1970, 1, 1, tzinfo=UTC), None,
        # The public API discards "state" if ALL requested metadata has_sum=False.
        # Request the integration's real energy IDs alongside its legacy audits.
        set(existing) | set(entities), "hour", {"energy": "Wh"}, {"state"},
    ) if ids else {}
    return {panel: [(row["start"], row["state"]) for row in rows.get(entity, [])
                    if row.get("state") is not None] for panel, entity in ids.items()}


def read_history(instance, entry_id, entities, day, zone, audit_panels=(), pending=()):
    """Keep attribute-only sample updates; never forward-fill missing telemetry."""
    start, end = day_bounds(day, zone)
    samples, powers, daily = {}, {}, {}
    audits = read_audits(instance, entry_id, set(entities) | set(audit_panels), entities.values())
    uncertain = set()
    daily_rows = statistics_during_period(
        instance.hass, datetime.fromtimestamp(start, UTC),
        datetime.fromtimestamp(end, UTC) - timedelta(microseconds=1),
        set(entities.values()), "day", {"energy": "Wh"}, {"change"},
    ) if entities else {}
    states = get_significant_states(
        instance.hass, datetime.fromtimestamp(start, UTC), datetime.fromtimestamp(end + 300, UTC),
        list(entities.values()), significant_changes_only=False,
    ) if entities else {}
    for panel, entity in entities.items():
        rows = daily_rows.get(entity, [])
        values = [number(row.get("change")) for row in rows]
        if rows and all(value is not None for value in values):
            daily[panel] = sum(values)
        previous = None
        for state in states.get(entity, []):
            attrs = state.attributes
            value = number(state.state)
            factor = {"Wh": 1, "kWh": 1000}.get(attrs.get("unit_of_measurement"))
            sample = attrs.get("last_sample_id")
            if value is None or factor is None or not isinstance(sample, int):
                previous = None
                continue
            value = value * factor - (number(attrs.get("recovered_energy_wh", 0)) or 0)
            interval = (sample - 1) * 300
            if previous is not None and sample == previous[0] + 1 and start <= interval < end:
                delta = value - previous[1]
                if delta >= -1e-6:
                    samples.setdefault(str(sample - 1), {})[panel] = max(0, delta)
            previous = (sample, value)
            if "power" in attrs:
                powers[panel] = attrs["power"]
        for record in pending:
            if record["panel"] != panel or record["day"] == day.isoformat() or record["created"] >= end:
                continue
            marker = datetime.fromisoformat(record["day"]).replace(tzinfo=UTC).timestamp()
            if any(stamp == marker and target >= record["target"] - 1e-6 for stamp, target in audits.get(panel, [])):
                continue
            # If the source raw history was purged, lack of a match is not proof
            # that this day's increase is original production.
            uncertain.add(panel)
    return {"samples": samples, "powers": powers, "daily": daily,
            "audits": audits, "uncertain_panels": uncertain}
