"""Restart continuity, including Recorder compilation after historical repair."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder.db_schema import StateAttributes, States, Statistics, StatisticsShortTerm
from homeassistant.components.recorder.statistics import (
    _compile_hourly_statistics,
    cache_latest_short_term_statistic_id_for_metadata_id,
    get_short_term_statistics_run_cache,
)
from homeassistant.components.sensor import recorder as sensor_recorder
from homeassistant.core import State

from custom_components.plenitude_panels import manual_repair
from custom_components.plenitude_panels.sensor import PlenitudePanelSensor, PlenitudeTotalProductionSensor
from custom_components.plenitude_panels.statistics_repair import TABLES, replacement_plan
from custom_components.plenitude_panels.total_restore import ProductionCheckpoint, last_recorded_total
from test_production_recovery import END
from test_statistics_repair import analysis, corrupt, local_zone, recorder, table_values


def compile_sample(instance, value, stamp):
    """Run HA's real sum/reset calculation, reading previous sums from SQLite."""
    start = datetime.fromtimestamp(stamp, UTC)
    state = State("sensor.panel_total", str(value), {
        "unit_of_measurement": "Wh", "device_class": "energy", "state_class": "total_increasing",
    }, last_updated=start, last_changed=start)
    args = [instance.hass, None, start, start + timedelta(minutes=5)]
    if "custom_units_for_entities" in inspect.signature(sensor_recorder.compile_statistics).parameters:
        args.append({})
    with (
        instance.get_session() as session,
        patch.object(sensor_recorder, "_get_sensor_states", return_value=[state]),
        patch.object(sensor_recorder.history, "get_full_significant_states_with_session",
                     return_value={state.entity_id: [state]}),
    ):
        args[1] = session
        result = sensor_recorder.compile_statistics(*args)
    return result.platform_stats[0]["stat"]


@pytest.mark.parametrize("before,after,expected", [(105000, 54000, 54000), (24500, 23000, -1500)])
def test_recorder_explains_positive_spike_and_negative_day(recorder, before, after, expected):
    with recorder.get_session() as session:
        session.query(StatisticsShortTerm).filter_by(metadata_id=1).update({"state": before})
        session.commit()
    previous = table_values(recorder, StatisticsShortTerm)[-1][1]
    compiled = compile_sample(recorder, after, END + 7200)
    assert compiled["sum"] - previous == pytest.approx(expected)


