"""Repair preview, public imports/deltas, durable retry and conservative undo."""

import asyncio
import inspect
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder.db_schema import Statistics, StatisticsShortTerm, States
from homeassistant.util import dt as dt_util
from custom_components.plenitude_panels import manual_repair
from custom_components.plenitude_panels.config_flow import PlenitudePanelsOptionsFlow
from custom_components.plenitude_panels.manual_recovery import RecoveryError
from custom_components.plenitude_panels.recovery import aggregate_intervals, day_bounds
from custom_components.plenitude_panels.recovery_statistics import async_execute_journal
from custom_components.plenitude_panels.statistics_repair import replacement_plan, snapshot
from test_production_recovery import (
    DAY, END, QUARTER, ROME, START, owner, record, values,
)
from test_production_recovery import recorder as recorder_fixture

recorder = recorder_fixture
TABLES = {"hour": Statistics, "short": StatisticsShortTerm}


@pytest.fixture(autouse=True)
def local_zone():
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ROME)
    yield
    dt_util.set_default_time_zone(previous)


def analysis(instance, *, day=DAY, aggregate=None, pending=(), prior=None, entities=None):
    bounds = day_bounds(day, ROME)
    return {
        "day": day.isoformat(), "identity": ["account", "supply"], "pod": "POD",
        "panels": {p: {"power": 400, "position": p} for p in (entities or {"a": ""})},
        "aggregate": aggregate if aggregate is not None else {
            stamp: 90. if stamp == bounds[0] + 12 * 3600 else 0. for stamp in range(*bounds, 900)},
        "prior": prior or {},
        "snapshot": snapshot(instance, "entry", entities or {"a": "sensor.panel_total"}, bounds, pending),
    }


def table_values(instance, table):
    with instance.get_session() as session:
        return [(r.start_ts, r.sum, r.state) for r in session.query(table).filter_by(metadata_id=1).order_by(table.start_ts)]


def corrupt(instance, baseline=150420.):
    with instance.get_session() as session:
        for table in TABLES.values():
            session.query(table).filter(table.start_ts < START + 17 * 3600).update({"sum": baseline})
        session.commit()


def test_negative_api_samples_and_real_october_payload():
    data = json.loads((Path(__file__).parent / "fixtures" / "2026-10-03.json").read_text())
    from datetime import date
    assert sum(aggregate_intervals(data, date(2026, 10, 3), ROME).values()) == pytest.approx(5993.56)
    data["energyProducedConsumed"][0]["energyProduced"] = "-1.234,56"
    assert sum(aggregate_intervals(data, date(2026, 10, 3), ROME).values()) == pytest.approx(5993.56)


def test_repair_and_undo_preserve_raw_states_future_increments_and_next_compile(recorder):
    async def run():
        corrupt(recorder)
        recovery = owner(recorder)
        original = {name: table_values(recorder, table) for name, table in TABLES.items()}
        with recorder.get_session() as session:
            raw = [(r.state, r.last_updated_ts) for r in session.query(States)]
        plan = replacement_plan(analysis(recorder), ["a"])
        await manual_repair.async_apply(recorder.hass, recovery, plan)
        sums = values(recorder)
        assert sums[END - 3600] - sums[START - 3600] == pytest.approx(90)
        assert len(recorder.adjustments) == 1
        # No rewriting of historical short buckets; only public future offset.
        short = values(recorder, StatisticsShortTerm)
        assert short[QUARTER] == dict((s, v) for s, v, _ in original["short"])[QUARTER]
        with recorder.get_session() as session:
            assert [(r.state, r.last_updated_ts) for r in session.query(States)] == raw
        from test_total_restore import compile_sample
        compiled = compile_sample(recorder, 105, END + 7200)
        assert compiled["sum"] == pytest.approx(short[END + 6900] + 5)
        await manual_repair.async_undo(recorder.hass, recovery)
        assert {name: table_values(recorder, table) for name, table in TABLES.items()} == original
    asyncio.run(run())


