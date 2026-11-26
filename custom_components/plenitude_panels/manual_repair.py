"""Durable repair journal and orchestration for the options-flow statistics tool."""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import CONF_ACCOUNT_ID, CONF_POD, CONF_SUPPLY_ID, DOMAIN
from .manual_recovery import RecoveryError
from .manual_recovery import async_analyze as analyze_recovery
from .recovery import day_bounds
from .recovery_statistics import (
    async_barrier, async_execute_journal, async_recorder_job, statistics_lock,
)
from .statistics_repair import snapshot, undo_journal


def _require_recorder(hass):
    if "recorder" not in hass.config.components:
        raise RecoveryError("repair_recorder_required")


async def async_analyze(hass, recovery, panels, day):
    _require_recorder(hass)
    await async_reconcile_journal(hass, recovery)
    analysis = await analyze_recovery(hass, recovery, panels, day, allow_zero=True)
    registry = er.async_get(hass)
    prefix = recovery.entry.unique_id or recovery.entry.entry_id
    entities = {
        panel: recovery.entities.get(panel) or registry.async_get_entity_id(
            "sensor", DOMAIN, f"{prefix}_{panel}_total_production",
        ) for panel in panels
    }
    if any(entity is None for entity in entities.values()):
        raise RecoveryError("repair_no_statistics")
    bounds = day_bounds(day, dt_util.get_default_time_zone())
    analysis["snapshot"] = await async_recorder_job(
        hass, snapshot, recovery.entry.entry_id, entities, bounds, deepcopy(recovery.state["pending"]),
    )
    if set(analysis["snapshot"]["panels"]) != set(panels):
        raise RecoveryError("repair_no_statistics")
    aggregate_wh = sum(analysis["aggregate"].values())
    analysis["suggested_repair"] = [
        panel for panel, data in analysis["snapshot"]["panels"].items()
        if data["hour"]["negative"] or not data["hour"]["has_day"]
        or data["hour"]["total"] <= 0 or any(not item["applied"] for item in data["pending"])
        or data["hour"]["total"] * data["factor"] > aggregate_wh + 0.1
    ]
    return analysis


def _acknowledge(recovery, records):
    keys = {(record["panel"], record["day"], record["target"]) for record in records}
    recovery.state["pending"] = [record for record in recovery.state["pending"]
                                  if (record["panel"], record["day"], record["target"]) not in keys]


def _restore_pending(recovery, records):
    keys = {(record["panel"], record["day"], record["target"]) for record in recovery.state["pending"]}
    recovery.state["pending"].extend(deepcopy(record) for record in records
                                      if (record["panel"], record["day"], record["target"]) not in keys)
    recovery.state["pending"].sort(key=lambda record: (record["created"], record["target"]))


async def async_reconcile_journal(hass, recovery):
    """Resume verified public calls, never infer a commit from a DB marker."""
    async with statistics_lock(hass, recovery.entry.entry_id):
        await _async_reconcile_journal(hass, recovery)


async def _async_reconcile_journal(hass, recovery):
    backup = recovery.state.get("repair_backup")
    if not backup or backup["phase"] in ("failed", "undone"):
        return
    _require_recorder(hass)
    if "journal" not in backup.get("plan", {}):
        # Preserve legacy backups, but old private-schema undo cannot be replayed.
        if backup["phase"] == "applied":
            return
        raise RecoveryError("repair_undo_conflict")
    before = deepcopy(recovery.state)
    if backup["phase"] == "undo_prepared":
        await async_execute_journal(hass, recovery, backup["undo_journal"])
        backup["phase"] = "undone"
        _restore_pending(recovery, backup["plan"]["acknowledgements"])
    elif backup["phase"] == "prepared":
        await async_execute_journal(hass, recovery, backup["plan"]["journal"])
        backup["phase"] = "applied"
        backup.pop("previous_backup", None)
        _acknowledge(recovery, backup["plan"]["acknowledgements"])
    if before != recovery.state:
        try:
            await recovery.store.async_save(recovery.state)
        except Exception:
            recovery.state = before
            raise


