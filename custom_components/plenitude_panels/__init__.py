"""Plenitude Solar Panels integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PlenitudeApi, PlenitudeApiError, PlenitudeAuthError
from .const import (
    CONF_ACCOUNT_ID,
    CONF_COOKIE,
    CONF_POLL_INTERVAL,
    CONF_SUPPLY_ID,
    CONF_USER_AGENT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_USER_AGENT,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import PlenitudeCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Plenitude Solar Panels from a config entry."""
    api = PlenitudeApi(
        async_get_clientsession(hass),
        entry.data[CONF_COOKIE],
        entry.data[CONF_ACCOUNT_ID],
        entry.data[CONF_SUPPLY_ID],
        user_agent=entry.data.get(CONF_USER_AGENT, DEFAULT_USER_AGENT),
    )
    try:
        await api.async_initialize_session()
    except PlenitudeAuthError as err:
        raise ConfigEntryAuthFailed from err
    except PlenitudeApiError as err:
        raise ConfigEntryNotReady from err
    if api.session_changed:
        if entry.data.get(CONF_COOKIE) == api.session_source_cookie:
            updated_data = {**entry.data, **api.session_data}
            hass.config_entries.async_update_entry(entry, data=updated_data)
        api.mark_session_persisted()
    coordinator = PlenitudeCoordinator(
        hass,
        entry,
        api,
        entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
        session_initialized=True,
    )
    await coordinator.recovery.async_load()
    await coordinator.production_limits.async_load()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove obsolete Gigya data from existing entries."""
    if entry.version >= 2:
        return True

    data = {
        CONF_COOKIE: entry.data[CONF_COOKIE],
        CONF_ACCOUNT_ID: entry.data[CONF_ACCOUNT_ID],
        CONF_SUPPLY_ID: entry.data[CONF_SUPPLY_ID],
        CONF_POLL_INTERVAL: entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
    }
    hass.config_entries.async_update_entry(entry, data=data, version=2)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
