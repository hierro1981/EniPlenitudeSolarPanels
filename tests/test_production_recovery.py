"""Allocation and hourly recovery using real HA public Recorder operations."""

import asyncio
import json
import threading
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from homeassistant.components.recorder.db_schema import (
    SCHEMA_VERSION, Base, StateAttributes, States, StatesMeta,
    Statistics, StatisticsMeta, StatisticsShortTerm,
)
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.table_managers.statistics_meta import StatisticsMetaManager
from homeassistant.components.recorder.table_managers.states_meta import StatesMetaManager
from homeassistant.components.recorder.statistics import import_statistics, adjust_statistics
from homeassistant.util import dt as dt_util

from custom_components.plenitude_panels.api import PlenitudeApi, PlenitudeAuthError
from custom_components.plenitude_panels.config_flow import _entry_data, _schema
from custom_components.plenitude_panels.recovery import (
    ProductionRecovery, aggregate_intervals, day_bounds, number, reconstruct,
)
from custom_components.plenitude_panels.recovery_statistics import (
    async_move_recovery, recovery_plan,
)

ROME = ZoneInfo("Europe/Rome")
DAY = date(2026, 10, 31)
START, END = day_bounds(DAY, ROME)
QUARTER = START + 12 * 3600


@pytest.fixture(autouse=True)
def local_timezone():
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ROME)
    yield
    dt_util.set_default_time_zone(previous)