@pytest.mark.parametrize("before,recovered", [(105000, 54000), (24500, 23000)])
def test_repair_then_restart_with_unavailable_state_preserves_total(recorder, before, recovered):
    async def run():
        sample = (END + 7200) // 300 - 1
        attrs = {"unit_of_measurement": "Wh", "recovered_energy_wh": recovered, "last_sample_id": sample}
        corrupt(recorder)
        with recorder.get_session() as session:
            for table in TABLES.values():
                session.query(table).filter_by(metadata_id=1).update({"state": before})
            session.query(States).update({"state": str(before)})
            session.query(StateAttributes).update({"shared_attrs": json.dumps(attrs)})
            session.commit()
        recovery = SimpleNamespace(
            entry=SimpleNamespace(entry_id="entry", unique_id="account_supply", data={
                "account_id": "account", "supply_id": "supply", "pod": "POD",
            }),
            state={"allocations": {}, "pending": [], "totals": {"a": recovered}, "last_days": {}},
            store=SimpleNamespace(async_save=AsyncMock()), entities={}, status="monitoring",
        )
        hass = SimpleNamespace(config=SimpleNamespace(components={"recorder"}))

        async def execute(hass, job, *args):
            return job(recorder, *args)

        with patch.object(manual_repair, "async_recorder_job", execute):
            await manual_repair.async_apply(hass, recovery, replacement_plan(analysis(recorder), ["a"]))
            # Restart: deserialize the committed journal before restoring entities.
            recovery.state = json.loads(json.dumps(recovery.state))
            recovery.state["repair_backup"]["phase"] = "prepared"
            await manual_repair.async_reconcile_journal(hass, recovery)
        recovery.total = lambda panel: recovery.state["totals"].get(panel, 0)
        coordinator = SimpleNamespace(
            data={"a": {"instantProductionWh": 0}}, sample_id=sample + 1, recovery=recovery,
        )
        sensor = PlenitudeTotalProductionSensor(coordinator, recovery.entry, "a")
        sensor.hass = hass
        sensor.entity_id = "sensor.panel_total"
        sensor.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state="unavailable", attributes=attrs))
        sensor.async_get_last_extra_data = AsyncMock(return_value=None)
        with (
            patch.object(PlenitudePanelSensor, "async_added_to_hass", AsyncMock()),
            patch("custom_components.plenitude_panels.recovery_statistics.async_recorder_job", execute),
        ):
            await sensor.async_added_to_hass()
        # Exercise the real compiler before asserting continuity: the legacy code
        # turns these two restore losses into +54 kWh and -1.5 kWh respectively.
        previous = table_values(recorder, StatisticsShortTerm)[-1][1]
        compiled = compile_sample(recorder, sensor.native_value, END + 7200)
        assert compiled["sum"] - previous == pytest.approx(0)
        assert sensor.native_value == before
        assert recovery.state["totals"] == {"a": recovered}
        # Finish a real hour after the repair/restart, adding only 5 Wh of live
        # production. Persist successive five-minute sums just like Recorder.
        for offset in range(12):
            stamp = END + 7200 + offset * 300
            coordinator.sample_id = stamp // 300
            coordinator.data["a"]["instantProductionWh"] = 60 if offset == 1 else 0
            sensor._add_current_sample()
            compiled = compile_sample(recorder, sensor.native_value, stamp)
            assert compiled["sum"] - previous == pytest.approx(5 if offset else 0)
            with recorder.get_session() as session:
                session.add(StatisticsShortTerm.from_stats(1, compiled, 1))
                session.flush()
                cache_latest_short_term_statistic_id_for_metadata_id(
                    get_short_term_statistics_run_cache(recorder.hass), session, 1,
                )
                if offset == 11:
                    _compile_hourly_statistics(session, datetime.fromtimestamp(stamp, UTC))
                session.commit()
        assert table_values(recorder, Statistics)[-1][1] - previous == pytest.approx(5)
        # Undo changes normalized sums back, never the live/recovery checkpoint.
        saved = sensor.extra_restore_state_data
        with patch.object(manual_repair, "async_recorder_job", execute):
            await manual_repair.async_undo(hass, recovery)
        reloaded = restore_sensor(
            SimpleNamespace(state="unavailable", attributes={}), saved,
            recovered=recovered, sample=coordinator.sample_id,
        )
        with patch.object(PlenitudePanelSensor, "async_added_to_hass", AsyncMock()):
            await reloaded.async_added_to_hass()
        assert reloaded.native_value == before + 5
        previous = table_values(recorder, StatisticsShortTerm)[-1][1]
        compiled = compile_sample(recorder, reloaded.native_value, END + 10800)
        assert compiled["sum"] - previous == pytest.approx(0)
    asyncio.run(run())


def restore_sensor(last_state=None, extra=None, recovered=90, value=0, sample=100, hass=None):
    recovery = SimpleNamespace(
        total=lambda panel: recovered, entities={}, state={"last_days": {}, "pending": []},
        status="monitoring",
    )
    coordinator = SimpleNamespace(data={"a": {"instantProductionWh": value}}, sample_id=sample, recovery=recovery)
    entry = SimpleNamespace(unique_id="account_supply", entry_id="entry")
    sensor = PlenitudeTotalProductionSensor(coordinator, entry, "a")
    sensor.hass = hass
    sensor.entity_id = "sensor.panel_total"
    sensor.async_get_last_state = AsyncMock(return_value=last_state)
    sensor.async_get_last_extra_data = AsyncMock(return_value=extra)
    return sensor


