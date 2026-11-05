"""Durable per-panel limits applied before live samples reach consumers."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import logging

from homeassistant.helpers.storage import Store

from .const import CONF_ACCOUNT_ID, CONF_LIMIT_NEW_PANELS, CONF_SUPPLY_ID, DOMAIN
from .recovery import number, production

_LOGGER = logging.getLogger(__name__)


class ProductionLimits:
    """Persist controls independently of entity restore order and polling."""

    def __init__(self, hass, entry):
        self.entry = entry
        identity = f"{entry.data[CONF_ACCOUNT_ID]}:{entry.data[CONF_SUPPLY_ID]}"
        digest = sha256(identity.encode()).hexdigest()[:16]
        self.store = Store(hass, 1, f"{DOMAIN}.production_limits.{entry.entry_id}.{digest}")
        self.panels: dict[str, dict] = {}

    async def async_load(self):
        self.panels = await self.store.async_load() or {}

    async def async_update(self, panel_id, **changes):
        updated = deepcopy(self.panels)
        updated[panel_id].update(changes)
        # Publish only after a successful write, including for newly seen panels.
        await self.store.async_save(updated)
        self.panels = updated

    async def async_filter(self, panels):
        updated = deepcopy(self.panels)
        for panel in panels:
            identifier = panel["identifier"]
            if identifier not in updated:
                # The details endpoint expresses nominal power in kW.
                nominal_kw = number(panel.get("power"))
                updated[identifier] = {
                    "enabled": self.entry.data.get(CONF_LIMIT_NEW_PANELS, True),
                    "maximum_w": nominal_kw * 2000 if nominal_kw else None,
                }
        if updated != self.panels:
            await self.store.async_save(updated)
            self.panels = updated

        result = []
        for panel in panels:
            filtered = dict(panel)
            settings = self.panels[panel["identifier"]]
            maximum = number(settings.get("maximum_w"))
            value = production(panel.get("instantProductionWh"))
            if settings["enabled"] and maximum and value is not None and value > maximum:
                filtered["instantProductionWh"] = maximum
                _LOGGER.warning(
                    "Panel %s production limited from %s W to %s W",
                    panel["identifier"], value, maximum,
                )
            result.append(filtered)
        return result
