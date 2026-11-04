"""Manual recovery preview, confirmation, migration and race regression tests."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest
import voluptuous as vol
from custom_components.plenitude_panels.config_flow import PlenitudePanelsOptionsFlow
from custom_components.plenitude_panels.coordinator import PlenitudeCoordinator
from custom_components.plenitude_panels.manual_recovery import (
    RecoveryError,
    _migrate_audits,
    _recorded_panels,
    async_analyze,
    async_apply,
    automatic_plan,
    daily_plan,
)
from custom_components.plenitude_panels.recovery import ProductionRecovery, day_bounds

ROME = ZoneInfo("Europe/Rome")
DAY = date(2026, 10, 31)
START, END = day_bounds(DAY, ROME)
QUARTER = START + 12 * 3600
NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def wizard_clock(monkeypatch):
    monkeypatch.setattr("custom_components.plenitude_panels.config_flow.dt_util.now", lambda: NOW)


@pytest.fixture
def recovery():
    entry = SimpleNamespace(entry_id="entry", unique_id="account_supply", data={
        "account_id": "account", "supply_id": "supply", "pod": "POD", "recovery_enabled": False,
    })
    with patch("custom_components.plenitude_panels.recovery.Store") as store:
        store.return_value.async_save = AsyncMock()
        store.return_value.async_load = AsyncMock(return_value=None)
        return ProductionRecovery(None, entry, SimpleNamespace(async_fetch_daily_production=AsyncMock()))


def analysis(prior=None):
    return {
        "day": DAY.isoformat(), "pod": "POD", "identity": ["account", "supply"],
        "panels": {"a": {"power": 400}, "b": {"power": 600}},
        "powers": {"a": 400, "b": 600}, "known": None,
        "prior": prior or {}, "aggregate": {QUARTER: 100, QUARTER + 900: 200},
        "automatic": None,
    }


def aggregate_payload(day=DAY):
    start, end = day_bounds(day, ROME)
    return {"measureUnit": "Wh", "energyProducedConsumed": [
        {"timeUnit": datetime.fromtimestamp(stamp, ROME).replace(tzinfo=None).isoformat(),
         "energyProduced": "300,00" if stamp == start + 12 * 3600 else "0,00"}
        for stamp in range(start, end, 900)
    ]}


def test_daily_estimate_subtracts_original_and_previous_then_weights():
    prior = {"a": {"total": 50, "intervals": {str(QUARTER): 50}}}
    plan = daily_plan(analysis(prior), ["a", "b"], 100)
    assert sum(plan["allocation"]["a"].values()) == pytest.approx(60)
    assert sum(plan["allocation"]["b"].values()) == pytest.approx(90)
    assert plan["allocation"]["a"][str(QUARTER + 900)] == pytest.approx(40)
    assert plan["mode"] == "daily_estimate"


def test_daily_estimate_missing_power_equal_fallback():
    data = analysis()
    data["powers"]["b"] = None
    plan = daily_plan(data, ["a", "b"], 0)
    assert sum(plan["allocation"]["a"].values()) == 150
    assert sum(plan["allocation"]["b"].values()) == 150


@pytest.mark.parametrize("selected,known,error", [
    ([], 0, "invalid_panels"), (["unknown"], 0, "invalid_panels"),
    (["a", "a"], 0, "invalid_panels"), (["a"], -1, "invalid_energy"),
    (["a"], float("nan"), "invalid_energy"), (["a"], float("inf"), "invalid_energy"),
    (["a"], 301, "energy_exceeds_aggregate"),
])
def test_invalid_manual_inputs_are_rejected(selected, known, error):
    with pytest.raises(RecoveryError, match=error):
        daily_plan(analysis(), selected, known)


def test_automatic_plan_only_adds_interval_differences():
    data = analysis({"b": {"total": 40, "intervals": {str(QUARTER): 40}}})
    data["automatic"] = {"b": {QUARTER: 50, QUARTER + 900: 100}}
    plan = automatic_plan(data)
    assert plan["allocation"] == {"b": {str(QUARTER): 10, str(QUARTER + 900): 100}}
    data["automatic"]["b"][QUARTER] = 30
    with pytest.raises(RecoveryError, match="manual_data_required"):
        automatic_plan(data)


def test_manual_replay_is_zero_after_restoring_ledger(recovery):
    async def run():
        plan = daily_plan(analysis(), ["a", "b"], 0)
        await async_apply(recovery, plan)
        assert recovery.total("a") == 120
        assert recovery.total("b") == 180
        assert len(recovery.state["pending"]) == 2
        with pytest.raises(RecoveryError, match="preview_stale"):
            await async_apply(recovery, plan)
        stored = json.loads(json.dumps(recovery.state))
        recovery.store.async_load = AsyncMock(return_value=stored)
        await recovery.async_load()
        fresh = analysis(deepcopy(recovery.state["allocations"][DAY.isoformat()]))
        replay = daily_plan(fresh, ["a", "b"], 0)
        assert replay["allocation"] == {}
        await async_apply(recovery, replay)
        assert recovery.total("a") + recovery.total("b") == 300
    asyncio.run(run())


def test_failed_durable_save_restores_memory(recovery):
    async def run():
        before = deepcopy(recovery.state)
        recovery.store.async_save.side_effect = OSError("disk failure")
        with pytest.raises(OSError, match="disk failure"):
            await async_apply(recovery, daily_plan(analysis(), ["a"], 0))
        assert recovery.state == before
    asyncio.run(run())


def test_changed_supply_invalidates_preview(recovery):
    async def run():
        plan = daily_plan(analysis(), ["a"], 0)
        recovery.entry.data["supply_id"] = "different"
        with pytest.raises(RecoveryError, match="preview_stale"):
            await async_apply(recovery, plan)
        recovery.store.async_save.assert_not_awaited()
    asyncio.run(run())


def test_concurrent_confirmations_allocate_once(recovery):
    async def run():
        coordinator = object.__new__(PlenitudeCoordinator)
        coordinator._operation_lock = asyncio.Lock()
        coordinator.recovery = recovery
        coordinator.data = analysis()["panels"]
        coordinator.async_set_updated_data = Mock()
        recovery.async_correct_statistics = AsyncMock()
        plan = daily_plan(analysis(), ["a"], 0)
        results = await asyncio.gather(
            coordinator.async_apply_recovery(plan), coordinator.async_apply_recovery(plan),
            return_exceptions=True,
        )
        assert sum(isinstance(result, RecoveryError) for result in results) == 1
        assert recovery.total("a") == 300
        coordinator.async_set_updated_data.assert_called_once()
    asyncio.run(run())


def test_legacy_audits_reconstruct_daily_amounts(recovery):
    recovery.state["totals"]["a"] = 150
    marker = datetime(2026, 10, 31, tzinfo=UTC).timestamp()
    _migrate_audits(recovery, {"a": [(marker, 50), (marker + 86400, 150)]})
    assert recovery.state["allocations"][DAY.isoformat()]["a"]["total"] == 50
    assert recovery.state["allocations"]["2026-11-01"]["a"]["total"] == 100


def test_unknown_legacy_amount_is_not_silently_counted_twice(recovery):
    recovery.state["totals"].update({"a": 50, "b": 100})
    before = deepcopy(recovery.state["allocations"])
    with pytest.raises(RecoveryError, match="legacy_recovery_unknown"):
        _migrate_audits(recovery, {"a": [(START, 50)]})
    assert recovery.state["allocations"] == before


def test_pending_07_migrates_with_original_intervals(recovery):
    async def run():
        state = {"days": {}, "totals": {"a": 40}, "last_days": {"a": DAY.isoformat()}, "pending": [
            {"day": DAY.isoformat(), "panel": "a", "intervals": {str(QUARTER): 40},
             "target": 40, "created": NOW.timestamp()},
        ]}
        recovery.store.async_load = AsyncMock(return_value=state)
        await recovery.async_load()
        assert recovery.state["allocations"][DAY.isoformat()]["a"] == {
            "total": 40, "intervals": {str(QUARTER): 40},
        }
    asyncio.run(run())


def test_analyze_old_day_without_history_requests_manual_input(recovery):
    async def run():
        recovery.api.async_fetch_daily_production.return_value = aggregate_payload()
        hass = SimpleNamespace(config=SimpleNamespace(components=set()))
        with (
            patch("custom_components.plenitude_panels.manual_recovery.dt_util.now", return_value=NOW),
            patch("custom_components.plenitude_panels.manual_recovery.dt_util.get_default_time_zone", return_value=ROME),
        ):
            result = await async_analyze(hass, recovery, analysis()["panels"], DAY)
        assert result["automatic"] is None
        assert result["known"] is None  # Zero must be explicitly entered, never assumed.
        assert result["aggregate"][QUARTER] == 300
        assert not recovery.state["pending"]
    asyncio.run(run())


def test_analyze_local_complete_data_with_automatic_flag_disabled(recovery):
    async def run():
        recovery.api.async_fetch_daily_production.return_value = aggregate_payload()
        recovery.state["days"][DAY.isoformat()] = {
            "samples": {str((QUARTER + offset) // 300): {"a": 10, "b": 0} for offset in (0, 300, 600)},
            "powers": {"a": 400, "b": 600}, "done": False,
        }
        hass = SimpleNamespace(config=SimpleNamespace(components=set()))
        with (
            patch("custom_components.plenitude_panels.manual_recovery.dt_util.now", return_value=NOW),
            patch("custom_components.plenitude_panels.manual_recovery.dt_util.get_default_time_zone", return_value=ROME),
        ):
            result = await async_analyze(hass, recovery, analysis()["panels"], DAY)
        assert result["known"] == 30
        assert sum(automatic_plan(result)["allocation"]["b"].values()) == 270
        assert recovery.total("b") == 0  # Analysis is not application.
    asyncio.run(run())


def test_analyze_today_and_missing_pod_do_not_call_api(recovery):
    async def run():
        hass = SimpleNamespace(config=SimpleNamespace(components=set()))
        with patch("custom_components.plenitude_panels.manual_recovery.dt_util.now", return_value=NOW):
            with pytest.raises(RecoveryError, match="date_not_past"):
                await async_analyze(hass, recovery, analysis()["panels"], NOW.date())
            recovery.entry.data["pod"] = ""
            with pytest.raises(RecoveryError, match="pod_required"):
                await async_analyze(hass, recovery, analysis()["panels"], DAY)
        recovery.api.async_fetch_daily_production.assert_not_awaited()
    asyncio.run(run())


def test_recorder_supplements_missing_local_samples_and_subtracts_previous(recovery):
    async def run():
        recovery.api.async_fetch_daily_production.return_value = aggregate_payload()
        recovery.state["totals"]["b"] = 60
        recovery.state["allocations"][DAY.isoformat()] = {
            "b": {"total": 60, "intervals": {str(QUARTER): 60}},
        }
        recorded = {
            "samples": {str((QUARTER + offset) // 300): {"a": 10, "b": 0} for offset in (0, 300, 600)},
            "powers": {"a": 400, "b": 600}, "daily": {"a": 30, "b": 60}, "audits": {},
        }
        instance = SimpleNamespace(async_add_executor_job=AsyncMock(return_value=recorded))
        registry = SimpleNamespace(async_get_entity_id=Mock(side_effect=lambda *args: "sensor." + args[-1]))
        hass = SimpleNamespace(config=SimpleNamespace(components={"recorder"}))
        with (
            patch("custom_components.plenitude_panels.manual_recovery.dt_util.now", return_value=NOW),
            patch("custom_components.plenitude_panels.manual_recovery.dt_util.get_default_time_zone", return_value=ROME),
            patch("custom_components.plenitude_panels.manual_recovery.er.async_get", return_value=registry),
            patch("homeassistant.components.recorder.get_instance", return_value=instance),
        ):
            result = await async_analyze(hass, recovery, analysis()["panels"], DAY)
        assert result["known"] == 30  # The 60 recovered Wh is not original production.
        assert result["source"] == "local_and_recorder"
        assert sum(automatic_plan(result)["allocation"]["b"].values()) == 210
        assert recovery.total("b") == 60  # Analysis alone still does not allocate.
    asyncio.run(run())


def test_wizard_menu_manual_input_and_explicit_confirmation():
    async def run():
        coordinator = SimpleNamespace(
            async_analyze_recovery=AsyncMock(return_value=analysis()), async_apply_recovery=AsyncMock(),
        )
        flow = PlenitudePanelsOptionsFlow(SimpleNamespace(entry_id="entry", data={}))
        flow.hass = SimpleNamespace(
            data={"plenitude_panels": {"entry": coordinator}}, config=SimpleNamespace(language="en"),
            async_add_executor_job=AsyncMock(side_effect=lambda func, *args: func(*args)),
        )
        menu = await flow.async_step_init()
        assert menu["type"] == "menu"
        assert menu["menu_options"] == ["settings", "recovery_date", "repair_date"]
        form = await flow.async_step_recovery_date({"date": DAY.isoformat()})
        assert form["step_id"] == "recovery_targets"
        with pytest.raises(vol.Invalid):
            form["data_schema"]({"panels": ["a"]})  # Original daily energy is required.
        preview = await flow.async_step_recovery_targets({"panels": ["b"], "known_energy_wh": 100})
        assert preview["step_id"] == "recovery_confirm"
        assert preview["description_placeholders"]["additional"] == "200.000"
        coordinator.async_apply_recovery.assert_not_awaited()
        unconfirmed = await flow.async_step_recovery_confirm({"confirm": False})
        assert unconfirmed["errors"] == {"base": "confirmation_required"}
        coordinator.async_apply_recovery.assert_not_awaited()
        completed = await flow.async_step_recovery_confirm({"confirm": True})
        assert completed["type"] == "menu"
        assert completed["step_id"] == "recovery_done"
        assert completed["menu_options"] == ["recovery_previous", "recovery_next", "recovery_close"]
        assert flow._plan is None
        coordinator.async_apply_recovery.assert_awaited_once()
    asyncio.run(run())


def test_wizard_automatic_preview_can_be_edited_without_application():
    async def run():
        data = analysis()
        data["automatic"] = {"b": {QUARTER: 300}}
        coordinator = SimpleNamespace(
            async_analyze_recovery=AsyncMock(return_value=data), async_apply_recovery=AsyncMock(),
        )
        flow = PlenitudePanelsOptionsFlow(SimpleNamespace(entry_id="entry", data={}))
        flow.hass = SimpleNamespace(
            data={"plenitude_panels": {"entry": coordinator}}, config=SimpleNamespace(language="en"),
            async_add_executor_job=AsyncMock(side_effect=lambda func, *args: func(*args)),
        )
        preview = await flow.async_step_recovery_date({"date": DAY.isoformat()})
        assert preview["step_id"] == "recovery_confirm"
        edited = await flow.async_step_recovery_confirm({"edit_allocation": True, "confirm": True})
        assert edited["step_id"] == "recovery_targets"
        coordinator.async_apply_recovery.assert_not_awaited()
    asyncio.run(run())


def test_per_panel_statistics_do_not_collapse_when_one_panel_has_no_data(recovery):
    recorded = _recorded_panels(
        recovery, DAY.isoformat(), analysis()["panels"], {}, {QUARTER: 6973.75}, {},
        {"daily": {"b": 3530}, "audits": {}},
    )
    assert recorded["a"]["value"] is None
    assert recorded["a"]["observed"] is None
    assert recorded["b"]["value"] == 3530


def test_partial_readings_are_not_presented_as_a_complete_zero(recovery):
    recorded = _recorded_panels(
        recovery, DAY.isoformat(), analysis()["panels"],
        {str(QUARTER // 300): {"a": 0, "b": 10}}, {QUARTER: 6973.75}, {},
        {"daily": {}, "audits": {}},
    )
    assert recorded["a"] == {"value": None, "observed": 0, "source": "partial"}
    assert recorded["b"] == {"value": None, "observed": 10, "source": "partial"}


def test_only_recovery_confirmed_in_original_statistics_is_subtracted(recovery):
    prior = {"a": {"total": 90, "intervals": {str(QUARTER): 90}}}
    recovery.state["pending"] = [{
        "day": DAY.isoformat(), "panel": "a", "intervals": {str(QUARTER): 90},
        "target": 90, "created": NOW.timestamp(),
    }]
    uncorrected = _recorded_panels(
        recovery, DAY.isoformat(), analysis()["panels"], {}, {QUARTER: 6973.75}, prior,
        {"daily": {"a": 100, "b": 3530}, "audits": {}},
    )
    assert uncorrected["a"]["value"] == 100
    assert uncorrected["b"]["value"] == 3530
    marker = datetime.combine(DAY, datetime.min.time(), UTC).timestamp()
    corrected = _recorded_panels(
        recovery, DAY.isoformat(), analysis()["panels"], {}, {QUARTER: 6973.75}, prior,
        {"daily": {"a": 190, "b": 3530}, "audits": {"a": [(marker, 90)]}},
    )
    assert corrected["a"]["value"] == 100  # Same original value before/after the commit.


def test_detected_october_27_totals_prefill_and_suggest_zero_panel(recovery):
    async def run():
        day = date(2026, 10, 27)
        payload = aggregate_payload(day)
        payload["energyProducedConsumed"][48]["energyProduced"] = "6.973,750"
        recovery.api.async_fetch_daily_production.return_value = payload
        recorded = {"samples": {}, "powers": {}, "daily": {"a": 0, "b": 3530}, "audits": {}}
        instance = SimpleNamespace(async_add_executor_job=AsyncMock(return_value=recorded))
        registry = SimpleNamespace(async_get_entity_id=Mock(side_effect=lambda *args: "sensor." + args[-1]))
        hass = SimpleNamespace(config=SimpleNamespace(components={"recorder"}))
        with (
            patch("custom_components.plenitude_panels.manual_recovery.dt_util.now", return_value=NOW),
            patch("custom_components.plenitude_panels.manual_recovery.dt_util.get_default_time_zone", return_value=ROME),
            patch("custom_components.plenitude_panels.manual_recovery.er.async_get", return_value=registry),
            patch("homeassistant.components.recorder.get_instance", return_value=instance),
        ):
            result = await async_analyze(hass, recovery, analysis()["panels"], day)
        assert result["known"] == 3530
        assert result["recorded"]["b"]["value"] == 3530
        assert result["suggested_panels"] == ["a"]
        assert sum(daily_plan(result, ["a"], result["known"])["allocation"]["a"].values()) == pytest.approx(3443.75)
        flow = PlenitudePanelsOptionsFlow(SimpleNamespace(entry_id="entry", data={}))
        flow.hass = SimpleNamespace(
            config=SimpleNamespace(language="it"),
            async_add_executor_job=AsyncMock(side_effect=lambda func, *args: func(*args)),
        )
        flow._analysis = result
        form = await flow.async_step_recovery_targets()
        validated = form["data_schema"]({})
        assert validated == {"panels": ["a"], "known_energy_wh": 3530}
        assert form["description_placeholders"]["known"] == "3.530,000"
        assert form["description_placeholders"]["date"] == "2026-10-27"
        assert form["description_placeholders"]["remaining"] == "3.443,750"
        assert "| b | 3.530,000 |" in form["description_placeholders"]["panels"]
    asyncio.run(run())


@pytest.fixture
def navigation_flow():
    coordinator = SimpleNamespace(
        async_analyze_recovery=AsyncMock(side_effect=lambda day: {**analysis(), "day": day.isoformat()}),
        async_apply_recovery=AsyncMock(),
    )
    flow = PlenitudePanelsOptionsFlow(SimpleNamespace(entry_id="entry", data={}))
    flow.hass = SimpleNamespace(
        data={"plenitude_panels": {"entry": coordinator}}, config=SimpleNamespace(language="en"),
        async_add_executor_job=AsyncMock(side_effect=lambda func, *args: func(*args)),
    )
    return flow, coordinator


@pytest.mark.parametrize("direction,offset", [("previous", -1), ("next", 1)])
def test_completion_shortcut_starts_fresh_analysis_without_reapplying(navigation_flow, direction, offset):
    async def run():
        flow, coordinator = navigation_flow
        original_plan = daily_plan(analysis(), ["b"], 100)
        flow._plan = original_plan
        completed = await flow.async_step_recovery_confirm({"confirm": True})
        assert completed["step_id"] == "recovery_done"
        coordinator.async_apply_recovery.assert_awaited_once_with(original_plan)
        assert flow._plan is None
        assert flow._analysis is None
        result = await getattr(flow, f"async_step_recovery_{direction}")()
        target = DAY + timedelta(days=offset)
        coordinator.async_analyze_recovery.assert_awaited_once_with(target)
        assert result["step_id"] == "recovery_targets"
        assert flow._analysis["day"] == target.isoformat()
        assert flow._plan is None
        assert result["data_schema"]({"known_energy_wh": 0})["panels"] == []
        assert coordinator.async_apply_recovery.await_count == 1
    asyncio.run(run())


def test_yesterday_hides_next_and_rejects_direct_navigation_to_today(navigation_flow):
    async def run():
        flow, coordinator = navigation_flow
        flow._completed_day = NOW.date() - timedelta(days=1)
        menu = await flow.async_step_recovery_done()
        assert menu["menu_options"] == ["recovery_previous", "recovery_close"]
        invalid = await flow.async_step_recovery_next()
        assert invalid["step_id"] == "recovery_date"
        assert invalid["errors"] == {"base": "date_not_past"}
        assert invalid["data_schema"]({})["date"] == NOW.date().isoformat()
        coordinator.async_analyze_recovery.assert_not_awaited()
        coordinator.async_apply_recovery.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("direction,offset", [("previous", -1), ("next", 1)])
def test_failed_shortcut_keeps_requested_date_for_retry(navigation_flow, direction, offset):
    async def run():
        flow, coordinator = navigation_flow
        flow._completed_day = DAY
        coordinator.async_analyze_recovery.side_effect = TimeoutError()
        failed = await getattr(flow, f"async_step_recovery_{direction}")()
        target = DAY + timedelta(days=offset)
        assert failed["step_id"] == "recovery_date"
        assert failed["errors"] == {"base": "cannot_connect"}
        assert failed["data_schema"]({}) == {"date": target.isoformat()}
        coordinator.async_apply_recovery.assert_not_awaited()
        assert flow._plan is None
    asyncio.run(run())


def test_completion_refresh_repeated_confirmation_and_close_do_not_reapply(navigation_flow):
    async def run():
        flow, coordinator = navigation_flow
        flow._plan = daily_plan(analysis(), ["a"], 0)
        await flow.async_step_recovery_confirm({"confirm": True})
        for _ in range(2):
            refreshed = await flow.async_step_recovery_done()
            assert refreshed["description_placeholders"]["date"] == DAY.isoformat()
        repeated = await flow.async_step_recovery_confirm({"confirm": True})
        assert repeated["step_id"] == "recovery_date"
        closed = await flow.async_step_recovery_close()
        assert closed["type"] == "abort"
        assert closed["reason"] == "recovery_closed"
        coordinator.async_apply_recovery.assert_awaited_once()
        coordinator.async_analyze_recovery.assert_not_awaited()
    asyncio.run(run())


def test_zero_addition_still_offers_adjacent_days(navigation_flow):
    async def run():
        flow, coordinator = navigation_flow
        flow._plan = daily_plan(analysis(), ["a"], 300)
        assert flow._plan["allocation"] == {}
        menu = await flow.async_step_recovery_confirm({"confirm": True})
        assert menu["type"] == "menu"
        assert menu["menu_options"] == ["recovery_previous", "recovery_next", "recovery_close"]
        assert flow._plan is None
        coordinator.async_apply_recovery.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("completed,previous,following", [
    ("2024-03-01", "2024-02-29", "2024-03-02"),
    ("2025-01-01", "2024-12-31", "2025-01-02"),
])
def test_navigation_labels_and_dates_cross_month_year_and_leap_day(navigation_flow, completed, previous, following):
    async def run():
        flow, coordinator = navigation_flow
        flow._completed_day = date.fromisoformat(completed)
        menu = await flow.async_step_recovery_done()
        assert menu["description_placeholders"] == {
            "date": completed, "previous_date": previous, "next_date": following,
        }
        await flow.async_step_recovery_previous()
        await flow.async_step_recovery_next()
        assert [call.args[0] for call in coordinator.async_analyze_recovery.await_args_list] == [
            date.fromisoformat(previous), date.fromisoformat(following),
        ]
        coordinator.async_apply_recovery.assert_not_awaited()
    asyncio.run(run())


def test_failed_application_does_not_offer_success_navigation(navigation_flow):
    async def run():
        flow, coordinator = navigation_flow
        flow._plan = daily_plan(analysis(), ["a"], 0)
        coordinator.async_apply_recovery.side_effect = OSError("save failed")
        result = await flow.async_step_recovery_confirm({"confirm": True})
        assert result["step_id"] == "recovery_confirm"
        assert result["errors"] == {"base": "cannot_save_recovery"}
        assert flow._completed_day is None
        assert flow._plan is not None
    asyncio.run(run())


@pytest.fixture
def analyze_defaults(recovery):
    """Run the real analyzer with controlled local samples and Recorder results."""
    async def analyze(panels=None, samples=None, daily=None, uncertain=None):
        panels = panels if panels is not None else analysis()["panels"]
        recovery.api.async_fetch_daily_production.return_value = aggregate_payload()
        if samples is not None:
            recovery.state["days"][DAY.isoformat()] = {
                "samples": samples, "powers": {key: value["power"] for key, value in panels.items()},
                "done": False,
            }
        history = {
            "samples": {}, "powers": {}, "daily": daily or {},
            "audits": {}, "uncertain_panels": uncertain or set(),
        }
        instance = SimpleNamespace(async_add_executor_job=AsyncMock(return_value=history))
        registry = SimpleNamespace(async_get_entity_id=Mock(side_effect=lambda *args: "sensor." + args[-1]))
        hass = SimpleNamespace(config=SimpleNamespace(components={"recorder"}))
        with (
            patch("custom_components.plenitude_panels.manual_recovery.dt_util.get_default_time_zone", return_value=ROME),
            patch("custom_components.plenitude_panels.manual_recovery.er.async_get", return_value=registry),
            patch("homeassistant.components.recorder.get_instance", return_value=instance),
        ):
            return await async_analyze(hass, recovery, panels, DAY)
    return analyze


@pytest.mark.parametrize("count", [2, 3, 5])
def test_all_missing_preselects_every_panel_and_suggests_zero_without_fabricating_history(
    analyze_defaults, navigation_flow, recovery, count,
):
    async def run():
        panels = {f"panel_{index}": {"power": 400 + 100 * index} for index in range(count)}
        result = await analyze_defaults(panels=panels)
        assert result["known"] is None
        assert result["suggested_known"] == 0
        assert result["suggested_panels"] == list(panels)
        assert all(item["value"] is None for item in result["recorded"].values())
        flow, coordinator = navigation_flow
        flow._analysis = result
        form = await flow.async_step_recovery_targets()
        assert form["data_schema"]({}) == {"panels": list(panels), "known_energy_wh": 0}
        assert form["description_placeholders"]["known"] == "—"
        assert form["description_placeholders"]["remaining"] == "—"
        assert not recovery.state["pending"]
        assert recovery.state["totals"] == {}
        coordinator.async_apply_recovery.assert_not_awaited()
    asyncio.run(run())


def test_mixed_missing_zero_positive_and_partial_data_have_independent_suggestions(
    analyze_defaults, navigation_flow,
):
    async def run():
        panels = {panel: {"power": 400} for panel in ("missing", "zero", "positive", "partial", "partial_zero")}
        result = await analyze_defaults(
            panels=panels, daily={"zero": 0, "positive": 50},
            samples={str(QUARTER // 300): {"partial": 10, "partial_zero": 0}},
        )
        assert result["suggested_panels"] == ["missing", "zero"]
        assert result["suggested_known"] is None
        assert result["recorded"]["positive"]["value"] == 50
        assert result["recorded"]["partial"]["observed"] == 10
        flow, _ = navigation_flow
        flow._analysis = result
        form = await flow.async_step_recovery_targets()
        with pytest.raises(vol.Invalid):
            form["data_schema"]({})
        assert form["data_schema"]({"known_energy_wh": 60})["panels"] == ["missing", "zero"]
    asyncio.run(run())


def test_partial_positive_readings_are_not_selected_even_with_zero_statistics(analyze_defaults):
    async def run():
        result = await analyze_defaults(
            daily={"a": 0, "b": 0}, samples={str(QUARTER // 300): {"b": 10}},
        )
        assert result["suggested_panels"] == ["a"]
    asyncio.run(run())


def test_uncertain_statistics_are_not_treated_as_no_history(analyze_defaults, navigation_flow):
    async def run():
        result = await analyze_defaults(daily={"a": 5}, uncertain={"a"})
        assert result["recorded"]["a"]["source"] == "uncertain"
        assert result["suggested_known"] is None
        assert result["suggested_panels"] == ["b"]
        flow, _ = navigation_flow
        flow._analysis = result
        form = await flow.async_step_recovery_targets()
        with pytest.raises(vol.Invalid):
            form["data_schema"]({})
    asyncio.run(run())


@pytest.mark.parametrize("original_wh", [0, 17])
def test_editing_all_missing_preview_keeps_user_recipients_and_original_energy(
    analyze_defaults, navigation_flow, original_wh,
):
    async def run():
        flow, coordinator = navigation_flow
        flow._analysis = await analyze_defaults()
        initial = await flow.async_step_recovery_targets()
        assert initial["data_schema"]({}) == {"panels": ["a", "b"], "known_energy_wh": 0}
        preview = await flow.async_step_recovery_targets({"panels": ["b"], "known_energy_wh": original_wh})
        assert preview["step_id"] == "recovery_confirm"
        edited = await flow.async_step_recovery_confirm({"edit_allocation": True})
        assert edited["data_schema"]({}) == {"panels": ["b"], "known_energy_wh": original_wh}
        coordinator.async_apply_recovery.assert_not_awaited()
    asyncio.run(run())


def test_invalid_empty_selection_does_not_restore_suggested_recipients(analyze_defaults, navigation_flow):
    async def run():
        flow, _ = navigation_flow
        flow._analysis = await analyze_defaults()
        invalid = await flow.async_step_recovery_targets({"panels": [], "known_energy_wh": 17})
        assert invalid["errors"] == {"base": "invalid_panels"}
        assert invalid["data_schema"]({}) == {"panels": [], "known_energy_wh": 17}
    asyncio.run(run())


def test_editing_automatic_missing_readings_preview_still_suggests_zero(analyze_defaults, navigation_flow):
    async def run():
        readings = {str((QUARTER + offset) // 300): {"a": None, "b": None} for offset in (0, 300, 600)}
        result = await analyze_defaults(samples=readings)
        flow, _ = navigation_flow
        flow._analysis = result
        flow._plan = automatic_plan(result)
        assert flow._plan["known"] is None
        edited = await flow.async_step_recovery_confirm({"edit_allocation": True})
        defaults = edited["data_schema"]({})
        assert set(defaults["panels"]) == {"a", "b"}
        assert defaults["known_energy_wh"] == 0
    asyncio.run(run())