def samples(values):
    return {str((QUARTER + offset) // 300): dict(values) for offset in (0, 300, 600)}


def payload(day=DAY, aware=False):
    start, end = day_bounds(day, ROME)
    return {"measureUnit": "Wh", "energyProducedConsumed": [
        {"energyProduced": "1,71", "timeUnit": datetime.fromtimestamp(stamp, ROME).isoformat()
         if aware else datetime.fromtimestamp(stamp, ROME).replace(tzinfo=None).isoformat()}
        for stamp in range(start, end, 900)]}


def test_weighted_residual_and_equal_fallback():
    result = reconstruct(samples({"a": 100 / 3, "b": 0, "c": 0}),
                         {"a": 400, "b": 400, "c": 600}, {QUARTER: 350})
    assert result == {"b": {QUARTER: 100}, "c": {QUARTER: 150}}
    result = reconstruct(samples({"a": None, "b": 0}), {"a": None, "b": 600}, {QUARTER: 300})
    assert result == {"a": {QUARTER: 150}, "b": {QUARTER: 150}}
    assert reconstruct(samples({"a": 10, "b": 0}), {"a": 400, "b": 600}, {QUARTER: 20}) == {}


@pytest.mark.parametrize("case", ["window", "healthy_value", "inventory"])
def test_incomplete_healthy_data_defers(case):
    live = samples({"a": 10, "b": 0})
    first = str(QUARTER // 300)
    if case == "window":
        del live[first]
    elif case == "healthy_value":
        live[first]["a"] = None
    else:
        del live[first]["b"]
    with pytest.raises(ValueError):
        reconstruct(live, {"a": 400, "b": 600}, {QUARTER: 90})


def test_positive_reading_excludes_panel():
    live = samples({"a": 10, "b": 0})
    live[str((QUARTER + 900) // 300)] = {"a": 0, "b": 1}
    assert reconstruct(live, {"a": 400, "b": 600}, {QUARTER: 90}) == {}


def test_units_numbers_and_configuration():
    assert aggregate_intervals(payload(), DAY, ROME)[START] == 1.71
    data = payload()
    data["measureUnit"] = "kWh"
    assert aggregate_intervals(data, DAY, ROME)[START] == 1710
    assert number("1.234,56") == 1234.56
    assert all(number(v) is None for v in (None, "nan", "inf", -1, "-0,1"))
    settings = {"cookie": "cookie", "account_id": "account", "supply_id": "supply"}
    assert _schema()(settings)["recovery_enabled"] is True
    assert _entry_data({**settings, "pod": " it012345 ", "recovery_enabled": False})["pod"] == "IT012345"


@pytest.mark.parametrize("day,count", [(date(2026, 3, 29), 92), (date(2026, 10, 25), 100)])
def test_dst_days(day, count):
    assert len(aggregate_intervals(payload(day, aware=True), day, ROME)) == count


def test_ambiguous_incomplete_duplicate_aggregate():
    with pytest.raises(ValueError, match="ambiguous"):
        aggregate_intervals(payload(date(2026, 10, 25)), date(2026, 10, 25), ROME)
    data = payload()
    data["energyProducedConsumed"].pop()
    with pytest.raises(ValueError, match="incomplete"):
        aggregate_intervals(data, DAY, ROME)
    data = payload()
    data["energyProducedConsumed"][-1] = data["energyProducedConsumed"][0]
    with pytest.raises(ValueError, match="duplicate"):
        aggregate_intervals(data, DAY, ROME)


def test_request_reauth_and_pod():
    async def run():
        api = PlenitudeApi(None, "cookie", "account", "supply")
        api._json_request = AsyncMock(side_effect=[PlenitudeAuthError(), payload()])
        api.async_initialize_session = AsyncMock()
        await api.async_fetch_daily_production("IT012345", DAY)
        api.async_initialize_session.assert_awaited_once()
        assert "/accounts/account/pod/IT012345/productions-consumes" in api._json_request.call_args.args[1]
    asyncio.run(run())


@pytest.fixture
def recorder():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    instance = SimpleNamespace(
        get_session=factory, thread_id=threading.get_ident(), schema_version=SCHEMA_VERSION,
        max_bind_vars=999,
    )
    instance.hass = type("RecorderHass", (), {})()
    instance.hass.data = {"recorder_instance": instance}
    instance.hass.states = SimpleNamespace(get=lambda entity_id: None)
    instance.hass.config = SimpleNamespace(components={"recorder"})
    instance.database_engine = SimpleNamespace(optimizer=SimpleNamespace(slow_dependent_subquery=False))
    instance.statistics_meta_manager = StatisticsMetaManager(instance)
    instance.states_meta_manager = StatesMetaManager(instance)
    instance.states_meta_manager.active = True
    instance.states_manager = SimpleNamespace(oldest_ts=START - 86400)
    instance.recorder_and_worker_thread_ids = {threading.get_ident()}
    instance.async_add_executor_job = AsyncMock(side_effect=lambda func, *args: func(*args))
    instance.async_block_till_done = AsyncMock()
    instance.async_import_statistics = lambda meta, rows, table: import_statistics(instance, meta, rows, table)
    instance.adjustments = []

    def adjust(entity, start, delta, unit):
        instance.adjustments.append((entity, start, delta, unit))
        adjust_statistics(instance, entity, start, delta, unit)

    instance.async_adjust_statistics = adjust
    with factory.begin() as session:
        meta = {"statistic_id": "sensor.panel_total", "source": "recorder", "name": None,
                "unit_of_measurement": "Wh", "has_sum": True, "mean_type": StatisticMeanType.NONE}
        if hasattr(StatisticsMeta, "unit_class"):
            meta["unit_class"] = "energy"
        _, metadata_id = instance.statistics_meta_manager.update_or_add(session, meta, {})
        session.add(StatesMeta(metadata_id=1, entity_id="sensor.panel_total"))
        for identifier, recovered in ((1, 0), (2, 90)):
            session.add(StateAttributes(attributes_id=identifier, shared_attrs=json.dumps({
                "recovered_energy_wh": recovered, "unit_of_measurement": "Wh",
            })))
        session.add(States(metadata_id=1, attributes_id=1, state="10", last_updated_ts=END + 3000,
                           last_changed_ts=END + 3000))
        session.add(States(metadata_id=1, attributes_id=2, state="100", last_updated_ts=END + 3601,
                           last_changed_ts=END + 3601))
        for table, duration in ((StatisticsShortTerm, 300), (Statistics, 3600)):
            for stamp in range(START - duration, END + 7200, duration):
                session.add(table.from_stats(metadata_id, {
                    "start": datetime.fromtimestamp(stamp, UTC),
                    "state": 100 if stamp >= END + 3600 else 10,
                    "sum": 100 if stamp >= END + 3600 else 10,
                }, 1))
    yield instance
    engine.dispose()


def values(instance, table=Statistics):
    with instance.get_session() as session:
        return {row.start_ts: row.sum for row in session.query(table).filter_by(metadata_id=1)}


def record():
    return {"panel": "a", "day": DAY.isoformat(), "created": END + 3600,
            "target": 90, "intervals": {str(QUARTER): 90}, "day_bounds": [START, END]}


def owner(instance, pending=()):
    return SimpleNamespace(
        entry=SimpleNamespace(entry_id="entry", data={"account_id": "account", "supply_id": "supply", "pod": "POD"}),
        state={"pending": list(pending), "allocations": {}, "totals": {}, "days": {}},
        store=SimpleNamespace(async_save=AsyncMock()),
    )


def test_hourly_recovery_preserves_short_and_final_sum_and_retries(recorder):
    async def run():
        pending = record()
        recovery = owner(recorder, [pending])
        short = values(recorder, StatisticsShortTerm)
        before = values(recorder)
        assert await async_move_recovery(recorder.hass, "entry", "sensor.panel_total", pending, recovery)
        after = values(recorder)
        assert after[QUARTER] == before[QUARTER] + 90
        assert after[END + 3600] == before[END + 3600]
        assert values(recorder, StatisticsShortTerm) == short
        assert await async_move_recovery(recorder.hass, "entry", "sensor.panel_total", pending, recovery)
        assert values(recorder) == after
        assert not recorder.adjustments
    asyncio.run(run())


def test_first_baseline_and_uncompiled_hour_defer(recorder):
    with recorder.get_session() as session:
        session.query(StatisticsShortTerm).filter(StatisticsShortTerm.start_ts < END + 3600).delete()
        session.commit()
    assert recovery_plan(recorder, "sensor.panel_total", record()) is None


def test_purged_pre_recovery_raw_state_cannot_turn_later_live_increase_into_recovery(recorder):
    with recorder.get_session() as session:
        session.query(States).filter_by(attributes_id=1).delete()
        session.commit()
    assert recovery_plan(recorder, "sensor.panel_total", record()) is None


def test_public_history_keeps_attribute_only_samples_and_never_fills_gaps(recorder):
    from custom_components.plenitude_panels.recovery_history import read_history

    with recorder.get_session() as session:
        for index, (stamp, value, sample) in enumerate([
            (QUARTER - 300, 0, QUARTER // 300),
            (QUARTER, 0, QUARTER // 300 + 1),
            (QUARTER + 300, 10, QUARTER // 300 + 2),
            (QUARTER + 900, 20, QUARTER // 300 + 4),
        ], 3):
            session.add(StateAttributes(attributes_id=index, shared_attrs=json.dumps({
                "unit_of_measurement": "Wh", "last_sample_id": sample,
                "recovered_energy_wh": 0, "power": 400,
            })))
            session.add(States(metadata_id=1, attributes_id=index, state=str(value),
                               last_updated_ts=stamp, last_changed_ts=QUARTER - 300))
        session.commit()
    history = read_history(recorder, "entry", {"a": "sensor.panel_total"}, DAY, ROME)
    assert history["samples"][str(QUARTER // 300)] == {"a": 0}
    assert history["samples"][str(QUARTER // 300 + 1)] == {"a": 10}
    assert str(QUARTER // 300 + 3) not in history["samples"]
    assert history["powers"] == {"a": 400}


def test_stored_statistic_units_ignore_frontend_display_conversion(recorder):
    from homeassistant.core import State
    from custom_components.plenitude_panels.recovery_statistics import read_series

    recorder.hass.states.get = lambda _: State("sensor.panel_total", ".1", {"unit_of_measurement": "kWh"})
    assert read_series(recorder, "sensor.panel_total", END + 3600)[0]["sum"] == 100


def test_runtime_modules_do_not_import_private_recorder_schema_or_tasks():
    from pathlib import Path

    root = Path(__file__).parents[1] / "custom_components" / "plenitude_panels"
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "recorder.db_schema" not in text, path
        assert "recorder.tasks" not in text, path
        assert "session.query(" not in text, path


def test_legacy_audit_read_only_prevents_recovery_replay(recorder):
    from hashlib import sha256

    async def run():
        pending = record()
        digest = sha256(b"entry:a").hexdigest()[:32]
        with recorder.get_session() as session:
            meta = {"statistic_id": f"plenitude_panels:recovery_audit_{digest}", "source": "plenitude_panels",
                    "name": None, "unit_of_measurement": "Wh", "has_sum": False, "mean_type": StatisticMeanType.NONE}
            if hasattr(StatisticsMeta, "unit_class"):
                meta["unit_class"] = "energy"
            _, identifier = recorder.statistics_meta_manager.update_or_add(session, meta, {})
            session.add(Statistics.from_stats(identifier, {
                "start": datetime.combine(DAY, datetime.min.time(), UTC), "state": 90,
            }, 1))
            session.commit()
        before = values(recorder)
        assert await async_move_recovery(recorder.hass, "entry", "sensor.panel_total", pending, owner(recorder, [pending]))
        assert values(recorder) == before
        assert "statistics_journal" not in pending
    asyncio.run(run())


def test_persistent_allocation_failure_rolls_back_and_disabled_recovery_keeps_total():
    async def run():
        with patch("custom_components.plenitude_panels.recovery.Store"):
            recovery = ProductionRecovery(None, SimpleNamespace(entry_id="entry", data={
                "account_id": "account", "supply_id": "supply", "recovery_enabled": False,
            }), None)
        recovery.store = SimpleNamespace(async_save=AsyncMock(side_effect=OSError))
        with pytest.raises(OSError):
            await recovery.async_allocate(DAY.isoformat(), {"a": {QUARTER: 90}}, datetime.now(UTC))
        assert recovery.total("a") == 0
        recovery.store.async_save.side_effect = None
        await recovery.async_allocate(DAY.isoformat(), {"a": {QUARTER: 90}}, datetime.now(UTC))
        assert recovery.total("a") == 90
        assert len(recovery.state["pending"]) == 1
    asyncio.run(run())


def test_live_five_minute_sampling_midnight_and_durable_recovery_replay():
    async def run():
        saved = {}

        class MemoryStore:
            def __init__(self, *_):
                pass

            async def async_load(self):
                return json.loads(json.dumps(saved)) if saved else None

            async def async_save(self, value):
                saved.clear()
                saved.update(json.loads(json.dumps(value)))

        entry = SimpleNamespace(entry_id="entry", data={
            "account_id": "account", "supply_id": "supply", "pod": "POD",
        })
        data = payload()
        for row in data["energyProducedConsumed"]:
            row["energyProduced"] = "0,00"
        data["energyProducedConsumed"][48]["energyProduced"] = "90,00"
        api = SimpleNamespace(async_fetch_daily_production=AsyncMock(return_value=data))
        with patch("custom_components.plenitude_panels.recovery.Store", MemoryStore):
            recovery = ProductionRecovery(None, entry, api)
            recovery.state["days"][DAY.isoformat()] = {
                "samples": samples({"a": 10, "b": 0}), "powers": {"a": 400, "b": 600}, "done": False,
            }
            now = datetime(2026, 11, 1, 12, tzinfo=UTC)
            panels = [{"identifier": "a", "power": 400, "instantProductionWh": 120},
                      {"identifier": "b", "power": 600, "instantProductionWh": 0}]
            await recovery.async_sample(panels, int(now.timestamp() // 300), now)
            restored = ProductionRecovery(None, entry, api)
            await restored.async_load()
            await restored.async_sample(panels, int(now.timestamp() // 300), now)
            assert restored.total("b") == 60
            assert len(restored.state["pending"]) == 1
            api.async_fetch_daily_production.assert_awaited_once()
            entry.data["recovery_enabled"] = False
            restored._last_attempt = None
            api.async_fetch_daily_production.reset_mock()
            midnight = datetime(2026, 11, 2, tzinfo=ROME)
            await restored.async_sample(panels, int(midnight.timestamp() // 300), midnight)
            assert str(int(midnight.timestamp() // 300) - 1) in restored.state["days"]["2026-11-01"]["samples"]
            assert "2026-11-02" not in restored.state["days"]
            api.async_fetch_daily_production.assert_not_awaited()
            assert restored.status == "disabled"
            assert restored.total("b") == 60
    asyncio.run(run())