@pytest.mark.parametrize("crash", ["after_delta", "after_import"])
def test_crash_between_public_commit_and_store_checkpoint_never_repeats_delta(recorder, crash):
    async def run():
        corrupt(recorder)
        recovery = owner(recorder)
        plan = replacement_plan(analysis(recorder), ["a"])
        checkpoint = {}
        async def save(state):
            checkpoint.clear()
            checkpoint.update(deepcopy(state))
            journal = state.get("repair_backup", {}).get("plan", {}).get("journal", {})
            done = sum(s["phase"] == "done" for s in journal.get("steps", []))
            if done == (1 if crash == "after_delta" else 2):
                # Model process death: persisted journal still says prepared for
                # the just-committed call.
                checkpoint["repair_backup"]["plan"]["journal"]["steps"][done - 1]["phase"] = (
                    "submitted" if crash == "after_delta" else "prepared"
                )
                raise OSError("crash")
        recovery.store.async_save.side_effect = save
        with pytest.raises(OSError):
            await manual_repair.async_apply(recorder.hass, recovery, plan)
        recovery.state = json.loads(json.dumps(checkpoint))
        recovery.store.async_save.side_effect = None
        await manual_repair.async_reconcile_journal(recorder.hass, recovery)
        assert len(recorder.adjustments) == 1
        assert recovery.state["repair_backup"]["phase"] == "applied"
        assert values(recorder)[END - 3600] == pytest.approx(150510)
    asyncio.run(run())


def test_queued_delta_timeout_then_retry_drains_before_witness_check(recorder):
    async def run():
        corrupt(recorder)
        recovery = owner(recorder)
        plan = replacement_plan(analysis(recorder), ["a"])
        queue = []
        real_adjust = recorder.async_adjust_statistics
        recorder.async_adjust_statistics = lambda *args: queue.append(args)
        barriers = 0
        async def drain():
            nonlocal barriers
            barriers += 1
            if queue:
                # First post-submit barrier times out, without cancelling task.
                if barriers == 3:
                    raise TimeoutError
                real_adjust(*queue.pop())
        recorder.async_block_till_done.side_effect = drain
        with pytest.raises(TimeoutError):
            await manual_repair.async_apply(recorder.hass, recovery, plan)
        await manual_repair.async_reconcile_journal(recorder.hass, recovery)
        assert len(recorder.adjustments) == 1
        assert not queue
    asyncio.run(run())


def test_missing_witness_or_external_changes_block_retry(recorder):
    async def run():
        corrupt(recorder)
        recovery = owner(recorder)
        plan = replacement_plan(analysis(recorder), ["a"])
        step = plan["journal"]["steps"][0]
        with recorder.get_session() as session:
            session.query(StatisticsShortTerm).filter_by(start_ts=step["witnesses"][1]["start"]).update({"sum": 12345})
            session.commit()
        with pytest.raises(RecoveryError, match="repair_undo_conflict"):
            await async_execute_journal(recorder.hass, recovery, plan["journal"])
        assert not recorder.adjustments
    asyncio.run(run())


def test_crash_after_intent_before_submission_does_not_guess_or_resubmit(recorder):
    async def run():
        corrupt(recorder)
        recovery = owner(recorder)
        journal = replacement_plan(analysis(recorder), ["a"])["journal"]
        # This durable state is indistinguishable from a still-running delta.
        journal["steps"][0]["phase"] = "submitted"
        with pytest.raises(RecoveryError, match="repair_undo_conflict"):
            await async_execute_journal(recorder.hass, recovery, journal)
        assert not recorder.adjustments
    asyncio.run(run())


def test_purged_witness_blocks_ambiguous_delta_without_repetition(recorder):
    async def run():
        corrupt(recorder)
        recovery = owner(recorder)
        journal = replacement_plan(analysis(recorder), ["a"])["journal"]
        step = journal["steps"][0]
        step["phase"] = "submitted"
        with recorder.get_session() as session:
            session.query(StatisticsShortTerm).filter_by(start_ts=step["witnesses"][1]["start"]).delete()
            session.commit()
        with pytest.raises(RecoveryError, match="repair_undo_conflict"):
            await async_execute_journal(recorder.hass, recovery, journal)
        assert not recorder.adjustments
    asyncio.run(run())


