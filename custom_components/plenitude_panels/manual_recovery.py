"""Analyze and apply a confirmed historical recovery without changing settings."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import CONF_ACCOUNT_ID, CONF_POD, CONF_SUPPLY_ID, DOMAIN
from .recovery import aggregate_intervals, number, production, reconstruct


class RecoveryError(ValueError):
    """A recoverable wizard error identified by a translation key."""


def _prior(recovery, day):
    return deepcopy(recovery.state["allocations"].get(day, {}))


def _recorded_panels(recovery, day, panels, samples, aggregate, prior, history):
    """Keep known totals and observed lower bounds independent for each panel."""
    windows = [str((quarter + offset) // 300)
               for quarter, value in aggregate.items() if value > 0
               for offset in (0, 300, 600)]
    marker_time = datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp()
    result = {}
    for panel in panels:
        raw = [number(values.get(panel)) for values in samples.values()]
        valid = [value for value in raw if value is not None]
        observed = sum(valid) if valid else None
        value = None
        source = "partial" if valid else "unavailable"
        uncertain = panel in history.get("uncertain_panels", set())
        if not valid and (panel in history["daily"] or uncertain):
            # Present but unusable statistics are not evidence of absent history.
            source = "uncertain"
        if panel in history["daily"] and not uncertain:
            # A queued recovery is not yet part of the original day's statistics,
            # unless its transactional audit already confirms the commit.
            marker = max((target for stamp, target in history["audits"].get(panel, [])
                          if stamp == marker_time), default=0)
            unapplied = sum(sum(record["intervals"].values())
                            for record in recovery.state["pending"]
                            if record["day"] == day and record["panel"] == panel
                            and marker < record["target"] - 1e-6)
            applied = max(0, prior.get(panel, {}).get("total", 0) - unapplied)
            original = history["daily"][panel] - applied
            if original >= -1e-6:
                value = max(0, original)
                source = "statistics"
        if value is None and windows and all(
            number(samples.get(window, {}).get(panel)) is not None for window in windows
        ):
            value = observed
            source = "samples"
        result[panel] = {"value": value, "observed": observed, "source": source}
    return result


def _migrate_audits(recovery, audits):
    """Recover per-day amounts of completed 0.7 allocations when auditable."""
    ledger = deepcopy(recovery.state["allocations"])
    for panel in recovery.state["totals"]:
        known = sum(allocations.get(panel, {}).get("total", 0)
                    for allocations in ledger.values())
        if abs(known - recovery.total(panel)) < 1e-6:
            continue
        targets = {
            target: datetime.fromtimestamp(stamp, UTC).date().isoformat()
            for stamp, target in audits.get(panel, [])
        }
        targets.update({record["target"]: record["day"] for record in recovery.state["pending"]
                        if record["panel"] == panel})
        if not targets or abs(max(targets) - recovery.total(panel)) > 1e-6:
            raise RecoveryError("legacy_recovery_unknown")
        previous = 0.0
        amounts = {}
        for target, day in sorted(targets.items()):
            amounts[day] = amounts.get(day, 0) + target - previous
            previous = target
        for day, amount in amounts.items():
            allocation = ledger.setdefault(day, {}).get(panel)
            if allocation is not None:
                if abs(allocation["total"] - amount) > 1e-6:
                    raise RecoveryError("legacy_recovery_unknown")
            else:
                ledger[day][panel] = {"total": amount, "intervals": {}}
    recovery.state["allocations"] = ledger


async def async_analyze(hass, recovery, panels, day: date, *, allow_zero=False) -> dict:
    """Fetch the requested day and merge local samples with actual Recorder data."""
    if day >= dt_util.now().date():
        raise RecoveryError("date_not_past")
    pod = recovery.entry.data.get(CONF_POD, "")
    if not pod:
        raise RecoveryError("pod_required")
    if not panels:
        raise RecoveryError("no_panels")
    payload = await recovery.api.async_fetch_daily_production(pod, day)
    aggregate = aggregate_intervals(payload, day, dt_util.get_default_time_zone())
    if not any(aggregate.values()) and not allow_zero:
        raise RecoveryError("aggregate_not_ready")
    key = day.isoformat()
    local = recovery.state["days"].get(key, {})
    samples = deepcopy(local.get("samples", {}))
    powers = {panel: details.get("power") for panel, details in panels.items()}
    powers.update(local.get("powers", {}))
    history = {"samples": {}, "powers": {}, "daily": {}, "audits": {}}
    if "recorder" in hass.config.components:
        from homeassistant.components.recorder import get_instance
        from sqlalchemy.exc import SQLAlchemyError

        from .recovery_history import read_history

        registry = er.async_get(hass)
        prefix = recovery.entry.unique_id or recovery.entry.entry_id
        entities = {}
        for panel in panels:
            entity_id = recovery.entities.get(panel) or registry.async_get_entity_id(
                "sensor", DOMAIN, f"{prefix}_{panel}_total_production",
            )
            if entity_id:
                entities[panel] = entity_id
        instance = get_instance(hass)
        try:
            history = await instance.async_add_executor_job(
                read_history, instance, recovery.entry.entry_id, entities,
                day, dt_util.get_default_time_zone(), tuple(recovery.state["totals"]),
                deepcopy(recovery.state["pending"]),
            )
        except (SQLAlchemyError, OSError) as err:
            raise RecoveryError("history_unavailable") from err
        for window, values in history["samples"].items():
            destination = samples.setdefault(window, {})
            for panel, value in values.items():
                if destination.get(panel) is None:
                    destination[panel] = value
        powers.update(history["powers"])
    _migrate_audits(recovery, history["audits"])
    await recovery.store.async_save(recovery.state)
    prior = _prior(recovery, key)
    recorded = _recorded_panels(recovery, key, panels, samples, aggregate, prior, history)
    known = (
        sum(item["value"] for item in recorded.values())
        if all(item["value"] is not None for item in recorded.values())
        and set(powers) == set(panels) else None
    )
    missing_all = all(
        item["source"] == "unavailable" and item["value"] is None and item["observed"] is None
        for item in recorded.values()
    )
    suggested_panels = [
        panel for panel, item in sorted(recorded.items())
        if not ((item["value"] or 0) > 0 or (item["observed"] or 0) > 0)
        and (item["value"] == 0 or item["source"] == "unavailable")
    ]
    automatic = None
    if set(powers) == set(panels):
        try:
            automatic = reconstruct(samples, powers, aggregate)
        except ValueError:
            pass
    productive = [str((quarter + offset) // 300)
                  for quarter, value in aggregate.items() if value > 0
                  for offset in (0, 300, 600)]
    if any(set(samples.get(window, {})) != set(panels) for window in productive):
        automatic = None
    # A legacy audit gives only an amount, not its quarter-hour distribution.
    if any(value["total"] > 0 and not value["intervals"] for value in prior.values()):
        automatic = None
    return {
        "day": key, "pod": pod, "panels": deepcopy(panels), "powers": powers,
        "aggregate": aggregate, "prior": prior, "known": known,
        "recorded": recorded,
        "negative_api_samples": sum(
            production(row.get("energyProduced")) == 0 and number(row.get("energyProduced")) is None
            for row in payload["energyProducedConsumed"]
        ),
        "suggested_panels": suggested_panels,
        # A form suggestion is not a detected total: preserve None/— in history.
        "suggested_known": 0.0 if missing_all else known,
        "identity": [recovery.entry.data[CONF_ACCOUNT_ID], recovery.entry.data[CONF_SUPPLY_ID]],
        "automatic": automatic, "source": "local_and_recorder" if history["samples"] else "local",
    }


def automatic_plan(analysis: dict) -> dict:
    """Subtract previously applied quarter-hour allocations from the proposal."""
    desired = analysis["automatic"]
    if desired is None:
        raise RecoveryError("manual_data_required")
    allocation = {}
    for panel in set(desired) | set(analysis["prior"]):
        old = analysis["prior"].get(panel, {}).get("intervals", {})
        proposed = {str(stamp): value for stamp, value in desired.get(panel, {}).items()}
        if any(proposed.get(stamp, 0) < value - 1e-6 for stamp, value in old.items()):
            raise RecoveryError("manual_data_required")
        intervals = {stamp: value - old.get(stamp, 0) for stamp, value in proposed.items()
                     if value - old.get(stamp, 0) > 1e-6}
        if intervals:
            allocation[panel] = intervals
    if analysis["known"] is not None:
        proposed_total = sum(sum(intervals.values()) for intervals in allocation.values())
        prior_total = sum(value["total"] for value in analysis["prior"].values())
        if analysis["known"] + prior_total + proposed_total > sum(analysis["aggregate"].values()) + 0.1:
            raise RecoveryError("manual_data_required")
    return {**analysis, "allocation": allocation, "mode": "quarter_hour"}


def daily_plan(analysis: dict, selected: list[str], known_wh: float) -> dict:
    """Allocate only the remaining daily deficit, using the aggregate profile."""
    if not selected or len(set(selected)) != len(selected) or set(selected) - set(analysis["panels"]):
        raise RecoveryError("invalid_panels")
    known = number(known_wh)
    if known is None:
        raise RecoveryError("invalid_energy")
    total = sum(analysis["aggregate"].values())
    prior = sum(value["total"] for value in analysis["prior"].values())
    if known + prior > total + 0.1:
        raise RecoveryError("energy_exceeds_aggregate")
    residual = max(0, total - known - prior)
    weights = {panel: number(analysis["powers"].get(panel)) for panel in selected}
    if not all(weights.values()):
        weights = dict.fromkeys(selected, 1.0)
    weight_sum = sum(weights.values())
    allocation = {
        panel: {str(stamp): residual * value / total * weight / weight_sum
                for stamp, value in analysis["aggregate"].items() if value > 0}
        for panel, weight in weights.items()
    } if residual > 1e-6 else {}
    return {
        **analysis, "known": known, "allocation": allocation,
        "mode": "daily_estimate", "selected_panels": list(selected),
    }


async def async_apply(recovery, plan):
    """Reject stale previews before committing a monotonic, durable top-up."""
    identity = [recovery.entry.data[CONF_ACCOUNT_ID], recovery.entry.data[CONF_SUPPLY_ID]]
    if (plan["pod"] != recovery.entry.data.get(CONF_POD)
        or plan["identity"] != identity
        or plan["prior"] != _prior(recovery, plan["day"])):
        raise RecoveryError("preview_stale")
    if not plan["allocation"]:
        return
    await recovery.async_allocate(plan["day"], plan["allocation"], datetime.now(UTC))
    recovery.status = "manual_reconciled"
