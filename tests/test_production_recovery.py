"""Exercise residual allocation, persistence, configuration and Recorder replay."""

import asyncio
import json
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from custom_components.plenitude_panels.api import PlenitudeApi, PlenitudeAuthError
from custom_components.plenitude_panels.config_flow import _entry_data, _schema
from custom_components.plenitude_panels.recovery import (
    ProductionRecovery,
    aggregate_intervals,
    day_bounds,
    number,
    reconstruct,
)
from custom_components.plenitude_panels.recovery_history import read_history
from custom_components.plenitude_panels.recovery_statistics import _move
from custom_components.plenitude_panels.sensor import (
    PlenitudePanelSensor,
    PlenitudeTotalProductionSensor,
)
from homeassistant.components.recorder.db_schema import (
    SCHEMA_VERSION,
    Base,
    StateAttributes,
    States,
    StatesMeta,
    Statistics,
    StatisticsMeta,
    StatisticsShortTerm,
)
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.table_managers.statistics_meta import (
    StatisticsMetaManager,
)
from homeassistant.util import dt as dt_util
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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
    return {
        "measureUnit": "Wh",
        "energyProducedConsumed": [
            {
                "energyProduced": "1,71",
                "timeUnit": datetime.fromtimestamp(stamp, ROME).isoformat() if aware else
                datetime.fromtimestamp(stamp, ROME).replace(tzinfo=None).isoformat(),
            }
            for stamp in range(start, end, 900)
        ],
    }


def test_weighted_residual_three_panels():
    result = reconstruct(samples({"a": 100 / 3, "b": 0, "c": 0}),
                         {"a": 400, "b": 400, "c": 600}, {QUARTER: 350})
    assert result["b"][QUARTER] == pytest.approx(100)
    assert result["c"][QUARTER] == pytest.approx(150)
    assert "a" not in result


def test_all_missing_and_equal_fallback():
    result = reconstruct(samples({"a": None, "b": 0, "c": 0}),
                         {"a": 400, "b": None, "c": 600}, {QUARTER: 300})
    assert [result[panel][QUARTER] for panel in ("a", "b", "c")] == [100, 100, 100]


def test_only_one_zero_and_no_negative_corrections():
    live = samples({"a": 10, "b": 0})
    assert reconstruct(live, {"a": 400, "b": 600}, {QUARTER: 90}) == {"b": {QUARTER: 60}}
    assert reconstruct(live, {"a": 400, "b": 600}, {QUARTER: 20}) == {}


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


