"""Verify live spike protection, durable controls and shared sample consumers."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.plenitude_panels.coordinator import PlenitudeCoordinator
from custom_components.plenitude_panels.number import ProductionMaximumNumber
from custom_components.plenitude_panels.production_limit import ProductionLimits
from custom_components.plenitude_panels.recovery import ProductionRecovery
from custom_components.plenitude_panels.sensor import (
    PlenitudeAveragePowerSensor,
    PlenitudeProductionSensor,
    PlenitudeTotalProductionSensor,
)
from custom_components.plenitude_panels.switch import ProductionLimitSwitch
from homeassistant.exceptions import HomeAssistantError


def make_limits(data=None):
    entry = SimpleNamespace(
        entry_id="entry", unique_id="account_supply",
        data={"account_id": "account", "supply_id": "supply", **(data or {})},
    )
    store = SimpleNamespace(async_load=AsyncMock(return_value=None), async_save=AsyncMock())
    with patch("custom_components.plenitude_panels.production_limit.Store", return_value=store):
        limits = ProductionLimits(None, entry)
    return limits, entry


@pytest.mark.parametrize("nominal", ["0.9", "0,9", 0.9])
def test_spike_is_capped_before_sensors_and_recovery(nominal):
    async def run():
        limits, entry = make_limits({"recovery_enabled": False})
        raw = [{"identifier": "a", "power": nominal, "instantProductionWh": 300_000}]
        coordinator = object.__new__(PlenitudeCoordinator)
        coordinator.data = None
        coordinator.hass = SimpleNamespace()
        coordinator._session_initialized = True
        coordinator._last_panel_update = None
        coordinator.api = SimpleNamespace(async_fetch_panels=AsyncMock(return_value=raw))
        coordinator.production_limits = limits
        coordinator._async_persist_session = AsyncMock()
        with patch("custom_components.plenitude_panels.recovery.Store"):
            recovery = ProductionRecovery(None, entry, None)
        recovery.store = SimpleNamespace(async_save=AsyncMock())
        recovery.async_correct_statistics = AsyncMock()
        coordinator.recovery = recovery
        coordinator.data = await coordinator._async_update_locked()
        assert raw[0]["instantProductionWh"] == 300_000  # No mutation of API objects.
        assert PlenitudeAveragePowerSensor(coordinator, entry, "a").native_value == 1800
        assert PlenitudeProductionSensor(coordinator, entry, "a").native_value == 150
        total = PlenitudeTotalProductionSensor(coordinator, entry, "a")
        total._add_current_sample()
        total._add_current_sample()
        assert total.native_value == 150
        history = next(iter(recovery.state["days"].values()))
        assert next(iter(history["samples"].values())) == {"a": 150}
    asyncio.run(run())


def test_restore_and_global_default_only_seed_unseen_panels():
    async def run():
        limits, entry = make_limits()
        panels = [{"identifier": "a", "power": "0.9", "instantProductionWh": 300_000}]
        await limits.async_filter(panels)
        await limits.async_update("a", enabled=False, maximum_w=1234)
        saved = deepcopy(limits.panels)
        restored, _ = make_limits({"limit_new_panels": True})
        restored.store.async_load.return_value = saved
        await restored.async_load()
        assert (await restored.async_filter(panels))[0]["instantProductionWh"] == 300_000
        assert restored.panels["a"]["maximum_w"] == 1234
        # A later default change affects only a newly discovered panel.
        restored.entry.data["limit_new_panels"] = False
        await restored.async_filter(panels + [{"identifier": "b", "power": "0.5"}])
        assert restored.panels["a"] == saved["a"]
        assert restored.panels["b"] == {"enabled": False, "maximum_w": 1000}
        await restored.async_update("a", enabled=True)
        assert (await restored.async_filter(panels))[0]["instantProductionWh"] == 1234
    asyncio.run(run())


@pytest.mark.parametrize("nominal", [None, "", "bad", 0, -1, "nan", "inf"])
def test_missing_nominal_needs_manual_threshold(nominal):
    async def run():
        limits, entry = make_limits()
        panels = [{"identifier": "a", "power": nominal, "instantProductionWh": 300_000}]
        assert (await limits.async_filter(panels))[0]["instantProductionWh"] == 300_000
        assert limits.panels["a"] == {"enabled": True, "maximum_w": None}
        await limits.async_update("a", maximum_w=1800)
        assert (await limits.async_filter(panels))[0]["instantProductionWh"] == 1800
    asyncio.run(run())


@pytest.mark.parametrize("value", [0, 500, 1800, None, "bad", "nan", -10])
def test_non_excessive_or_invalid_input_is_not_replaced(value):
    async def run():
        limits, _ = make_limits()
        panels = [{"identifier": "a", "power": 0.9, "instantProductionWh": value}]
        assert (await limits.async_filter(panels))[0]["instantProductionWh"] == value
    asyncio.run(run())


def test_controls_persist_without_reprocessing_current_sample():
    async def run():
        limits, entry = make_limits()
        panels = [{"identifier": "a", "power": "0.9", "instantProductionWh": 300_000}]
        coordinator = object.__new__(PlenitudeCoordinator)
        coordinator.data = {"a": (await limits.async_filter(panels))[0]}
        coordinator.production_limits = limits
        coordinator._operation_lock = asyncio.Lock()
        coordinator.async_update_listeners = Mock()
        switch = ProductionLimitSwitch(coordinator, entry, "a")
        maximum = ProductionMaximumNumber(coordinator, entry, "a")
        assert switch.device_info["identifiers"] == {("plenitude_panels", "account_supply_a")}
        assert maximum.native_value == 1800
        await asyncio.gather(switch.async_turn_off(), maximum.async_set_native_value(2000))
        assert not switch.is_on
        assert maximum.native_value == 2000
        assert coordinator.data["a"]["instantProductionWh"] == 1800
        await switch.async_turn_on()
        assert switch.is_on
        assert not switch.extra_state_attributes["threshold_required"]
        for invalid in (0, -1, float("nan"), float("inf")):
            with pytest.raises(HomeAssistantError):
                await maximum.async_set_native_value(invalid)
        limits.store.async_save.side_effect = OSError("disk full")
        with pytest.raises(OSError):
            await switch.async_turn_off()
        assert switch.is_on  # A failed write must not change the active policy.
    asyncio.run(run())


def test_initialization_is_not_published_when_storage_fails():
    async def run():
        limits, _ = make_limits()
        limits.store.async_save.side_effect = OSError("disk full")
        with pytest.raises(OSError):
            await limits.async_filter([{"identifier": "a", "power": 0.9}])
        assert limits.panels == {}
    asyncio.run(run())