def test_misaligned_future_short_and_hour_endpoints_are_not_silently_repaired(recorder):
    corrupt(recorder)
    with recorder.get_session() as session:
        session.query(StatisticsShortTerm).filter_by(start_ts=END + 6900).update({"sum": 101})
        session.commit()
    with pytest.raises(RecoveryError, match="repair_no_alignment"):
        replacement_plan(analysis(recorder), ["a"])


@pytest.mark.parametrize("unit,factor", [("kWh", .001), ("MWh", .000001)])
def test_repair_and_undo_use_stored_energy_units_without_scaling_twice(recorder, unit, factor):
    async def run():
        corrupt(recorder)
        with recorder.get_session() as session:
            for table in TABLES.values():
                for row in session.query(table):
                    row.sum *= factor
                    row.state *= factor
            update = recorder.statistics_meta_manager.update_unit_of_measurement
            args = [session, "sensor.panel_total"]
            if "new_unit_class" in inspect.signature(update).parameters:
                args.append("energy")
            update(*args, unit)
            session.commit()
        recovery = owner(recorder)
        before = values(recorder)
        await manual_repair.async_apply(recorder.hass, recovery, replacement_plan(analysis(recorder), ["a"]))
        after = values(recorder)
        assert (after[END - 3600] - after[START - 3600]) / factor == pytest.approx(90)
        assert recorder.adjustments[0][3] == unit
        await manual_repair.async_undo(recorder.hass, recovery)
        assert values(recorder) == pytest.approx(before)
    asyncio.run(run())


def test_appended_rows_are_adjusted_once_and_retained_on_undo(recorder):
    async def run():
        corrupt(recorder)
        recovery = owner(recorder)
        plan = replacement_plan(analysis(recorder), ["a"])
        with recorder.get_session() as session:
            for table in TABLES.values():
                session.add(table.from_stats(1, {"start": datetime.fromtimestamp(END + 7200, UTC),
                                                "sum": 105, "state": 105}, 1))
            session.commit()
        await manual_repair.async_apply(recorder.hass, recovery, plan)
        offset = recorder.adjustments[0][2]
        assert values(recorder)[END + 7200] == pytest.approx(105 + offset)
        await manual_repair.async_undo(recorder.hass, recovery)
        assert values(recorder)[END + 7200] == pytest.approx(105)
    asyncio.run(run())


def test_missing_hour_can_be_imported_but_not_undone_by_deleting_a_bucket(recorder):
    async def run():
        corrupt(recorder)
        with recorder.get_session() as session:
            session.query(Statistics).filter_by(start_ts=QUARTER).delete()
            session.commit()
        recovery = owner(recorder)
        await manual_repair.async_apply(recorder.hass, recovery, replacement_plan(analysis(recorder), ["a"]))
        with pytest.raises(RecoveryError, match="repair_undo_conflict"):
            await manual_repair.async_undo(recorder.hass, recovery)
        assert QUARTER in values(recorder)
        assert recovery.state["repair_backup"]["phase"] == "applied"
    asyncio.run(run())


def test_stale_preview_is_rejected_before_journal_or_mutation(recorder):
    async def run():
        corrupt(recorder)
        plan = replacement_plan(analysis(recorder), ["a"])
        with recorder.get_session() as session:
            session.query(Statistics).filter_by(start_ts=QUARTER).update({"sum": 999})
            session.commit()
        recovery = owner(recorder)
        with pytest.raises(RecoveryError, match="preview_stale"):
            await manual_repair.async_apply(recorder.hass, recovery, plan)
        assert "repair_backup" not in recovery.state
        assert not recorder.adjustments
    asyncio.run(run())


def test_failed_initial_journal_save_does_not_submit_public_writes(recorder):
    async def run():
        corrupt(recorder)
        recovery = owner(recorder)
        recovery.store.async_save.side_effect = OSError("disk full")
        before = values(recorder)
        with pytest.raises(OSError):
            await manual_repair.async_apply(recorder.hass, recovery, replacement_plan(analysis(recorder), ["a"]))
        assert values(recorder) == before
        assert not recorder.adjustments
        assert "repair_backup" not in recovery.state
    asyncio.run(run())


