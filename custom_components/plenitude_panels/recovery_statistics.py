"""Move recorded recovery increases back in time on the Recorder's own queue.

The live total is recorded normally first. Counted increases are moved backwards
without changing the latest sum. Increases already included in Recorder's first
baseline are added to later sums as well. Existing states remain untouched, and
a transactional audit marker makes retry after a restart safe.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from hashlib import sha256
from time import time

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import (
    StateAttributes,
    States,
    StatesMeta,
    Statistics,
    StatisticsMeta,
    StatisticsShortTerm,
)
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.helpers.recorder import session_scope
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .recovery import day_bounds, number


def _fill_day(session, table, metadata_id, record, intervals, spike, counted, fallback, retention):
    """Create only missing buckets of the requested day and its baseline."""
    duration = 300 if table is StatisticsShortTerm else 3600
    bounds = record.get("day_bounds") or day_bounds(
        datetime.fromisoformat(record["day"]).date(), dt_util.get_default_time_zone(),
    )
    start = int(bounds[0] // duration) * duration - duration
    end = min(int((bounds[1] + duration - 1) // duration) * duration, spike // duration * duration)
    if table is StatisticsShortTerm:
        start = max(start, retention // duration * duration)
    if start >= end:
        return
    rows = session.query(table).filter(
        table.metadata_id == metadata_id, table.start_ts >= start, table.start_ts < end,
    ).order_by(table.start_ts).all()
    existing = {int(row.start_ts): row for row in rows}
    previous = session.query(table).filter(
        table.metadata_id == metadata_id, table.start_ts < start, table.sum.is_not(None),
    ).order_by(table.start_ts.desc()).first()
    following = next((row for row in rows if row.sum is not None), None)
    after = session.query(table).filter(
        table.metadata_id == metadata_id, table.start_ts >= end, table.sum.is_not(None),
    ).order_by(table.start_ts).first()
    reference = previous or following or after
    baseline = reference.sum if reference is not None else fallback[1] if fallback is not None else 0
    reference_time = reference.start_ts if reference is not None else fallback[0] if fallback is not None else 0
    if reference_time >= spike and counted:
        baseline -= sum(value for _, value in intervals)
    for stamp in range(start, end, duration):
        if stamp in existing:
            if existing[stamp].sum is not None:
                baseline = existing[stamp].sum
            continue
        # Insert the unadjusted baseline. The common adjustment below adds the
        # historical recovery to both existing and newly inserted buckets.
        session.add(table.from_stats(metadata_id, {
            "start": datetime.fromtimestamp(stamp, UTC), "sum": baseline,
        }, time()))
    session.flush()


def assert_monotonic(session, table, metadata_id, start, end):
    """Never commit a recovery across a falling normalized cumulative sum."""
    previous = session.query(table).filter(
        table.metadata_id == metadata_id, table.start_ts < start, table.sum.is_not(None),
    ).order_by(table.start_ts.desc()).first()
    rows = session.query(table).filter(
        table.metadata_id == metadata_id, table.start_ts >= start,
        table.start_ts <= end, table.sum.is_not(None),
    ).order_by(table.start_ts)
    last = previous.sum if previous else None
    for row in rows:
        if last is not None and row.sum < last - 1e-7:
            raise ValueError("Falling production statistics: use Repair statistics before recovering")
        last = row.sum


def _move(instance, entry_id: str, entity_id: str, record: dict) -> bool:
    """Apply sum adjustments and their deduplication marker in one transaction."""
    digest = sha256(f"{entry_id}:{record['panel']}".encode()).hexdigest()[:32]
    marker_id = f"{DOMAIN}:recovery_audit_{digest}"
    marker_time = datetime.fromisoformat(record["day"]).replace(tzinfo=UTC).timestamp()
    with session_scope(session=instance.get_session()) as session:
        manager = instance.statistics_meta_manager
        metadata = manager.get_many(session, statistic_ids={entity_id, marker_id})
        marker = None
        if marker_id in metadata:
            marker = session.query(Statistics).filter_by(
                metadata_id=metadata[marker_id][0], start_ts=marker_time,
            ).first()
            if marker is not None and marker.state is not None and marker.state >= record["target"] - 1e-6:
                return True
        if entity_id not in metadata:
            return False
        metadata_id, meta = metadata[entity_id]
        factor = {"Wh": 1.0, "kWh": 0.001}.get(meta["unit_of_measurement"])
        if factor is None:
            return False
        # Find the actual first recorded increase, rather than assuming that
        # publishing a state and committing it to Recorder happened together.
        state_query = session.query(States.last_updated_ts, States.state, StateAttributes.shared_attrs).join(
            StatesMeta, States.metadata_id == StatesMeta.metadata_id,
        ).join(StateAttributes, States.attributes_id == StateAttributes.attributes_id).filter(
            StatesMeta.entity_id == entity_id,
        )
        states = state_query.filter(States.last_updated_ts >= record["created"]).order_by(States.last_updated_ts)
        spike = None
        for stamp, state, attributes in states:
            attrs = json.loads(attributes or "{}")
            if number(state) is not None and attrs.get("recovered_energy_wh", 0) >= record["target"] - 1e-6:
                spike = int(stamp // 300) * 300
                break
        if spike is None:
            return False
        latest = session.query(StatisticsShortTerm.start_ts).filter_by(
            metadata_id=metadata_id,
        ).order_by(StatisticsShortTerm.start_ts.desc()).first()
        if latest is None or latest[0] < spike:
            return False  # The recovery must already be in the baseline sum.
        intervals = sorted((int(stamp), value * factor) for stamp, value in record["intervals"].items())
        prior_stat = session.query(StatisticsShortTerm).filter(
            StatisticsShortTerm.metadata_id == metadata_id, StatisticsShortTerm.start_ts < spike,
            StatisticsShortTerm.state.is_not(None),
        ).order_by(StatisticsShortTerm.start_ts.desc()).first()
        fallback = (prior_stat.start_ts, prior_stat.sum or 0) if prior_stat is not None else None
        counted = True
        if prior_stat is None:
            # If Recorder's very first baseline already contained this recovery,
            # it did not count the increase. Add it to later sums as well instead
            # of subtracting a spike which never existed.
            first = session.query(StatisticsShortTerm.start_ts).filter_by(
                metadata_id=metadata_id,
            ).filter(StatisticsShortTerm.state.is_not(None)).order_by(StatisticsShortTerm.start_ts).first()
            if first is None:
                return False
            first = first[0]
            baseline = state_query.filter(States.last_updated_ts <= first).order_by(
                States.last_updated_ts.desc(),
            ).first()
            if baseline is None or number(baseline[1]) is None:
                baseline = next((row for row in state_query.filter(
                    States.last_updated_ts >= first, States.last_updated_ts < first + 300,
                ).order_by(States.last_updated_ts) if number(row[1]) is not None), None)
            if baseline is not None:
                attrs = json.loads(baseline[2] or "{}")
                counted = attrs.get("recovered_energy_wh", 0) < record["target"] - 1e-6
        retention = int(latest[0] - getattr(instance, "keep_days", 10) * 86400)
        for table, duration in ((StatisticsShortTerm, 300), (Statistics, 3600)):
            assert_monotonic(session, table, metadata_id, intervals[0][0] - duration, float("inf"))
            _fill_day(session, table, metadata_id, record, intervals, spike, counted, fallback, retention)
            cutoff = spike // duration * duration if counted else float("inf")
            start = intervals[0][0] // duration * duration
            rows = session.query(table).filter(
                table.metadata_id == metadata_id,
                table.start_ts >= start,
                table.start_ts < cutoff,
            ).all()
            for row in rows:
                if row.sum is None:
                    continue
                # Spread a quarter's reconstructed energy over its three
                # five-minute buckets. Hourly sums get all completed quarters.
                end = row.start_ts + duration
                addition = sum(
                    value * min(3, max(0, (end - stamp) // 300)) / 3
                    for stamp, value in intervals
                )
                row.sum += addition
            session.flush()
            assert_monotonic(session, table, metadata_id, intervals[0][0] - duration, float("inf"))
        audit_metadata = {
            "statistic_id": marker_id, "source": DOMAIN,
            "name": "Production recovery audit (not an energy source)",
            "unit_of_measurement": "Wh",
            "mean_type": StatisticMeanType.NONE, "has_sum": False,
        }
        if hasattr(StatisticsMeta, "unit_class"):
            audit_metadata["unit_class"] = "energy"
        _, audit_metadata_id = manager.update_or_add(session, audit_metadata, metadata)
        if marker is not None:
            marker.state = record["target"]
        else:
            session.add(Statistics.from_stats(audit_metadata_id, {
                "start": datetime.fromtimestamp(marker_time, UTC),
                "state": record["target"],
            }, time()))
    return True


class _RecorderJob(RecorderTask):
    """Serialize integration statistics operations with Recorder generation."""

    def __init__(self, job, args, future) -> None:
        self.job = job
        self.args = args
        self.future = future

    def run(self, instance) -> None:
        try:
            result = self.job(instance, *self.args)
        except Exception as err:  # noqa: BLE001 - propagate failures to the waiting coroutine
            instance.hass.loop.call_soon_threadsafe(self._finish, None, err)
        else:
            instance.hass.loop.call_soon_threadsafe(self._finish, result, None)

    def _finish(self, result, error) -> None:
        if self.future.done():
            return
        if error is not None:
            self.future.set_exception(error)
        else:
            self.future.set_result(result)


async def async_move_recovery(hass, entry_id, entity_id, record) -> bool:
    """Wait for an atomic, replay-safe correction on the Recorder queue."""
    return await async_recorder_job(hass, _move, entry_id, entity_id, record)


async def async_recorder_job(hass, job, *args):
    """Run a read/write operation on Recorder, including its transactional marker."""
    future = hass.loop.create_future()
    get_instance(hass).queue_task(_RecorderJob(job, args, future))
    return await asyncio.wait_for(future, timeout=30)