async def async_apply(hass, recovery, plan):
    async with statistics_lock(hass, recovery.entry.entry_id):
        return await _async_apply(hass, recovery, plan)


async def _async_apply(hass, recovery, plan):
    _require_recorder(hass)
    await _async_reconcile_journal(hass, recovery)
    identity = [recovery.entry.data[CONF_ACCOUNT_ID], recovery.entry.data[CONF_SUPPLY_ID]]
    if (identity != plan["identity"] or recovery.entry.data.get(CONF_POD) != plan["pod"]
        or recovery.state["allocations"].get(plan["day"], {}) != plan["prior"]):
        raise RecoveryError("preview_stale")
    if not plan["journal"]["steps"] and not plan["acknowledgements"]:
        return False
    await async_barrier(hass)
    entities = {p: d["entity"] for p, d in plan["snapshot"]["panels"].items()}
    fresh = await async_recorder_job(
        hass, snapshot, recovery.entry.entry_id, entities, plan["snapshot"]["bounds"],
        deepcopy(recovery.state["pending"]),
    )
    # New tail rows are allowed; existing rows, metadata and pending proofs must
    # still match the preview. A delta affects appended rows on the public queue.
    for panel, old in plan["snapshot"]["panels"].items():
        new = fresh["panels"].get(panel)
        if new is None or new["metadata"] != old["metadata"] or new["pending"] != old["pending"]:
            raise RecoveryError("preview_stale")
        for period in ("hour", "short"):
            previous = {row["start"]: row for row in old[period]["rows"]}
            current = {row["start"]: row for row in new[period]["rows"]}
            if any(current.get(stamp) != row for stamp, row in previous.items()):
                raise RecoveryError("preview_stale")
            # Newly backfilled old rows also invalidate the proposed baseline.
            latest = max(previous, default=float("inf"))
            if any(stamp <= latest and stamp not in previous for stamp in current):
                raise RecoveryError("preview_stale")
    before = deepcopy(recovery.state)
    backup = {
        "token": int(uuid4().hex[:13], 16), "phase": "prepared", "plan": deepcopy(plan),
    }
    if (previous := before.get("repair_backup")) and previous["phase"] == "applied":
        backup["previous_backup"] = previous
    recovery.state["repair_backup"] = backup
    try:
        await recovery.store.async_save(recovery.state)
    except Exception:
        recovery.state = before
        raise
    await _async_reconcile_journal(hass, recovery)
    return True


async def async_undo(hass, recovery):
    async with statistics_lock(hass, recovery.entry.entry_id):
        await _async_undo(hass, recovery)


async def _async_undo(hass, recovery):
    _require_recorder(hass)
    await _async_reconcile_journal(hass, recovery)
    backup = recovery.state.get("repair_backup")
    if not backup or backup["phase"] != "applied":
        raise RecoveryError("repair_no_backup")
    if "journal" not in backup["plan"]:
        raise RecoveryError("repair_undo_conflict")
    backup["undo_journal"] = undo_journal(backup["plan"])
    backup["phase"] = "undo_prepared"
    try:
        await recovery.store.async_save(recovery.state)
    except Exception:
        backup["phase"] = "applied"
        raise
    await _async_reconcile_journal(hass, recovery)


def repair_summary(analysis, formatter, plan=None):
    rows = []
    for panel, details in sorted(analysis["panels"].items()):
        data = analysis["snapshot"]["panels"][panel]
        name = str(details.get("position") or panel).replace("|", "\\|").replace("\n", " ")
        current = formatter.number(data["hour"]["total"] * data["factor"])
        negative = len(data["hour"]["negative"])
        target = plan["targets"].get(panel, data["hour"]["total"] * data["factor"]) if plan else None
        rows.append(f"| {name} | {current} | {negative} | {formatter.number(target)} |")
    return {
        "date": analysis["day"], "aggregate": formatter.number(sum(analysis["aggregate"].values())),
        "panels": "\n".join(rows),
        "rows": formatter.number(len(plan["operations"]), precision=0) if plan else "—",
        "negative_api": formatter.number(analysis.get("negative_api_samples", 0), precision=0),
    }
