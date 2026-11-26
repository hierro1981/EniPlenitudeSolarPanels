"""Public Recorder operations with durable, verified, non-transactional journals."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_metadata,
    statistics_during_period,
)

from .recovery import day_bounds, number

_RECORDER_TIMEOUT = 30
_READ_JOBS = "plenitude_panels_recorder_read_jobs"


class RecorderTimeoutError(TimeoutError):
    """An operation may still be running: retain its durable journal."""


def utc(stamp):
    return datetime.fromtimestamp(stamp, UTC)


def statistics_lock(hass, entry_id):
    return hass.data.setdefault("plenitude_panels_statistics_locks", {}).setdefault(entry_id, asyncio.Lock())


def read_series(instance, entity, start, end=None, period="hour"):
    """Read normalized sums and raw statistic states in their stored units."""
    meta = metadata(instance, entity)
    units = {"energy": meta["unit_of_measurement"]} if meta else None
    return statistics_during_period(
        instance.hass, utc(start), utc(end) if end is not None else None,
        {entity}, period, units, {"sum", "state"},
    ).get(entity, [])


async def async_recorder_job(hass, job, *args):
    """Run public read APIs in Recorder's executor, never on its write queue."""
    jobs = hass.data.setdefault(_READ_JOBS, {})
    # Lists/dicts are deliberately not deduplicated.
    key = repr((job, args))
    task = jobs.get(key)
    if task is None:
        task = asyncio.ensure_future(get_instance(hass).async_add_executor_job(
            job, get_instance(hass), *args,
        ))
        jobs[key] = task

        def finished(done):
            if jobs.get(key) is done:
                jobs.pop(key, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
    try:
        return await asyncio.wait_for(asyncio.shield(task), _RECORDER_TIMEOUT)
    except TimeoutError as err:
        raise RecorderTimeoutError(f"Recorder read {job.__name__} timed out") from err


async def async_barrier(hass):
    """Drain previously submitted public operations before inspecting witnesses."""
    try:
        await asyncio.wait_for(
            asyncio.shield(get_instance(hass).async_block_till_done()), _RECORDER_TIMEOUT,
        )
    except TimeoutError as err:
        raise RecorderTimeoutError("Recorder verification timed out") from err


def same(left, right, tolerance=1e-6):
    return left is None and right is None or (
        left is not None and right is not None and abs(left - right) < tolerance
    )


def metadata(instance, entity):
    value = get_metadata(instance.hass, statistic_ids={entity}).get(entity)
    return dict(value[1]) if value else None


def import_step(meta, points):
    """Preserve each existing statistic's state when replacing its hourly sum."""
    return {"kind": "import", "entity": meta["statistic_id"], "metadata": meta,
            "points": points, "phase": "prepared"}


async def async_execute_journal(hass, recovery, journal):
    """Resume only from observed before/after values, never blindly replay a delta.

    A queue barrier also covers an earlier timed-out call in this process. After
    a restart uncommitted queue tasks no longer exist. Missing/changed witnesses
    are a conflict, not permission to guess. Each public call is a separate commit.
    """
    from .manual_recovery import RecoveryError

    await async_barrier(hass)
    for step in journal["steps"]:
        if step["phase"] == "done":
            continue
        entity = step["entity"]
        current_metadata = await async_recorder_job(hass, metadata, entity)
        if current_metadata is None or current_metadata.get("unit_of_measurement") != step.get(
            "unit", step.get("metadata", {}).get("unit_of_measurement"),
        ) or current_metadata.get("source") != "recorder":
            raise RecoveryError("preview_stale")
        tolerance = {"Wh": 1e-6, "kWh": 1e-9, "MWh": 1e-12}[current_metadata["unit_of_measurement"]]

        def matches(left, right):
            return same(left, right, tolerance)

        if step["kind"] == "delta":
            observations = []
            for point in step["witnesses"]:
                rows = await async_recorder_job(
                    hass, read_series, entity, point["start"], point["start"] + 1, point["period"],
                )
                actual = next((row.get("sum") for row in rows if row["start"] == point["start"]), None)
                observations.append((matches(actual, point["before"]), matches(actual, point["after"])))
            if not observations:
                raise RecoveryError("repair_undo_conflict")
            if all(after for _, after in observations):
                pass
            elif all(before for before, _ in observations):
                if step["phase"] != "prepared":
                    # Recorder's drain API can report an empty queue while a
                    # task is already executing. A previously submitted delta
                    # with unchanged witnesses is therefore ambiguous, even
                    # after a restart (crash may precede queue submission).
                    # Never submit it a second time.
                    raise RecoveryError("repair_undo_conflict")
                # Persist intent BEFORE queueing. On timeout the next attempt
                # drains the queue and checks the same immutable witnesses.
                step["phase"] = "submitted"
                await recovery.store.async_save(recovery.state)
                get_instance(hass).async_adjust_statistics(
                    entity, utc(step["start"]), step["delta"], step["unit"],
                )
                await async_barrier(hass)
                # Re-read, do not mark done merely because queue submission worked.
                for point in step["witnesses"]:
                    rows = await async_recorder_job(
                        hass, read_series, entity, point["start"], point["start"] + 1, point["period"],
                    )
                    if not any(row["start"] == point["start"] and matches(row.get("sum"), point["after"])
                               for row in rows):
                        raise RecoveryError("repair_undo_conflict")
            else:
                raise RecoveryError("repair_undo_conflict")
        else:
            points = step["points"]
            rows = await async_recorder_job(
                hass, read_series, entity, min(p["start"] for p in points),
                max(p["start"] for p in points) + 3600,
            )
            actual = {row["start"]: row for row in rows}
            todo = []
            for point in points:
                row = actual.get(point["start"])
                value = row.get("sum") if row else None
                if row is not None and matches(value, point["after"]) and matches(row.get("state"), point["state"]):
                    continue
                if ((row is not None) != point["existed"] or not matches(value, point["before"])
                    or row is not None and not matches(row.get("state"), point["state"])):
                    raise RecoveryError("preview_stale")
                todo.append({"start": utc(point["start"]), "sum": point["after"], "state": point["state"]})
            if todo:
                await recovery.store.async_save(recovery.state)
                async_import_statistics(hass, dict(step["metadata"]), todo)
                await async_barrier(hass)
            rows = await async_recorder_job(
                hass, read_series, entity, min(p["start"] for p in points),
                max(p["start"] for p in points) + 3600,
            )
            actual = {row["start"]: row for row in rows}
            if any(p["start"] not in actual or not matches(actual[p["start"]].get("sum"), p["after"])
                   or not matches(actual[p["start"]].get("state"), p["state"]) for p in points):
                raise RecoveryError("repair_undo_conflict")
        step["phase"] = "done"
        await recovery.store.async_save(recovery.state)


def counted_spike(instance, entity, record, meta):
    """Identify an observed increase with a pre-existing statistic baseline."""
    states = get_significant_states(
        instance.hass, utc(record["created"]), entity_ids=[entity],
        significant_changes_only=False,
    ).get(entity, [])
    first = next((state for state in states
                  if state.last_updated.timestamp() >= record["created"] and number(state.state) is not None
                  and (number(state.attributes.get("recovered_energy_wh")) or 0) >= record["target"] - 1e-6), None)
    if first is None:
        return None
    previous_raw = [state for state in states
                    if state.last_updated < first.last_updated and number(state.state) is not None
                    and (number(state.attributes.get("recovered_energy_wh", 0)) or 0) < record["target"] - 1e-6]
    if not previous_raw:
        # A later live increase after raw-history purge is not evidence that the
        # original recovery was counted. Require both sides of the raw change.
        return None
    spike = int(first.last_updated.timestamp() // 300) * 300
    prior = read_series(instance, entity, 0, spike, "5minute")
    prior = [row for row in prior if row.get("state") is not None]
    following = read_series(instance, entity, spike, spike + 300, "5minute")
    if not prior or not following:
        return None
    raw_factor = {"Wh": 1, "kWh": 1000, "MWh": 1000000}.get(first.attributes.get("unit_of_measurement"))
    stat_factor = {"Wh": 1, "kWh": 1000, "MWh": 1000000}.get(meta["unit_of_measurement"])
    if raw_factor is None or stat_factor is None:
        return None
    amount = sum(record["intervals"].values())
    if (number(first.state) * raw_factor - prior[-1]["state"] * stat_factor < amount - 1e-6
        or following[0].get("sum") is None or prior[-1].get("sum") is None
        or following[0]["sum"] - prior[-1]["sum"] < amount / stat_factor - 1e-6):
        return None
    return spike


def recovery_plan(instance, entity, record):
    """Move a proven counted spike into completed earlier hourly buckets only."""
    meta = metadata(instance, entity)
    if not meta or meta.get("source") != "recorder":
        return None
    factor = {"Wh": 1, "kWh": .001, "MWh": .000001}.get(meta["unit_of_measurement"])
    if factor is None:
        return None
    spike = counted_spike(instance, entity, record, meta)
    if spike is None:
        return None
    spike = spike // 3600 * 3600
    bounds = record.get("day_bounds") or day_bounds(
        datetime.fromisoformat(record["day"]).date(), UTC,
    )
    start = bounds[0] - 3600
    if start % 3600 or bounds[1] > spike:
        return None
    rows = read_series(instance, entity, 0)
    prior = [r for r in rows if r["start"] < spike and r.get("sum") is not None and r.get("state") is not None]
    following = [r for r in rows if r["start"] >= spike and r.get("sum") is not None]
    if not prior or not following:
        return None  # Unknown first baseline or hour not compiled yet.
    relevant = [r for r in rows if r["start"] >= start - 3600]
    sums = [r["sum"] for r in relevant if r.get("sum") is not None]
    if any(b < a - 1e-6 for a, b in zip(sums, sums[1:])):
        raise ValueError("Falling production statistics: use Repair statistics")
    existing = {r["start"]: r for r in rows}
    baseline_rows = [r for r in rows if r["start"] < start and r.get("sum") is not None]
    baseline = baseline_rows[-1]["sum"] if baseline_rows else next(
        (r["sum"] for r in rows if r["start"] < spike and r.get("sum") is not None), None,
    )
    if baseline is None:
        return None
    points = []
    for stamp in range(start, spike, 3600):
        row = existing.get(stamp)
        if row and row.get("sum") is not None:
            baseline = row["sum"]
        addition = sum(value * factor for quarter, value in record["intervals"].items()
                       if int(quarter) < stamp + 3600)
        points.append({"start": stamp, "before": row.get("sum") if row else None,
                       "state": row.get("state") if row else None, "existed": row is not None,
                       "after": baseline + addition})
    if points[-1]["after"] > following[0]["sum"] + 1e-6:
        return None
    return {"steps": [import_step(meta, points)]}


async def async_move_recovery(hass, entry_id, entity_id, record, recovery) -> bool:
    """Keep the journal inside the existing recovery Store until acknowledged."""
    async with statistics_lock(hass, entry_id):
        if record not in recovery.state["pending"]:
            return False
        return await _async_move_recovery(hass, entry_id, entity_id, record, recovery)


async def _async_move_recovery(hass, entry_id, entity_id, record, recovery):
    if "statistics_journal" not in record:
        # Old transactional audits remain read-only and prevent a second move.
        from .recovery_history import read_audits

        audits = await async_recorder_job(hass, read_audits, entry_id, (record["panel"],), (entity_id,))
        stamp = datetime.fromisoformat(record["day"]).replace(tzinfo=UTC).timestamp()
        if any(t == stamp and target >= record["target"] - 1e-6
               for t, target in audits.get(record["panel"], [])):
            return True
        await async_barrier(hass)
        plan = await async_recorder_job(hass, recovery_plan, entity_id, record)
        if plan is None:
            return False
        record["statistics_journal"] = plan
        await recovery.store.async_save(recovery.state)
    await async_execute_journal(hass, recovery, record["statistics_journal"])
    return True