def test_native_checkpoint_survives_unavailable_display_and_repeated_restarts():
    async def run():
        checkpoint = ProductionCheckpoint(100.125, "Wh", 40.125, 60, 99)
        # Extra data is rehydrated as a generic dict by HA, not our dataclass.
        extra = SimpleNamespace(as_dict=lambda: json.loads(json.dumps(checkpoint.as_dict())))
        for displayed in ("unavailable", "unknown", "0.100"):
            last = SimpleNamespace(state=displayed, attributes={"unit_of_measurement": "kWh"})
            sensor = restore_sensor(last, extra, value=120)  # New recovery: 60 -> 90; new live: 10 Wh.
            with patch.object(PlenitudePanelSensor, "async_added_to_hass", AsyncMock()):
                await sensor.async_added_to_hass()
            assert sensor.native_value == pytest.approx(140.125)
            assert sensor.extra_state_attributes["live_energy_wh"] == pytest.approx(50.125)
            saved = sensor.extra_restore_state_data.as_dict()
            reloaded = restore_sensor(last, SimpleNamespace(as_dict=lambda: deepcopy(saved)), value=120)
            with patch.object(PlenitudePanelSensor, "async_added_to_hass", AsyncMock()):
                await reloaded.async_added_to_hass()
            assert reloaded.native_value == pytest.approx(140.125)  # Same positive sample is not counted twice.
    asyncio.run(run())


@pytest.mark.parametrize("last", ["unavailable", "unknown", "bad"])
def test_missing_legacy_baseline_does_not_publish_recovered_only_counter(last):
    async def run():
        sensor = restore_sensor(SimpleNamespace(state=last, attributes={}), value=120)
        with patch.object(PlenitudePanelSensor, "async_added_to_hass", AsyncMock()):
            await sensor.async_added_to_hass()
        assert not sensor.available
        assert sensor.native_value is None
        assert sensor.extra_restore_state_data is None
        sensor._add_current_sample()
        assert sensor._total_wh == 0
    asyncio.run(run())


def test_new_panel_starts_normally_without_a_previous_state():
    async def run():
        sensor = restore_sensor(recovered=0, value=120)
        with patch.object(PlenitudePanelSensor, "async_added_to_hass", AsyncMock()):
            await sensor.async_added_to_hass()
        assert sensor.native_value == 10
        assert sensor.extra_restore_state_data.live_energy_wh == 10
    asyncio.run(run())


def test_checkpoint_with_smaller_recovery_ledger_cannot_lower_published_total():
    async def run():
        sensor = restore_sensor(extra=ProductionCheckpoint(100, "Wh", 10, 90, 99), recovered=60)
        with patch.object(PlenitudePanelSensor, "async_added_to_hass", AsyncMock()):
            await sensor.async_added_to_hass()
        assert sensor.native_value is None
        assert not sensor.available
    asyncio.run(run())


def test_legacy_recorder_fallback_uses_latest_valid_raw_state_and_its_units(recorder):
    with recorder.get_session() as session:
        session.add(StateAttributes(attributes_id=2, shared_attrs=json.dumps({
            "unit_of_measurement": "kWh", "recovered_energy_wh": 90, "last_sample_id": 99,
        })))
        for offset, state in [(4000, "0.15"), (5000, "unavailable"), (6000, "nan")]:
            session.add(States(metadata_id=1, attributes_id=2, state=state, last_updated_ts=END + offset))
        session.query(StatisticsShortTerm).update({"sum": 999999})
        session.commit()
    assert last_recorded_total(recorder, "sensor.panel_total") == (60, 90, 99)


def test_recorder_failure_does_not_initialize_a_fake_zero_counter():
    async def run():
        hass = SimpleNamespace(config=SimpleNamespace(components={"recorder"}))
        sensor = restore_sensor(hass=hass)
        with (
            patch.object(PlenitudePanelSensor, "async_added_to_hass", AsyncMock()),
            patch("custom_components.plenitude_panels.recovery_statistics.async_recorder_job",
                  AsyncMock(side_effect=TimeoutError)),
        ):
            await sensor.async_added_to_hass()
        assert sensor.native_value is None
        assert sensor.extra_restore_state_data is None
    asyncio.run(run())


def test_native_checkpoint_works_without_generic_state():
    async def run():
        sensor = restore_sensor(extra=ProductionCheckpoint(100, "Wh", 10, 90, 100), value=120)
        with patch.object(PlenitudePanelSensor, "async_added_to_hass", AsyncMock()):
            await sensor.async_added_to_hass()
        assert sensor.native_value == 100  # Sample 100 was already counted.
    asyncio.run(run())
