"""Per-panel production limit switches."""

from homeassistant.components.switch import SwitchEntity

from .limit_entity import ProductionLimitEntity, setup_controls


async def async_setup_entry(hass, entry, async_add_entities):
    setup_controls(hass, entry, async_add_entities, ProductionLimitSwitch)


class ProductionLimitSwitch(ProductionLimitEntity, SwitchEntity):
    """Enable capping future production samples for one panel."""

    _attr_translation_key = "production_limit"
    _attr_icon = "mdi:speedometer"

    @property
    def is_on(self):
        return self.settings["enabled"]

    @property
    def extra_state_attributes(self):
        return {"threshold_required": not bool(self.settings.get("maximum_w"))}

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_set_production_limit(self._panel_id, enabled=True)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_set_production_limit(self._panel_id, enabled=False)