def test_zero_aggregate_can_explicitly_repair_a_day_to_zero(recorder):
    async def run():
        corrupt(recorder)
        aggregate = {stamp: 0 for stamp in range(START, END, 900)}
        recovery = owner(recorder)
        await manual_repair.async_apply(
            recorder.hass, recovery, replacement_plan(analysis(recorder, aggregate=aggregate), ["a"]),
        )
        sums = values(recorder)
        assert sums[END - 3600] == sums[START - 3600]
    asyncio.run(run())


@pytest.mark.parametrize("day_text,hours", [("2026-03-29", 23), ("2026-10-25", 25)])
def test_repair_uses_local_dst_day_bounds_without_fixed_24_hour_assumptions(recorder, day_text, hours):
    from datetime import date

    async def run():
        day = date.fromisoformat(day_text)
        start, end = day_bounds(day, ROME)
        with recorder.get_session() as session:
            for table, duration in ((Statistics, 3600), (StatisticsShortTerm, 300)):
                session.query(table).delete()
                for stamp in range(start - duration, end + 7200, duration):
                    session.add(table.from_stats(1, {
                        "start": datetime.fromtimestamp(stamp, UTC), "state": 100,
                        "sum": 100 if stamp >= end else 10,
                    }, 1))
            session.commit()
        recovery = owner(recorder)
        plan = replacement_plan(analysis(recorder, day=day), ["a"])
        assert end - start == hours * 3600
        await manual_repair.async_apply(recorder.hass, recovery, plan)
        sums = values(recorder)
        assert sums[end - 3600] - sums[start - 3600] == pytest.approx(90)
    asyncio.run(run())


def test_pending_counted_recovery_is_removed_once_and_acknowledged(recorder):
    async def run():
        pending = record()
        recovery = owner(recorder, [pending])
        plan = replacement_plan(analysis(recorder, pending=[pending]), ["a"])
        await manual_repair.async_apply(recorder.hass, recovery, plan)
        assert not recovery.state["pending"]
        sums = values(recorder)
        assert sums[END - 3600] - sums[START - 3600] == pytest.approx(90)
        assert sums[END + 3600] == pytest.approx(100)
        assert len(recorder.adjustments) == 2
        await manual_repair.async_reconcile_journal(recorder.hass, recovery)
        assert len(recorder.adjustments) == 2
    asyncio.run(run())


def test_no_first_baseline_or_ambiguous_pending_is_not_guessed(recorder):
    with recorder.get_session() as session:
        session.query(Statistics).filter(Statistics.start_ts < START).delete()
        session.commit()
    with pytest.raises(RecoveryError, match="repair_no_statistics"):
        replacement_plan(analysis(recorder), ["a"])


def test_repair_wizard_requires_preview_and_explicit_confirmation(recorder):
    async def run():
        corrupt(recorder)
        inspected = analysis(recorder)
        inspected.update(suggested_repair=["a"], negative_api_samples=0)
        coordinator = SimpleNamespace(
            recovery=SimpleNamespace(state={}),
            async_analyze_statistics=AsyncMock(return_value=inspected),
            async_repair_statistics=AsyncMock(),
        )
        flow = PlenitudePanelsOptionsFlow(SimpleNamespace(entry_id="entry", data={}))
        flow.hass = SimpleNamespace(
            data={"plenitude_panels": {"entry": coordinator}}, config=SimpleNamespace(language="en"),
            async_add_executor_job=AsyncMock(side_effect=lambda func, *args: func(*args)),
        )
        await flow.async_step_repair_date({"date": DAY.isoformat()})
        preview = await flow.async_step_repair_targets({"panels": ["a"]})
        assert preview["step_id"] == "repair_confirm"
        coordinator.async_repair_statistics.assert_not_awaited()
        denied = await flow.async_step_repair_confirm({"confirm": False})
        assert denied["errors"]["base"] == "confirmation_required"
        result = await flow.async_step_repair_confirm({"confirm": True})
        assert result["reason"] == "repair_completed"
        coordinator.async_repair_statistics.assert_awaited_once()
    asyncio.run(run())
