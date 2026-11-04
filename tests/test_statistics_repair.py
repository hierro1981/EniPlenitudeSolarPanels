"""Production-statistics repair against real Recorder models and SQLite."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.plenitude_panels import manual_repair
from custom_components.plenitude_panels.config_flow import PlenitudePanelsOptionsFlow
from custom_components.plenitude_panels.manual_recovery import RecoveryError
from custom_components.plenitude_panels.recovery import (
    aggregate_intervals,
    day_bounds,
    production,
)
from custom_components.plenitude_panels.recovery_statistics import _move
from custom_components.plenitude_panels.statistics_repair import (
    TABLES,
    apply_plan,
    repair_status,
    replacement_plan,
    snapshot,
    undo_plan,
)
from homeassistant.components.recorder.db_schema import (
    States,
    Statistics,
    StatisticsMeta,
    StatisticsShortTerm,
)
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import statistics_during_period
from test_production_recovery import DAY, END, QUARTER, ROME, START
from test_production_recovery import recorder as recorder_fixture

recorder = recorder_fixture


def analysis(instance, *, day=DAY, aggregate=None, pending=(), prior=None, entities=None):
    bounds = day_bounds(day, ROME)
    if aggregate is None:
        aggregate = {stamp: 90.0 if stamp == bounds[0] + 12 * 3600 else 0.0
                     for stamp in range(*bounds, 900)}
    entities = entities or {"a": "sensor.panel_total"}
    return {
        "day": day.isoformat(), "identity": ["account", "supply"], "pod": "POD",
        "panels": {panel: {"power": 400, "position": panel} for panel in entities},
        "aggregate": aggregate, "prior": prior or {},
        "snapshot": snapshot(instance, "entry", entities, bounds, pending),
    }


def table_values(instance, table):
    with instance.get_session() as session:
        return [(row.start_ts, row.sum, row.state) for row in session.query(table).filter_by(
            metadata_id=1,
        ).order_by(table.start_ts)]


def corrupt(instance, baseline=150420.0):
    with instance.get_session() as session:
        for table, duration in ((Statistics, 3600), (StatisticsShortTerm, 300)):
            session.add(table.from_stats(1, {
                "start": datetime.fromtimestamp(START - duration, UTC), "sum": baseline, "state": 100,
            }, 1))
            session.query(table).filter(table.start_ts < START + 17 * 3600, table.start_ts >= START).update({"sum": baseline})
        session.commit()


@pytest.fixture(autouse=True)
def local_zone():
    from homeassistant.util import dt as dt_util

    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ROME)
    yield
    dt_util.set_default_time_zone(previous)


def test_user_october_3_payload_and_negative_samples():
    payload = json.loads((Path(__file__).parent / "fixtures" / "2026-10-03.json").read_text(encoding="utf-8"))
    values = aggregate_intervals(payload, date(2026, 10, 3), ROME)
    assert len(values) == 96
    assert sum(values.values()) == pytest.approx(5993.56)
    payload["energyProducedConsumed"][0]["energyProduced"] = "-1.234,56"
    assert sum(aggregate_intervals(payload, date(2026, 10, 3), ROME).values()) == pytest.approx(5993.56)
    assert production("-1,00") == 0
    assert production("-inf") is None
    assert production(None) is None
    assert production("nan") is None


def test_negative_day_is_replaced_and_future_increments_and_raw_history_are_preserved(recorder):
    corrupt(recorder)
    before = table_values(recorder, Statistics)
    with recorder.get_session() as session:
        raw = [(row.state, row.last_updated_ts, row.attributes_id) for row in session.query(States)]
    inspected = analysis(recorder)
    assert inspected["snapshot"]["panels"]["a"]["hour"]["total"] == -150410
    plan = replacement_plan(inspected, ["a"])
    apply_plan(recorder, "entry", plan, 123)
    after = table_values(recorder, Statistics)
    sums = {stamp: value for stamp, value, _ in after}
    assert sums[END - 3600] - sums[START - 3600] == pytest.approx(90)
    old_future = [(stamp, value) for stamp, value, _ in before if stamp >= END]
    new_future = [(stamp, value) for stamp, value, _ in after if stamp >= END]
    assert [b[1] - a[1] for a, b in pairwise(old_future)] == pytest.approx(
        [b[1] - a[1] for a, b in pairwise(new_future)],
    )
    with recorder.get_session() as session:
        assert [(row.state, row.last_updated_ts, row.attributes_id) for row in session.query(States)] == raw
    assert repair_status(recorder, "entry") == 123
    assert replacement_plan(analysis(recorder), ["a"])["operations"] == []
    apply_plan(recorder, "entry", plan, 123)  # Same transaction token cannot apply twice.
    assert table_values(recorder, Statistics) == after


def test_undo_restores_original_rows_and_preserves_newly_appended_energy(recorder):
    corrupt(recorder)
    before = {name: table_values(recorder, table) for name, table in TABLES.items()}
    plan = replacement_plan(analysis(recorder), ["a"])
    apply_plan(recorder, "entry", plan, 123)
    with recorder.get_session() as session:
        for name, table in TABLES.items():
            duration = 3600 if name == "hour" else 300
            last = table_values(recorder, table)[-1]
            session.add(table.from_stats(1, {
                "start": datetime.fromtimestamp(last[0] + duration, UTC), "sum": last[1] + 5, "state": 105,
            }, 1))
        session.commit()
    undo_plan(recorder, "entry", {"token": 123, "plan": plan})
    for name, table in TABLES.items():
        after = table_values(recorder, table)
        assert after[:-1] == before[name]
        assert after[-1][1] == pytest.approx(before[name][-1][1] + 5)
    undo_plan(recorder, "entry", {"token": 123, "plan": plan})
    assert repair_status(recorder, "entry") is None


def test_stale_preview_and_undo_conflict_do_not_overwrite_changes(recorder):
    plan = replacement_plan(analysis(recorder), ["a"])
    with recorder.get_session() as session:
        session.query(Statistics).filter_by(metadata_id=1, start_ts=START).update({"sum": 12})
        session.commit()
    with pytest.raises(RecoveryError, match="preview_stale"):
        apply_plan(recorder, "entry", plan, 123)
    fresh = replacement_plan(analysis(recorder), ["a"])
    apply_plan(recorder, "entry", fresh, 123)
    with recorder.get_session() as session:
        session.query(Statistics).filter_by(metadata_id=1, start_ts=QUARTER).update({"sum": 999})
        session.commit()
    current = table_values(recorder, Statistics)
    with pytest.raises(RecoveryError, match="repair_undo_conflict"):
        undo_plan(recorder, "entry", {"token": 123, "plan": fresh})
    assert table_values(recorder, Statistics) == current


def test_failed_transaction_restores_every_modified_sum(recorder):
    corrupt(recorder)
    plan = replacement_plan(analysis(recorder), ["a"])
    before = table_values(recorder, Statistics)
    with (
        patch("custom_components.plenitude_panels.statistics_repair._set_marker", side_effect=RuntimeError("failure")),
        pytest.raises(RuntimeError, match="failure"),
    ):
        apply_plan(recorder, "entry", plan, 123)
    assert table_values(recorder, Statistics) == before


def test_compiled_pending_recovery_is_acknowledged_without_double_energy(recorder):
    corrupt(recorder)
    pending = {"panel": "a", "day": DAY.isoformat(), "created": END + 3600,
               "target": 90, "intervals": {str(QUARTER): 90}}
    plan = replacement_plan(analysis(recorder, pending=[pending], prior={"a": {"total": 90}}), ["a"])
    apply_plan(recorder, "entry", plan, 123)
    sums = {stamp: value for stamp, value, _ in table_values(recorder, Statistics)}
    assert sums[END - 3600] - sums[START - 3600] == pytest.approx(90)
    assert sums[END + 3600] - sums[END] == pytest.approx(0)
    assert _move(recorder, "entry", "sensor.panel_total", pending)  # Already audited: no second move.
    undo_plan(recorder, "entry", {"token": 123, "plan": plan})
    assert table_values(recorder, Statistics)[-1][1] == 100


def test_uncompiled_or_other_date_pending_recovery_is_not_guessed(recorder):
    pending = {"panel": "a", "day": DAY.isoformat(), "created": END + 3600,
               "target": 999, "intervals": {str(QUARTER): 90}}
    with pytest.raises(RecoveryError, match="repair_pending"):
        replacement_plan(analysis(recorder, pending=[pending]), ["a"])
    pending["day"] = "2026-10-30"
    with pytest.raises(RecoveryError, match="repair_pending"):
        replacement_plan(analysis(recorder, pending=[pending]), ["a"])


def test_new_recovery_rejects_existing_negative_statistics(recorder):
    corrupt(recorder)
    before = table_values(recorder, Statistics)
    record = {"panel": "a", "day": DAY.isoformat(), "created": END + 3600,
              "target": 90, "intervals": {str(QUARTER): 90}}
    with pytest.raises(ValueError, match="Repair statistics"):
        _move(recorder, "entry", "sensor.panel_total", record)
    assert table_values(recorder, Statistics) == before


def test_old_gap_uses_nearest_following_sum_not_distant_current_baseline(recorder):
    old_day = date(2026, 10, 3)
    old_start, old_end = day_bounds(old_day, ROME)
    with recorder.get_session() as session:
        for table in TABLES.values():
            session.query(table).filter(table.start_ts < END + 3600).update({"sum": 100000})
            session.query(table).filter(table.start_ts >= END + 3600).update({"sum": 100090})
        session.add(Statistics.from_stats(1, {"start": datetime.fromtimestamp(old_end, UTC), "sum": 5}, 1))
        session.commit()
    record = {"panel": "a", "day": old_day.isoformat(), "created": END + 3600,
              "target": 90, "intervals": {str(old_start + 12 * 3600): 90}}
    assert _move(recorder, "entry", "sensor.panel_total", record)
    sums = {stamp: value for stamp, value, _ in table_values(recorder, Statistics)}
    assert sums[old_start] == 5
    assert sums[old_end - 3600] == 95
    assert sums[old_end] == 95


def test_journal_reconciles_commit_after_restart_and_can_undo(recorder):
    async def run():
        corrupt(recorder)
        plan = replacement_plan(analysis(recorder), ["a"])
        state = {"allocations": {}, "pending": [], "totals": {"a": 1000}}
        recovery = SimpleNamespace(
            entry=SimpleNamespace(entry_id="entry", data={"account_id": "account", "supply_id": "supply", "pod": "POD"}),
            state=state, store=SimpleNamespace(async_save=AsyncMock()),
        )
        hass = SimpleNamespace(config=SimpleNamespace(components={"recorder"}))

        async def execute(hass, job, *args):
            return job(recorder, *args)

        with patch.object(manual_repair, "async_recorder_job", execute):
            await manual_repair.async_apply(hass, recovery, plan)
            backup = deepcopy(recovery.state["repair_backup"])
            backup["phase"] = "prepared"  # Simulate a crash after the DB commit.
            recovery.state["repair_backup"] = json.loads(json.dumps(backup))
            await manual_repair.async_reconcile_journal(hass, recovery)
            assert recovery.state["repair_backup"]["phase"] == "applied"
            assert recovery.state["totals"] == {"a": 1000}
            await manual_repair.async_undo(hass, recovery)
            assert recovery.state["repair_backup"]["phase"] == "undone"
            assert recovery.state["totals"] == {"a": 1000}
    asyncio.run(run())


def test_backup_write_failure_does_not_touch_database(recorder):
    async def run():
        plan = replacement_plan(analysis(recorder), ["a"])
        original = {"allocations": {}, "pending": [], "totals": {}}
        recovery = SimpleNamespace(
            state=deepcopy(original),
            entry=SimpleNamespace(entry_id="entry", data={"account_id": "account", "supply_id": "supply", "pod": "POD"}),
            store=SimpleNamespace(async_save=AsyncMock(side_effect=OSError("disk"))),
        )
        hass = SimpleNamespace(config=SimpleNamespace(components={"recorder"}))
        job = AsyncMock()
        with (
            patch.object(manual_repair, "async_recorder_job", job),
            pytest.raises(OSError),
        ):
            await manual_repair.async_apply(hass, recovery, plan)
        job.assert_not_awaited()
        assert recovery.state == original
    asyncio.run(run())


def test_october_3_repair_matches_api_and_energy_dashboard_across_purged_short_history(recorder):
    day = date(2026, 10, 3)
    start, end = day_bounds(day, ROME)
    payload = json.loads((Path(__file__).parent / "fixtures" / "2026-10-03.json").read_text(encoding="utf-8"))
    aggregate = aggregate_intervals(payload, day, ROME)
    with recorder.get_session() as session:
        session.add(Statistics.from_stats(1, {"start": datetime.fromtimestamp(start - 3600, UTC), "sum": 150420}, 1))
        for stamp in range(start, end, 3600):
            session.add(Statistics.from_stats(1, {
                "start": datetime.fromtimestamp(stamp, UTC), "sum": 150420 if stamp < start + 17 * 3600 else 10,
            }, 1))
        session.commit()
    plan = replacement_plan(analysis(recorder, day=day, aggregate=aggregate), ["a"])
    apply_plan(recorder, "entry", plan, 123)
    changes = statistics_during_period(
        recorder.hass, datetime.fromtimestamp(start, UTC), datetime.fromtimestamp(end, UTC) - timedelta(microseconds=1),
        {"sensor.panel_total"}, "day", {"energy": "Wh"}, {"change"},
    )
    assert sum(row["change"] for row in changes["sensor.panel_total"]) == pytest.approx(5993.56)
    with recorder.get_session() as session:
        assert not session.query(StatisticsShortTerm).filter(StatisticsShortTerm.start_ts < START).count()
    hourly = table_values(recorder, Statistics)
    short = table_values(recorder, StatisticsShortTerm)
    assert hourly[-1][1] == short[-1][1]


def add_panel(instance, entity, unit):
    factor = {"Wh": 1, "kWh": 1000}[unit]
    with instance.get_session() as session:
        meta = {
            "statistic_id": entity, "source": "recorder", "name": None,
            "unit_of_measurement": unit, "has_sum": True, "mean_type": StatisticMeanType.NONE,
        }
        if hasattr(StatisticsMeta, "unit_class"):
            meta["unit_class"] = "energy"
        _, metadata_id = instance.statistics_meta_manager.update_or_add(session, meta, {})
        for table in TABLES.values():
            for row in session.query(table).filter_by(metadata_id=1):
                session.add(table.from_stats(metadata_id, {
                    "start": datetime.fromtimestamp(row.start_ts, UTC),
                    "sum": row.sum / factor if row.sum is not None else None,
                    "state": row.state / factor if row.state is not None else None,
                }, 1))
        session.commit()
    return metadata_id


def test_multiple_panels_mixed_units_and_existing_recoveries_are_respected(recorder):
    add_panel(recorder, "sensor.b", "kWh")
    add_panel(recorder, "sensor.c", "Wh")
    data = analysis(recorder, entities={"a": "sensor.panel_total", "b": "sensor.b", "c": "sensor.c"},
                    prior={"a": {"total": 20}, "b": {"total": 10}})
    data["aggregate"] = {stamp: value * 10 for stamp, value in data["aggregate"].items()}
    data["panels"] = {"a": {"power": 400}, "b": {"power": 600}, "c": {"power": 1000}}
    plan = replacement_plan(data, ["a", "b", "c"])
    assert sum(plan["targets"].values()) == pytest.approx(900)
    assert plan["targets"]["a"] == pytest.approx(194)
    assert plan["targets"]["b"] == pytest.approx(271)
    assert plan["targets"]["c"] == pytest.approx(435)
    apply_plan(recorder, "entry", plan, 123)
    totals = statistics_during_period(
        recorder.hass, datetime.fromtimestamp(START, UTC), datetime.fromtimestamp(END, UTC) - timedelta(microseconds=1),
        {"sensor.panel_total", "sensor.b", "sensor.c"}, "day", {"energy": "Wh"}, {"change"},
    )
    assert sum(row["change"] for rows in totals.values() for row in rows) == pytest.approx(900)


def test_pending_initial_baseline_is_not_subtracted_as_if_it_were_counted(recorder):
    with recorder.get_session() as session:
        for table in TABLES.values():
            session.query(table).filter(table.start_ts < END + 3600).delete()
            session.query(table).update({"sum": 0})
        session.commit()
    pending = {"panel": "a", "day": DAY.isoformat(), "created": END + 3600,
               "target": 90, "intervals": {str(QUARTER): 90}}
    plan = replacement_plan(analysis(recorder, pending=[pending], prior={"a": {"total": 90}}), ["a"])
    apply_plan(recorder, "entry", plan, 123)
    sums = {stamp: value for stamp, value, _ in table_values(recorder, Statistics)}
    assert sums[END - 3600] == pytest.approx(90)
    assert sums[END + 3600] == pytest.approx(90)


def test_failed_new_repair_keeps_previous_undo_checkpoint(recorder):
    async def run():
        recovery = SimpleNamespace(
            entry=SimpleNamespace(entry_id="entry", data={"account_id": "account", "supply_id": "supply", "pod": "POD"}),
            state={"allocations": {}, "pending": [], "totals": {}}, store=SimpleNamespace(async_save=AsyncMock()),
        )
        hass = SimpleNamespace(config=SimpleNamespace(components={"recorder"}))

        async def execute(hass, job, *args):
            return job(recorder, *args)

        with patch.object(manual_repair, "async_recorder_job", execute):
            await manual_repair.async_apply(hass, recovery, replacement_plan(analysis(recorder), ["a"]))
            first = deepcopy(recovery.state["repair_backup"])
            assert not await manual_repair.async_apply(hass, recovery, replacement_plan(analysis(recorder), ["a"]))
            assert recovery.state["repair_backup"] == first
            next_data = analysis(recorder)
            next_data["aggregate"][QUARTER] = 100
            next_plan = replacement_plan(next_data, ["a"])
            with patch.object(manual_repair, "apply_plan", side_effect=RuntimeError("failure")), pytest.raises(RuntimeError):
                await manual_repair.async_apply(hass, recovery, next_plan)
            assert recovery.state["repair_backup"] == first
    asyncio.run(run())


def test_unselected_panel_change_invalidates_preview(recorder):
    second = add_panel(recorder, "sensor.b", "Wh")
    data = analysis(recorder, entities={"a": "sensor.panel_total", "b": "sensor.b"})
    data["aggregate"][QUARTER] = 200
    plan = replacement_plan(data, ["a"])
    before = table_values(recorder, Statistics)
    with recorder.get_session() as session:
        session.query(Statistics).filter_by(metadata_id=second, start_ts=QUARTER).update({"sum": 11})
        session.commit()
    with pytest.raises(RecoveryError, match="preview_stale"):
        apply_plan(recorder, "entry", plan, 123)
    assert table_values(recorder, Statistics) == before


def test_repair_wizard_preview_confirmation_and_undo(recorder):
    async def run():
        corrupt(recorder)
        inspected = analysis(recorder)
        inspected["suggested_repair"] = ["a"]
        inspected["negative_api_samples"] = 0
        backup = {"token": 123, "day": DAY.isoformat()}
        coordinator = SimpleNamespace(
            recovery=SimpleNamespace(state={"repair_backup": {"phase": "applied"}}),
            async_analyze_statistics=AsyncMock(return_value=inspected),
            async_repair_statistics=AsyncMock(),
            async_repair_backup=AsyncMock(return_value=backup),
            async_undo_statistics=AsyncMock(),
        )
        flow = PlenitudePanelsOptionsFlow(SimpleNamespace(entry_id="entry", data={}))
        flow.hass = SimpleNamespace(
            data={"plenitude_panels": {"entry": coordinator}}, config=SimpleNamespace(language="en"),
            async_add_executor_job=AsyncMock(side_effect=lambda func, *args: func(*args)),
        )
        menu = await flow.async_step_init()
        assert "repair_date" in menu["menu_options"] and "repair_undo" in menu["menu_options"]
        targets = await flow.async_step_repair_date({"date": DAY.isoformat()})
        assert targets["step_id"] == "repair_targets"
        assert targets["data_schema"]({})["panels"] == ["a"]
        preview = await flow.async_step_repair_targets({"panels": ["a"]})
        assert preview["step_id"] == "repair_confirm"
        assert "90.000" in preview["description_placeholders"]["panels"]
        coordinator.async_repair_statistics.assert_not_awaited()
        denied = await flow.async_step_repair_confirm({"confirm": False})
        assert denied["errors"]["base"] == "confirmation_required"
        edited = await flow.async_step_repair_confirm({"edit_allocation": True})
        assert edited["step_id"] == "repair_targets"
        await flow.async_step_repair_targets({"panels": ["a"]})
        done = await flow.async_step_repair_confirm({"confirm": True})
        assert done["reason"] == "repair_completed"
        coordinator.async_repair_statistics.assert_awaited_once()
        undo = await flow.async_step_repair_undo()
        assert undo["description_placeholders"]["date"] == DAY.isoformat()
        denied = await flow.async_step_repair_undo({"confirm": False})
        assert denied["errors"]["base"] == "confirmation_required"
        coordinator.async_undo_statistics.assert_not_awaited()
        done = await flow.async_step_repair_undo({"confirm": True})
        assert done["reason"] == "repair_undone"
        coordinator.async_undo_statistics.assert_awaited_once_with(123)
    asyncio.run(run())
