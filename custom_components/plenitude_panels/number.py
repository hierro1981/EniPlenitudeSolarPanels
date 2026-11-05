"""Per-panel maximum plausible average power."""

import math

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.exceptions import HomeAssistantError

from .limit_entity import ProductionLimitEntity, setup_controls


async def async_setup_entry(hass, entry, async_add_entities):
    setup_controls(hass, entry, async_add_entities, ProductionMaximumNumber)


class ProductionMaximumNumber(ProductionLimitEntity, NumberEntity):
    """Maximum accepted five-minute average power, in watts."""

    _attr_translation_key = "production_maximum"
    _attr_device_class = NumberDeviceClass.POWER
    _attr_native_unit_of_measurement = "W"
    _attr_native_min_value = 1
    _attr_native_max_value = 1_000_000_000
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    @property
    def native_value(self):
        return self.settings.get("maximum_w")

    async def async_set_native_value(self, value):
        if not math.isfinite(value) or not self.native_min_value <= value <= self.native_max_value:
            raise HomeAssistantError("The production maximum must be a positive finite value in W")
        await self.coordinator.async_set_production_limit(self._panel_id, maximum_w=value)
