"""Data coordinator for Plenitude Solar Panels."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PlenitudeApi, PlenitudeApiError, PlenitudeAuthError
from .const import (
    CONF_COOKIE,
    DEFAULT_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)
from .production_limit import ProductionLimits
from .recovery import ProductionRecovery

_LOGGER = logging.getLogger(__name__)


class PlenitudeCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Poll the Plenitude API and retain panels indexed by identifier."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: PlenitudeApi,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        session_initialized: bool = False,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Plenitude Solar Panels",
            update_interval=timedelta(minutes=MIN_POLL_INTERVAL),
        )
        self.entry = entry
        self.api = api
        self._session_initialized = session_initialized
        self._panel_interval = timedelta(minutes=max(MIN_POLL_INTERVAL, poll_interval))
        self._last_panel_update: datetime | None = None
        self.sample_id: int | None = None
        self.recovery = ProductionRecovery(hass, entry, api)
        self.production_limits = ProductionLimits(hass, entry)
        self._operation_lock = asyncio.Lock()

    async def async_set_production_limit(self, panel_id: str, **changes) -> None:
        """Serialize durable control changes with polling; affect future samples."""
        async with self._operation_lock:
            await self.production_limits.async_update(panel_id, **changes)
            self.async_update_listeners()

    async def async_analyze_recovery(self, day):
        """Serialize manual analysis with polling and session rotations."""
        from .manual_recovery import async_analyze

        async with self._operation_lock:
            try:
                return await async_analyze(self.hass, self.recovery, self.data or {}, day)
            finally:
                await self._async_persist_session()

    async def async_apply_recovery(self, plan):
        """Apply the preview once and publish totals without adding a live sample."""
        from .manual_recovery import RecoveryError, async_apply

        async with self._operation_lock:
            if set(plan["panels"]) != set(self.data or {}):
                raise RecoveryError("preview_stale")
            await async_apply(self.recovery, plan)
            self.async_set_updated_data(dict(self.data or {}))
            await self.recovery.async_correct_statistics()

    async def _async_persist_session(self) -> None:
        """Persist only newly refreshed session values, without reloading the entry."""
        if not self.api.session_changed:
            return
        if self.entry.data.get(CONF_COOKIE) != self.api.session_source_cookie:
            _LOGGER.debug("Discarding cookie update from a stale Plenitude session")
            self.api.mark_session_persisted()
            return
        session_data = self.api.session_data
        updated_data = {
            **self.entry.data,
            CONF_COOKIE: session_data[CONF_COOKIE],
        }
        self.hass.config_entries.async_update_entry(self.entry, data=updated_data)
        self.api.mark_session_persisted()

    async def async_analyze_statistics(self, day):
        """Analyze an API day and Recorder sums without changing energy totals."""
        from .manual_repair import async_analyze

        async with self._operation_lock:
            try:
                return await async_analyze(self.hass, self.recovery, self.data or {}, day)
            finally:
                await self._async_persist_session()

    async def async_repair_statistics(self, plan):
        from .manual_recovery import RecoveryError
        from .manual_repair import async_apply

        async with self._operation_lock:
            if set(plan["panels"]) != set(self.data or {}):
                raise RecoveryError("preview_stale")
            changed = await async_apply(self.hass, self.recovery, plan)
            self.async_set_updated_data(dict(self.data or {}))
            return changed

    async def async_repair_backup(self):
        from .manual_repair import async_reconcile_journal

        async with self._operation_lock:
            await async_reconcile_journal(self.hass, self.recovery)
            backup = self.recovery.state.get("repair_backup")
            if backup and backup["phase"] == "applied":
                return {"token": backup["token"], "day": backup["plan"]["day"]}
            return None

    async def async_undo_statistics(self, token):
        from .manual_recovery import RecoveryError
        from .manual_repair import async_reconcile_journal, async_undo

        async with self._operation_lock:
            await async_reconcile_journal(self.hass, self.recovery)
            backup = self.recovery.state.get("repair_backup")
            if not backup or backup["token"] != token:
                raise RecoveryError("preview_stale")
            await async_undo(self.hass, self.recovery)
            self.async_set_updated_data(dict(self.data or {}))

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Serialize live polling with manual recovery operations."""
        async with self._operation_lock:
            return await self._async_update_locked()

    async def _async_update_locked(self) -> dict[str, dict[str, Any]]:
        """Maintain the browser session and retrieve panel data when due."""
        now = datetime.now(UTC)
        panel_refresh_due = (
            self.data is None
            or self._last_panel_update is None
            or now - self._last_panel_update >= self._panel_interval
        )
        try:
            # The portal renews GYSESSION before it expires, even when panel data
            # is configured to refresh less frequently than the session cadence.
            if self._session_initialized:
                self._session_initialized = False
            else:
                await self.api.async_initialize_session()
                await self._async_persist_session()

            if not panel_refresh_due and self.data is not None:
                await self.recovery.async_correct_statistics()
                _LOGGER.debug("Plenitude session maintained; panel refresh is not due")
                return self.data

            panels = await self.api.async_fetch_panels()
            panels = await self.production_limits.async_filter(panels)
            # The API reports energy for a five-minute window. This id makes a
            # repeated refresh in the same window safe for cumulative sensors.
            sample_id = int(now.timestamp() // 300)
            await self.recovery.async_sample(panels, sample_id, now)
            await self.recovery.async_correct_statistics()
            self._last_panel_update = now
            self.sample_id = sample_id
            return {panel["identifier"]: panel for panel in panels}
        except PlenitudeAuthError as err:
            raise ConfigEntryAuthFailed from err
        except OSError as err:
            raise UpdateFailed("Unable to persist panel production limits or samples") from err
        except PlenitudeApiError as err:
            if not panel_refresh_due and self.data is not None:
                _LOGGER.debug(
                    "Plenitude session maintenance failed; retaining cached panel data: %s",
                    err,
                )
                return self.data
            raise UpdateFailed(f"Unable to update Plenitude panels: {err}") from err
        finally:
            # Keep a successful init or details rotation across a later request
            # failure, rather than waiting for the whole panel sequence to pass.
            await self._async_persist_session()