def test_any_positive_reading_excludes_panel_from_recovery():
    live = samples({"a": 10, "b": 0})
    live[str((QUARTER + 900) // 300)] = {"a": 0, "b": 1}
    assert reconstruct(live, {"a": 400, "b": 600}, {QUARTER: 90}) == {}


def test_units_local_time_and_italian_numbers():
    result = aggregate_intervals(payload(), DAY, ROME)
    assert len(result) == 96
    assert result[START] == 1.71
    kwh = payload()
    kwh["measureUnit"] = "kWh"
    assert aggregate_intervals(kwh, DAY, ROME)[START] == 1710
    assert number("1.234,56") == 1234.56
    for invalid in (None, "nan", "inf", -1, "-0,1"):
        assert number(invalid) is None


@pytest.mark.parametrize("day,count", [(date(2026, 3, 29), 92), (date(2026, 10, 25), 100)])
def test_offset_aware_dst_days(day, count):
    assert len(aggregate_intervals(payload(day, aware=True), day, ROME)) == count


def test_ambiguous_and_incomplete_aggregate_defers():
    with pytest.raises(ValueError, match="ambiguous"):
        aggregate_intervals(payload(date(2026, 10, 25)), date(2026, 10, 25), ROME)
    incomplete = payload()
    incomplete["energyProducedConsumed"].pop()
    with pytest.raises(ValueError, match="incomplete"):
        aggregate_intervals(incomplete, DAY, ROME)
    duplicate = payload()
    duplicate["energyProducedConsumed"][-1] = duplicate["energyProducedConsumed"][0]
    with pytest.raises(ValueError, match="duplicate"):
        aggregate_intervals(duplicate, DAY, ROME)


def test_configuration_defaults_and_reauthentication_preserve_flag():
    data = {"cookie": "cookie", "account_id": "account", "supply_id": "supply"}
    validated = _schema()(data)
    assert validated["recovery_enabled"] is True
    assert validated["pod"] == ""
    assert _entry_data({**data, "pod": " it012345 ", "recovery_enabled": False})["pod"] == "IT012345"
    assert _entry_data({**data, "pod": "POD", "recovery_enabled": False})["recovery_enabled"] is False


def test_aggregate_request_retries_auth_and_uses_pod():
    async def run():
        api = PlenitudeApi(None, "cookie", "account", "supply")
        api._json_request = AsyncMock(side_effect=[PlenitudeAuthError(), payload()])
        api.async_initialize_session = AsyncMock()
        result = await api.async_fetch_daily_production("IT012345", DAY)
        assert result["measureUnit"] == "Wh"
        api.async_initialize_session.assert_awaited_once()
        request = api._json_request.call_args
        assert "/accounts/account/pod/IT012345/productions-consumes" in request.args[1]
        assert request.kwargs["params"]["start-date"] == "2026-10-31T00:00:00.000Z"
        assert request.kwargs["params"]["end-date"] == "2026-10-31T00:00:00.000Z"
    asyncio.run(run())


def test_persistence_replay_and_disabled_switch():
    async def run():
        saved = {}

        class MemoryStore:
            def __init__(self, *args):
                pass

            async def async_load(self):
                return json.loads(json.dumps(saved)) if saved else None

            async def async_save(self, value):
                saved.clear()
                saved.update(json.loads(json.dumps(value)))

        entry = SimpleNamespace(entry_id="entry", data={
            "account_id": "account", "supply_id": "supply", "pod": "POD",
        })
        aggregate = payload()
        for row in aggregate["energyProducedConsumed"]:
            row["energyProduced"] = "0,00"
        aggregate["energyProducedConsumed"][48]["energyProduced"] = "90,00"
        api = SimpleNamespace(async_fetch_daily_production=AsyncMock(return_value=aggregate))
        with patch("custom_components.plenitude_panels.recovery.Store", MemoryStore), patch(
            "custom_components.plenitude_panels.recovery.dt_util.get_default_time_zone", return_value=ROME,
        ):
            recovery = ProductionRecovery(None, entry, api)
            recovery.state["days"][DAY.isoformat()] = {
                "samples": samples({"a": 10, "b": 0}), "powers": {"a": 400, "b": 600}, "done": False,
            }
            now = datetime(2026, 11, 1, 12, tzinfo=UTC)
            panels = [{"identifier": "a", "power": 400, "instantProductionWh": 120},
                      {"identifier": "b", "power": 600, "instantProductionWh": 0}]
            await recovery.async_sample(panels, int(now.timestamp() // 300), now)
            assert recovery.total("b") == 60
            restored = ProductionRecovery(None, entry, api)
            await restored.async_load()
            await restored.async_sample(panels, int(now.timestamp() // 300), now)
            assert restored.total("b") == 60
            assert len(restored.state["pending"]) == 1
            api.async_fetch_daily_production.assert_awaited_once()
            entry.data["recovery_enabled"] = False
            api.async_fetch_daily_production.reset_mock()
            restored._last_attempt = None
            await restored.async_sample(panels, int(now.timestamp() // 300), now)
            api.async_fetch_daily_production.assert_not_awaited()
            assert restored.status == "disabled"
            assert restored.total("b") == 60
            # Midnight's last-five-minute sample belongs to yesterday.
            midnight = datetime(2026, 11, 2, tzinfo=ROME)
            await restored.async_sample(panels, int(midnight.timestamp() // 300), midnight)
            last_window = str(int(midnight.timestamp() // 300) - 1)
            assert last_window in restored.state["days"]["2026-11-01"]["samples"]
            assert "2026-11-02" not in restored.state["days"]
            # A configured default-on flag still waits for a POD.
            entry.data.pop("recovery_enabled")
            entry.data.pop("pod")
            restored._last_attempt = None
            await restored.async_sample(panels, int(midnight.timestamp() // 300), midnight)
            assert restored.status == "pod_required"
            api.async_fetch_daily_production.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("stored,unit", [("100", "Wh"), ("0.1", "kWh"), ("0.0001", "MWh")])
def test_total_sensor_restore_does_not_double_count_recovery(stored, unit):
    async def run():
        recovery = SimpleNamespace(total=lambda panel: 90, entities={})
        coordinator = SimpleNamespace(
            data={"a": {"instantProductionWh": 120}}, sample_id=100, recovery=recovery,
        )
        entry = SimpleNamespace(unique_id="account_supply", entry_id="entry")
        sensor = PlenitudeTotalProductionSensor(coordinator, entry, "a")
        sensor.entity_id = "sensor.panel_total"
        sensor.async_get_last_state = AsyncMock(return_value=SimpleNamespace(
            state=stored, attributes={"recovered_energy_wh": 60, "last_sample_id": 99, "unit_of_measurement": unit},
        ))
        with patch.object(PlenitudePanelSensor, "async_added_to_hass", AsyncMock()):
            await sensor.async_added_to_hass()
        assert sensor.native_value == 140  # 40 live + 10 new live + 90 recovered.
        sensor._add_current_sample()
        assert sensor.native_value == 140
        assert recovery.entities["a"] == "sensor.panel_total"
    asyncio.run(run())


@pytest.fixture
def recorder():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    instance = SimpleNamespace(
        get_session=factory, thread_id=threading.get_ident(), schema_version=SCHEMA_VERSION,
    )
    instance.hass = type("RecorderHass", (), {})()
    instance.hass.data = {"recorder_instance": instance}
    instance.hass.states = SimpleNamespace(get=lambda entity_id: None)
    instance.database_engine = SimpleNamespace(optimizer=SimpleNamespace(slow_dependent_subquery=False))
    instance.statistics_meta_manager = StatisticsMetaManager(instance)
    with factory.begin() as session:
        metadata = {
            "statistic_id": "sensor.panel_total", "source": "recorder", "name": None,
            "unit_of_measurement": "Wh", "has_sum": True, "mean_type": StatisticMeanType.NONE,
        }
        if hasattr(StatisticsMeta, "unit_class"):
            metadata["unit_class"] = "energy"
        _, metadata_id = instance.statistics_meta_manager.update_or_add(session, metadata, {})
        session.add(StatesMeta(metadata_id=1, entity_id="sensor.panel_total"))
        session.add(StateAttributes(attributes_id=1, shared_attrs=json.dumps({"recovered_energy_wh": 90})))
        session.add(States(metadata_id=1, attributes_id=1, state="100", last_updated_ts=END + 3601))
        for table, duration in ((StatisticsShortTerm, 300), (Statistics, 3600)):
            for stamp in range(START, END + 7200, duration):
                session.add(table.from_stats(metadata_id, {
                    "start": datetime.fromtimestamp(stamp, UTC), "state": 100,
                    "sum": 100 if stamp >= END + 3600 else 10,
                }, 1))
    yield instance
    engine.dispose()


def test_statistics_moved_to_original_day_and_replay_is_idempotent(recorder):
    record = {
        "panel": "a", "day": DAY.isoformat(), "created": END + 3600,
        "target": 90, "intervals": {str(QUARTER): 90},
    }
    assert _move(recorder, "entry", "sensor.panel_total", record)
    with recorder.get_session() as session:
        hourly = {row.start_ts: row.sum for row in session.query(Statistics).filter_by(metadata_id=1)}
        short = {row.start_ts: row.sum for row in session.query(StatisticsShortTerm).filter_by(metadata_id=1)}
        assert hourly[QUARTER - 3600] == 10
        assert hourly[QUARTER] == 100
        assert hourly[END] == 100
        assert hourly[END + 3600] == 100  # No spike on the recovery day.
        assert short[QUARTER] == 40
        assert short[QUARTER + 300] == 70
        assert short[QUARTER + 600] == 100
        assert short[END + 3600] == 100
    assert _move(recorder, "entry", "sensor.panel_total", record)
    with recorder.get_session() as session:
        assert session.query(StatisticsShortTerm).filter_by(metadata_id=1, start_ts=QUARTER).one().sum == 40


def test_statistics_wait_for_actual_recording(recorder):
    record = {"panel": "a", "day": DAY.isoformat(), "created": END + 3600,
              "target": 180, "intervals": {str(QUARTER): 90}}
    assert not _move(recorder, "entry", "sensor.panel_total", record)
    with recorder.get_session() as session:
        assert session.query(StatisticsShortTerm).filter_by(metadata_id=1, start_ts=QUARTER).one().sum == 10


def test_statistics_wait_for_compilation(recorder):
    with recorder.get_session() as session:
        session.query(StatisticsShortTerm).filter(StatisticsShortTerm.start_ts >= END + 3600).delete()
        session.commit()
    record = {"panel": "a", "day": DAY.isoformat(), "created": END + 3600,
              "target": 90, "intervals": {str(QUARTER): 90}}
    assert not _move(recorder, "entry", "sensor.panel_total", record)


def test_statistics_transaction_rolls_back_before_retry(recorder):
    record = {"panel": "a", "day": DAY.isoformat(), "created": END + 3600,
              "target": 90, "intervals": {str(QUARTER): 90}}
    with (
        patch.object(recorder.statistics_meta_manager, "update_or_add", side_effect=RuntimeError("failure")),
        pytest.raises(RuntimeError, match="failure"),
    ):
        _move(recorder, "entry", "sensor.panel_total", record)
    with recorder.get_session() as session:
        assert session.query(StatisticsShortTerm).filter_by(metadata_id=1, start_ts=QUARTER).one().sum == 10
    assert _move(recorder, "entry", "sensor.panel_total", record)
    with recorder.get_session() as session:
        assert session.query(StatisticsShortTerm).filter_by(metadata_id=1, start_ts=QUARTER).one().sum == 40


def test_statistics_convert_wh_to_recorder_kwh(recorder):
    with recorder.get_session() as session:
        session.query(StatisticsMeta).filter_by(id=1).update({"unit_of_measurement": "kWh"})
        session.commit()
    recorder.statistics_meta_manager.reset()
    record = {"panel": "a", "day": DAY.isoformat(), "created": END + 3600,
              "target": 90, "intervals": {str(QUARTER): 90}}
    assert _move(recorder, "entry", "sensor.panel_total", record)
    with recorder.get_session() as session:
        assert session.query(StatisticsShortTerm).filter_by(metadata_id=1, start_ts=QUARTER).one().sum == pytest.approx(10.03)


def test_manifest_and_translation_consistency():
    component = Path(__file__).resolve().parents[1] / "custom_components" / "plenitude_panels"
    manifest = json.loads((component / "manifest.json").read_text(encoding="utf-8"))
    newest = next(line[3:] for line in (component / "CHANGELOG.md").read_text(encoding="utf-8").splitlines() if line.startswith("## "))
    assert manifest["version"] == newest
    english = json.loads((component / "strings.json").read_text(encoding="utf-8"))
    assert english == json.loads((component / "translations" / "en.json").read_text(encoding="utf-8"))
    italian = json.loads((component / "translations" / "it.json").read_text(encoding="utf-8"))
    for section, step in (("config", "user"), ("options", "settings")):
        assert set(english[section]["step"][step]["data"]) == set(italian[section]["step"][step]["data"])
    assert set(english["options"]["step"]) == set(italian["options"]["step"])
    assert set(english["options"]["error"]) == set(italian["options"]["error"])


def test_incremental_same_day_statistics_and_old_retry(recorder):
    first = {"panel": "a", "day": DAY.isoformat(), "created": END + 3600,
             "target": 90, "intervals": {str(QUARTER): 90}}
    assert _move(recorder, "entry", "sensor.panel_total", first)
    with recorder.get_session() as session:
        session.add(StateAttributes(attributes_id=2, shared_attrs=json.dumps({"recovered_energy_wh": 150})))
        session.add(States(metadata_id=1, attributes_id=2, state="160", last_updated_ts=END + 3901))
        session.query(StatisticsShortTerm).filter(
            StatisticsShortTerm.start_ts >= END + 3900,
        ).update({StatisticsShortTerm.sum: StatisticsShortTerm.sum + 60})
        session.query(Statistics).filter(
            Statistics.metadata_id == 1, Statistics.start_ts >= END + 3600,
        ).update({Statistics.sum: Statistics.sum + 60})
        session.commit()
    second = {**first, "created": END + 3900, "target": 150, "intervals": {str(QUARTER): 60}}
    assert _move(recorder, "entry", "sensor.panel_total", second)
    assert _move(recorder, "entry", "sensor.panel_total", second)
    assert _move(recorder, "entry", "sensor.panel_total", first)
    with recorder.get_session() as session:
        assert session.query(Statistics).filter_by(metadata_id=1, start_ts=QUARTER).one().sum == 160
        assert session.query(Statistics).filter_by(metadata_id=1, start_ts=END).one().sum == 160
        assert session.query(Statistics).filter_by(metadata_id=1, start_ts=END + 3600).one().sum == 160
        assert session.query(StatisticsShortTerm).filter_by(metadata_id=1, start_ts=QUARTER).one().sum == 60


def test_statistics_create_missing_original_day_and_baseline(recorder):
    with recorder.get_session() as session:
        for table in (Statistics, StatisticsShortTerm):
            session.query(table).filter(table.start_ts < END).delete()
        session.commit()
    record = {"panel": "a", "day": DAY.isoformat(), "created": END + 3600,
              "target": 90, "intervals": {str(QUARTER): 90}}
    assert _move(recorder, "entry", "sensor.panel_total", record)
    with recorder.get_session() as session:
        assert session.query(Statistics).filter_by(metadata_id=1, start_ts=START - 3600).one().sum == 10
        assert session.query(Statistics).filter_by(metadata_id=1, start_ts=QUARTER - 3600).one().sum == 10
        assert session.query(Statistics).filter_by(metadata_id=1, start_ts=QUARTER).one().sum == 100
        assert session.query(Statistics).filter_by(metadata_id=1, start_ts=END + 3600).one().sum == 100
        assert session.query(StatisticsShortTerm).filter_by(metadata_id=1, start_ts=QUARTER).one().sum == 40
    assert _move(recorder, "entry", "sensor.panel_total", record)


def test_recovery_already_in_initial_baseline_is_counted_once(recorder):
    with recorder.get_session() as session:
        for table in (Statistics, StatisticsShortTerm):
            session.query(table).filter(table.start_ts < END + 3600).delete()
            session.query(table).filter(table.metadata_id == 1).update({"sum": 0})
        session.commit()
    record = {"panel": "a", "day": DAY.isoformat(), "created": END + 3600,
              "target": 90, "intervals": {str(QUARTER): 90}}
    assert _move(recorder, "entry", "sensor.panel_total", record)
    assert _move(recorder, "entry", "sensor.panel_total", record)
    with recorder.get_session() as session:
        assert session.query(Statistics).filter_by(metadata_id=1, start_ts=QUARTER).one().sum == 90
        assert session.query(Statistics).filter_by(metadata_id=1, start_ts=END + 3600).one().sum == 90
        assert session.query(StatisticsShortTerm).filter_by(metadata_id=1, start_ts=END + 3600).one().sum == 90


def test_older_day_creates_hourly_but_not_purged_short_term_buckets(recorder):
    old_day = date(2026, 8, 31)
    old_start, _ = day_bounds(old_day, ROME)
    old_quarter = old_start + 12 * 3600
    record = {"panel": "a", "day": old_day.isoformat(), "created": END + 3600,
              "target": 90, "intervals": {str(old_quarter): 90}}
    assert _move(recorder, "entry", "sensor.panel_total", record)
    with recorder.get_session() as session:
        assert session.query(Statistics).filter_by(metadata_id=1, start_ts=old_quarter).one().sum == 100
        assert session.query(Statistics).filter_by(metadata_id=1, start_ts=END + 3600).one().sum == 100
        assert session.query(StatisticsShortTerm).filter(StatisticsShortTerm.start_ts < START).count() == 0


def test_multiple_pending_recoveries_in_initial_baseline(recorder):
    with recorder.get_session() as session:
        for table in (Statistics, StatisticsShortTerm):
            session.query(table).filter(table.start_ts < END + 3600).delete()
            session.query(table).filter(table.metadata_id == 1).update({"sum": 0})
        session.query(StateAttributes).filter_by(attributes_id=1).update({
            "shared_attrs": json.dumps({"recovered_energy_wh": 150}),
        })
        session.commit()
    first = {"panel": "a", "day": DAY.isoformat(), "created": END + 3600,
             "target": 90, "intervals": {str(QUARTER): 90}}
    second = {**first, "target": 150, "intervals": {str(QUARTER): 60}}
    assert _move(recorder, "entry", "sensor.panel_total", first)
    assert _move(recorder, "entry", "sensor.panel_total", second)
    with recorder.get_session() as session:
        assert session.query(Statistics).filter_by(metadata_id=1, start_ts=QUARTER).one().sum == 150
        assert session.query(Statistics).filter_by(metadata_id=1, start_ts=END + 3600).one().sum == 150
        assert session.query(StatisticsShortTerm).filter_by(metadata_id=1, start_ts=END + 3600).one().sum == 150


def test_recorder_history_uses_sampling_ids_units_and_recovery_offsets(recorder):
    base_id = START // 300
    values = [
        (0, "100", 90, base_id, "Wh"),
        (300, "102", 90, base_id + 1, "Wh"),
        (600, "162", 150, base_id + 2, "Wh"),
        (1200, "1", 150, base_id + 4, "kWh"),
        (1500, "1.001", 150, base_id + 5, "kWh"),
    ]
    with recorder.get_session() as session:
        for index, (offset, value, recovered, sample_id, unit) in enumerate(values, 10):
            session.add(StateAttributes(attributes_id=index, shared_attrs=json.dumps({
                "unit_of_measurement": unit, "last_sample_id": sample_id,
                "recovered_energy_wh": recovered, "power": 400,
            })))
            session.add(States(metadata_id=1, attributes_id=index, state=value, last_updated_ts=START + offset))
        session.commit()
    history = read_history(recorder, "entry", {"a": "sensor.panel_total"}, DAY, ROME)
    assert history["samples"][str(base_id)]["a"] == 2
    assert history["samples"][str(base_id + 1)]["a"] == 0  # Recovery is not original production.
    assert str(base_id + 2) not in history["samples"]
    assert str(base_id + 3) not in history["samples"]  # No interpolation over the gap.
    assert history["samples"][str(base_id + 4)]["a"] == pytest.approx(1)
    assert history["powers"]["a"] == 400


def test_complete_hourly_statistics_supply_daily_total_and_audits(recorder):
    record = {"panel": "a", "day": DAY.isoformat(), "created": END + 3600,
              "target": 90, "intervals": {str(QUARTER): 90}}
    assert _move(recorder, "entry", "sensor.panel_total", record)
    history = read_history(recorder, "entry", {"a": "sensor.panel_total"}, DAY, ROME)
    assert history["daily"]["a"] == 90
    assert history["audits"]["a"][0][1] == 90


def test_october_27_daytime_statistics_match_dashboard_despite_missing_hours(recorder):
    day = date(2026, 10, 27)
    start, _ = day_bounds(day, ROME)
    with recorder.get_session() as session:
        metadata = {
            "statistic_id": "sensor.murcia_total", "source": "recorder", "name": None,
            "unit_of_measurement": "kWh", "has_sum": True, "mean_type": StatisticMeanType.NONE,
        }
        if hasattr(StatisticsMeta, "unit_class"):
            metadata["unit_class"] = "energy"
        _, murcia_id = recorder.statistics_meta_manager.update_or_add(session, metadata, {})
        for stamp, cumulative in ((start - 5 * 3600, 17.25),
                                  (start + 9 * 3600, 17.65),
                                  (start + 12 * 3600, 19.75),
                                  (start + 15 * 3600, 20.78)):
            session.add(Statistics.from_stats(murcia_id, {
                "start": datetime.fromtimestamp(stamp, UTC), "sum": cumulative,
            }, 1))
            session.add(Statistics.from_stats(1, {
                "start": datetime.fromtimestamp(stamp, UTC), "sum": 10,
            }, 1))
        session.commit()
    history = read_history(recorder, "entry", {
        "assemini": "sensor.panel_total", "murcia": "sensor.murcia_total",
        "missing": "sensor.not_recorded",
    }, day, ROME)
    assert history["daily"]["assemini"] == 0  # A recorded zero, not missing data.
    assert history["daily"]["murcia"] == pytest.approx(3530)
    assert "missing" not in history["daily"]


def test_pending_spike_marks_only_affected_panel_uncertain(recorder):
    # The fixture records a recovery increase in the day after DAY.
    requested_day = date(2026, 11, 1)
    pending = {"panel": "a", "day": DAY.isoformat(), "created": END + 3600,
               "target": 90, "intervals": {str(QUARTER): 90}}
    history = read_history(recorder, "entry", {"a": "sensor.panel_total"}, requested_day, ROME,
                           pending=[pending])
    assert history["uncertain_panels"] == {"a"}
    assert _move(recorder, "entry", "sensor.panel_total", pending)
    corrected = read_history(recorder, "entry", {"a": "sensor.panel_total"}, requested_day, ROME,
                             pending=[pending])
    assert not corrected["uncertain_panels"]
