"""Sensor platform for Plenitude Solar Panels."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, FIVE_MINUTE_HOUR_FRACTION
from .coordinator import PlenitudeCoordinator
from .recovery import number, production


def _five_minute_energy(value: Any) -> float | None:
    """Convert the API's hourly-equivalent Wh value to a five-minute interval."""
    parsed = production(value)
    return parsed * FIVE_MINUTE_HOUR_FRACTION if parsed is not None else None


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up one production and one irradiance sensor for every discovered panel."""
    coordinator: PlenitudeCoordinator = hass.data[DOMAIN][entry.entry_id]
    known_panels: set[str] = set()

    def add_new_panels() -> None:
        panel_ids = set(coordinator.data or {}) - known_panels
        if not panel_ids:
            return
        known_panels.update(panel_ids)
        entities: list[SensorEntity] = []
        for panel_id in sorted(panel_ids):
            entities.extend(
                [
                    PlenitudeAveragePowerSensor(coordinator, entry, panel_id),
                    PlenitudeProductionSensor(coordinator, entry, panel_id),
                    PlenitudeTotalProductionSensor(coordinator, entry, panel_id),
                    PlenitudeIrradianceSensor(coordinator, entry, panel_id),
                ]
            )
        async_add_entities(entities)

    add_new_panels()
    entry.async_on_unload(coordinator.async_add_listener(add_new_panels))


class PlenitudePanelSensor(CoordinatorEntity[PlenitudeCoordinator], SensorEntity):
    """Base entity bound to one panel identifier."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: PlenitudeCoordinator, entry: ConfigEntry, panel_id: str
    ) -> None:
        super().__init__(coordinator)
        self._panel_id = panel_id
        self._entry_unique_id = entry.unique_id or entry.entry_id

    @property
    def _panel(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self._panel_id, {})

    @property
    def available(self) -> bool:
        """A removed panel is unavailable without deleting historical entities."""
        return super().available and self._panel_id in (self.coordinator.data or {})

    @property
    def device_info(self) -> DeviceInfo:
        """Expose the panel as an individual photovoltaic device."""
        panel = self._panel
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry_unique_id}_{self._panel_id}")},
            name=panel.get("position") or self._panel_id,
            manufacturer="Eni Plenitude",
            model="Photovoltaic Panel",
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return panel metadata supplied by the details endpoint."""
        return {
            key: self._panel[key]
            for key in ("identifier", "position", "locationId", "size", "power", "state", "terminationDate")
            if key in self._panel
        }


class PlenitudeProductionSensor(PlenitudePanelSensor):
    """Energy produced by a panel during the last five minutes."""

    _attr_name = "Production (5 min)"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = "Wh"

    def __init__(self, coordinator, entry, panel_id) -> None:
        super().__init__(coordinator, entry, panel_id)
        self._attr_unique_id = f"{self._entry_unique_id}_{panel_id}_production_5_min"

    @property
    def native_value(self) -> float | None:
        return _five_minute_energy(self._panel.get("instantProductionWh"))


class PlenitudeAveragePowerSensor(PlenitudePanelSensor):
    """Average panel power reported by the API for the last five minutes."""

    _attr_name = "Average Power (5 min)"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = "W"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry, panel_id) -> None:
        super().__init__(coordinator, entry, panel_id)
        self._attr_unique_id = f"{self._entry_unique_id}_{panel_id}_average_power_5_min"

    @property
    def native_value(self) -> float | None:
        return production(self._panel.get("instantProductionWh"))


class PlenitudeTotalProductionSensor(PlenitudePanelSensor, RestoreEntity):
    """Cumulative panel energy suitable for the Home Assistant Energy dashboard."""

    _attr_name = "Total Production"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = "Wh"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator, entry, panel_id) -> None:
        super().__init__(coordinator, entry, panel_id)
        self._attr_unique_id = f"{self._entry_unique_id}_{panel_id}_total_production"
        self._total_wh = 0.0
        self._last_sample_id: int | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the running total and process the coordinator's current sample."""
        await super().async_added_to_hass()
        self.coordinator.recovery.entities[self._panel_id] = self.entity_id
        if last_state := await self.async_get_last_state():
            try:
                restored = number(last_state.state)
                unit = last_state.attributes.get("unit_of_measurement", "Wh")
                factor = {"Wh": 1, "kWh": 1000, "MWh": 1000000}.get(unit)
                restored = restored * factor if restored is not None and factor is not None else None
                recovered = number(last_state.attributes.get("recovered_energy_wh", 0)) or 0
                self._total_wh = max(0, restored - recovered) if restored is not None else 0
            except (TypeError, ValueError):
                pass
            try:
                self._last_sample_id = int(last_state.attributes.get("last_sample_id"))
            except (TypeError, ValueError):
                pass
        self._add_current_sample()

    def _add_current_sample(self) -> None:
        """Add a valid API value at most once for each five-minute window."""
        sample_id = self.coordinator.sample_id
        if sample_id is None or sample_id == self._last_sample_id:
            return
        energy_wh = _five_minute_energy(self._panel.get("instantProductionWh"))
        if energy_wh is None:
            return
        if energy_wh < 0:
            return
        self._total_wh += energy_wh
        self._last_sample_id = sample_id

    def _handle_coordinator_update(self) -> None:
        self._add_current_sample()
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float:
        return self._total_wh + self.coordinator.recovery.total(self._panel_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        recovery = self.coordinator.recovery
        return {
            **super().extra_state_attributes,
            "last_sample_id": self._last_sample_id,
            "recovered_energy_wh": recovery.total(self._panel_id),
            "last_recovered_day": recovery.state["last_days"].get(self._panel_id),
            "recovery_status": recovery.status,
            "recovery_pending_statistics": sum(
                record["panel"] == self._panel_id for record in recovery.state["pending"]
            ),
        }


class PlenitudeIrradianceSensor(PlenitudePanelSensor):
    """Solar irradiance reported for a panel."""

    _attr_name = "Irradiance"
    _attr_device_class = SensorDeviceClass.IRRADIANCE
    _attr_native_unit_of_measurement = "W/m²"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry, panel_id) -> None:
        super().__init__(coordinator, entry, panel_id)
        self._attr_unique_id = f"{self._entry_unique_id}_{panel_id}_irradiance"

    @property
    def native_value(self) -> float | None:
        return self._panel.get("irradiance")
