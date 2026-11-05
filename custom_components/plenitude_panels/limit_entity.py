"""Shared discovery and device identity for production limit controls."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


def setup_controls(hass, entry, async_add_entities, entity_class):
    """Discover controls alongside sensors, including panels added later."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    known = set()

    def add_new_panels():
        new = set(coordinator.data or {}) - known
        known.update(new)
        async_add_entities([entity_class(coordinator, entry, panel) for panel in sorted(new)])

    add_new_panels()
    entry.async_on_unload(coordinator.async_add_listener(add_new_panels))


class ProductionLimitEntity(CoordinatorEntity):
    """A configuration control attached to the existing panel device."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry, panel_id):
        super().__init__(coordinator)
        self._panel_id = panel_id
        identity = f"{entry.unique_id or entry.entry_id}_{panel_id}"
        self._attr_unique_id = f"{identity}_{self._attr_translation_key}"
        panel = (coordinator.data or {}).get(panel_id, {})
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, identity)},
            name=panel.get("position") or panel_id,
            manufacturer="Eni Plenitude",
            model="Photovoltaic Panel",
        )

    @property
    def available(self):
        return super().available and self._panel_id in (self.coordinator.data or {})

    @property
    def settings(self):
        return self.coordinator.production_limits.panels[self._panel_id]
