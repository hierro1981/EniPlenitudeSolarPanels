"""Config flow for Plenitude Solar Panels."""

from __future__ import annotations

from datetime import date, timedelta

import voluptuous as vol
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    DateSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.util import dt as dt_util
from sqlalchemy.exc import SQLAlchemyError

from homeassistant import config_entries

from .api import (
    PlenitudeApi,
    PlenitudeApiError,
    PlenitudeAuthError,
    PlenitudeSessionRejectedError,
)
from .const import (
    CONF_ACCOUNT_ID,
    CONF_COOKIE,
    CONF_POD,
    CONF_POLL_INTERVAL,
    CONF_RECOVERY_ENABLED,
    CONF_SUPPLY_ID,
    CONF_USER_AGENT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_USER_AGENT,
    DOMAIN,
    MIN_POLL_INTERVAL,
)
from .manual_recovery import RecoveryError, automatic_plan, daily_plan
from .recovery_presentation import async_formatter, summary


def _schema(defaults: dict | None = None) -> vol.Schema:
    """Build the connection schema, optionally using existing entry values."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_COOKIE, default=defaults.get(CONF_COOKIE, "")): str,
            vol.Required(
                CONF_USER_AGENT, default=defaults.get(CONF_USER_AGENT, DEFAULT_USER_AGENT)
            ): vol.All(str, vol.Strip, vol.Length(min=1)),
            vol.Required(CONF_ACCOUNT_ID, default=defaults.get(CONF_ACCOUNT_ID, "")): str,
            vol.Required(CONF_SUPPLY_ID, default=defaults.get(CONF_SUPPLY_ID, "")): str,
            vol.Optional(CONF_POD, default=defaults.get(CONF_POD, "")): str,
            vol.Required(
                CONF_RECOVERY_ENABLED, default=defaults.get(CONF_RECOVERY_ENABLED, True)
            ): bool,
            vol.Required(
                CONF_POLL_INTERVAL,
                default=defaults.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_POLL_INTERVAL,
                    max=60,
                    step=5,
                    mode=NumberSelectorMode.SLIDER,
                    unit_of_measurement="min",
                )
            ),
        }
    )


def _reauth_schema(defaults: dict | None = None) -> vol.Schema:
    """Build the minimal schema used to replace an expired browser cookie."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_COOKIE, default=defaults.get(CONF_COOKIE, "")): str,
            vol.Required(
                CONF_USER_AGENT, default=defaults.get(CONF_USER_AGENT, DEFAULT_USER_AGENT)
            ): vol.All(str, vol.Strip, vol.Length(min=1)),
        }
    )


def _entry_data(data: dict) -> dict:
    """Keep ConfigEntry data limited to supported, user-owned values."""
    return {
        CONF_COOKIE: data[CONF_COOKIE],
        CONF_USER_AGENT: data.get(CONF_USER_AGENT, DEFAULT_USER_AGENT).strip() or DEFAULT_USER_AGENT,
        CONF_ACCOUNT_ID: data[CONF_ACCOUNT_ID],
        CONF_SUPPLY_ID: data[CONF_SUPPLY_ID],
        CONF_POD: data.get(CONF_POD, "").strip().upper(),
        CONF_RECOVERY_ENABLED: data.get(CONF_RECOVERY_ENABLED, True),
        CONF_POLL_INTERVAL: int(data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)),
    }


async def _validate(hass, data: dict) -> dict[str, str]:
    """Verify credentials with the same sequence used by the coordinator."""
    api = PlenitudeApi(
        async_get_clientsession(hass),
        data[CONF_COOKIE],
        data[CONF_ACCOUNT_ID],
        data[CONF_SUPPLY_ID],
        user_agent=data.get(CONF_USER_AGENT, DEFAULT_USER_AGENT),
    )
    await api.async_initialize_session()
    await api.async_fetch_panels()
    return api.session_data


class PlenitudePanelsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle configuration and reauthentication for Plenitude panels."""

    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        """Expose configuration changes from the integration's Configure button."""
        return PlenitudePanelsOptionsFlow(config_entry)

    async def _validate(self, data: dict) -> dict[str, str]:
        """Verify credentials with the same sequence used by the coordinator."""
        return await _validate(self.hass, data)

    async def async_step_user(self, user_input=None):
        """Handle the initial configuration."""
        errors = {}
        if user_input is not None:
            try:
                session_data = await self._validate(user_input)
            except PlenitudeSessionRejectedError:
                errors["base"] = "session_rejected"
            except PlenitudeAuthError:
                errors["base"] = "invalid_auth"
            except (PlenitudeApiError, TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                entry_data = _entry_data(user_input)
                entry_data.update(session_data)
                await self.async_set_unique_id(
                    f"{entry_data[CONF_ACCOUNT_ID]}_{entry_data[CONF_SUPPLY_ID]}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Plenitude {user_input[CONF_SUPPLY_ID]}",
                    data=entry_data,
                )
        return self.async_show_form(step_id="user", data_schema=_schema(user_input), errors=errors)

    async def async_step_reauth(self, entry_data):
        """Start reauthentication after the browser session has expired."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Accept replacement session credentials while preserving the entry identity."""
        errors = {}
        if user_input is not None:
            data = _entry_data({**self._reauth_entry.data, **user_input})
            try:
                session_data = await self._validate(data)
            except PlenitudeSessionRejectedError:
                errors["base"] = "session_rejected"
            except PlenitudeAuthError:
                errors["base"] = "invalid_auth"
            except (PlenitudeApiError, TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                data[CONF_COOKIE] = session_data[CONF_COOKIE]
                self.hass.config_entries.async_update_entry(self._reauth_entry, data=data)
                await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        defaults = {**self._reauth_entry.data, **(user_input or {})}
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=_reauth_schema(defaults), errors=errors
        )


class PlenitudePanelsOptionsFlow(config_entries.OptionsFlow):
    """Allow users to replace credentials or adjust integration settings."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._analysis = None
        self._plan = None
        self._selected_day: date | None = None
        self._completed_day: date | None = None
        self._repair_analysis = None
        self._repair_plan = None
        self._repair_day: date | None = None
        self._undo_token = None

    async def async_step_init(self, user_input=None):
        """Offer settings and a separate, non-reloading recovery wizard."""
        options = ["settings", "recovery_date", "repair_date"]
        recovery = getattr(self._coordinator(), "recovery", None)
        if recovery and recovery.state.get("repair_backup", {}).get("phase") in (
            "prepared", "applied", "undo_prepared",
        ):
            options.append("repair_undo")
        return self.async_show_menu(step_id="init", menu_options=options)

    def _coordinator(self):
        return self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)

    async def async_step_recovery_date(self, user_input=None):
        """Analyze a completed day before asking for missing information."""
        errors = {}
        coordinator = self._coordinator()
        if coordinator is None:
            return self.async_abort(reason="integration_not_loaded")
        if user_input is not None:
            self._analysis = None
            self._plan = None
            try:
                day = date.fromisoformat(user_input["date"])
                self._selected_day = day
                if day >= dt_util.now().date():
                    raise RecoveryError("date_not_past")
                self._analysis = await coordinator.async_analyze_recovery(day)
                try:
                    self._plan = automatic_plan(self._analysis)
                except RecoveryError:
                    return await self.async_step_recovery_targets()
                return await self.async_step_recovery_confirm()
            except RecoveryError as err:
                self._plan = None
                errors["base"] = str(err)
            except PlenitudeAuthError:
                errors["base"] = "invalid_auth"
            except (PlenitudeApiError, TimeoutError):
                errors["base"] = "cannot_connect"
            except OSError:
                errors["base"] = "cannot_save_recovery"
            except (TypeError, ValueError):
                errors["base"] = "invalid_recovery_data"
        return self.async_show_form(
            step_id="recovery_date",
            data_schema=vol.Schema({
                vol.Required("date", default=(
                    self._selected_day or dt_util.now().date() - timedelta(days=1)
                ).isoformat()): DateSelector(),
            }),
            errors=errors,
        )

    async def async_step_recovery_targets(self, user_input=None):
        """Request recipients and original daily energy, explicitly excluding recoveries."""
        errors = {}
        if self._analysis is None:
            return await self.async_step_recovery_date()
        if user_input is not None:
            try:
                self._plan = daily_plan(
                    self._analysis, user_input["panels"], user_input["known_energy_wh"],
                )
            except RecoveryError as err:
                self._plan = None
                errors["base"] = str(err)
            else:
                return await self.async_step_recovery_confirm()
        options = [
            {"value": identifier, "label": f"{details.get('position') or identifier} ({identifier})"}
            for identifier, details in sorted(self._analysis["panels"].items())
        ]
        suggested_known = self._analysis.get("suggested_known", self._analysis["known"])
        defaults = user_input or ({
            "panels": self._plan.get("selected_panels", list(self._plan["allocation"])),
            "known_energy_wh": self._plan["known"] if self._plan["known"] is not None else suggested_known,
        } if self._plan is not None else {})
        suggested = list(self._analysis.get("suggested_panels", self._analysis["automatic"] or {}))
        known_default = defaults.get("known_energy_wh", suggested_known)
        known_field = (
            vol.Required("known_energy_wh", default=known_default)
            if known_default is not None
            else vol.Required("known_energy_wh", default=None)
        )
        formatter = await async_formatter(self.hass, self.context)
        presentation = self._plan if self._plan is not None else self._analysis
        return self.async_show_form(
            step_id="recovery_targets",
            data_schema=vol.Schema({
                vol.Required("panels", default=defaults.get("panels", suggested)): SelectSelector(
                    SelectSelectorConfig(options=options, multiple=True, mode=SelectSelectorMode.DROPDOWN),
                ),
                known_field: NumberSelector(NumberSelectorConfig(
                    min=0, step="any", mode=NumberSelectorMode.BOX, unit_of_measurement="Wh",
                )),
            }),
            errors=errors,
            description_placeholders=summary(
                presentation, formatter, self._plan["allocation"] if self._plan is not None else None,
            ),
        )

    async def async_step_recovery_confirm(self, user_input=None):
        """Show an explicit preview; cancellation never allocates energy."""
        if self._plan is None:
            return await self.async_step_recovery_date()
        errors = {}
        if user_input is not None:
            if user_input.get("edit_allocation"):
                return await self.async_step_recovery_targets()
            if not user_input.get("confirm"):
                errors["base"] = "confirmation_required"
            else:
                coordinator = self._coordinator()
                if coordinator is None:
                    return self.async_abort(reason="integration_not_loaded")
                try:
                    await coordinator.async_apply_recovery(self._plan)
                except RecoveryError as err:
                    errors["base"] = str(err)
                except OSError:
                    errors["base"] = "cannot_save_recovery"
                else:
                    self._completed_day = date.fromisoformat(self._plan["day"])
                    self._selected_day = self._completed_day
                    self._analysis = None
                    self._plan = None
                    return await self.async_step_recovery_done()
        plan = self._plan
        formatter = await async_formatter(self.hass, self.context)
        return self.async_show_form(
            step_id="recovery_confirm",
            data_schema=vol.Schema({
                vol.Required("confirm", default=False): bool,
                vol.Optional("edit_allocation", default=False): bool,
            }),
            errors=errors,
            description_placeholders={
                **summary(plan, formatter, plan["allocation"]),
                "mode": "15 min" if plan["mode"] == "quarter_hour" else "≈",
            },
        )

    async def async_step_recovery_done(self, user_input=None):
        """Keep the completed flow open to analyze adjacent production dates."""
        if self._completed_day is None:
            return await self.async_step_recovery_date()
        day = self._completed_day
        options = []
        previous = day - timedelta(days=1) if day > date.min else None
        following = day + timedelta(days=1)
        if previous is not None:
            options.append("recovery_previous")
        if following < dt_util.now().date():
            options.append("recovery_next")
        options.append("recovery_close")
        return self.async_show_menu(
            step_id="recovery_done", menu_options=options,
            description_placeholders={
                "date": day.isoformat(),
                "previous_date": previous.isoformat() if previous else "—",
                "next_date": following.isoformat(),
            },
        )

    async def async_step_recovery_previous(self, user_input=None):
        """Analyze the previous date without reusing recipients or quantities."""
        if self._completed_day is None:
            return await self.async_step_recovery_date()
        if self._completed_day == date.min:
            return await self.async_step_recovery_done()
        return await self.async_step_recovery_date({
            "date": (self._completed_day - timedelta(days=1)).isoformat(),
        })

    async def async_step_recovery_next(self, user_input=None):
        """Analyze the next date; the date step also rejects today and the future."""
        if self._completed_day is None:
            return await self.async_step_recovery_date()
        return await self.async_step_recovery_date({
            "date": (self._completed_day + timedelta(days=1)).isoformat(),
        })

    async def async_step_recovery_close(self, user_input=None):
        """End the recovery session without changing integration settings."""
        return self.async_abort(reason="recovery_closed")

    async def async_step_settings(self, user_input=None):
        """Update and validate values from the Configure button."""
        errors = {}
        if user_input is not None:
            data = _entry_data(user_input)
            try:
                session_data = await _validate(self.hass, data)
            except PlenitudeSessionRejectedError:
                errors["base"] = "session_rejected"
            except PlenitudeAuthError:
                errors["base"] = "invalid_auth"
            except (PlenitudeApiError, TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                unique_id = f"{data[CONF_ACCOUNT_ID]}_{data[CONF_SUPPLY_ID]}"
                if any(
                    entry.entry_id != self._entry.entry_id and entry.unique_id == unique_id
                    for entry in self.hass.config_entries.async_entries(DOMAIN)
                ):
                    errors["base"] = "already_configured"
                    return self.async_show_form(
                        step_id="settings",
                        data_schema=_schema(data),
                        errors=errors,
                    )
                data[CONF_COOKIE] = session_data[CONF_COOKIE]
                self.hass.config_entries.async_update_entry(
                    self._entry,
                    data=data,
                    title=f"Plenitude {data[CONF_SUPPLY_ID]}",
                    unique_id=unique_id,
                )
                await self.hass.config_entries.async_reload(self._entry.entry_id)
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="settings",
            data_schema=_schema({**self._entry.data, **(user_input or {})}), errors=errors
        )

    async def async_step_repair_date(self, user_input=None):
        """Inspect production statistics and the API for a completed calendar day."""
        errors = {}
        coordinator = self._coordinator()
        if coordinator is None:
            return self.async_abort(reason="integration_not_loaded")
        if user_input is not None:
            self._repair_analysis = None
            self._repair_plan = None
            try:
                self._repair_day = date.fromisoformat(user_input["date"])
                self._repair_analysis = await coordinator.async_analyze_statistics(self._repair_day)
            except RecoveryError as err:
                errors["base"] = str(err)
            except PlenitudeAuthError:
                errors["base"] = "invalid_auth"
            except (PlenitudeApiError, TimeoutError):
                errors["base"] = "cannot_connect"
            except (OSError, ValueError):
                errors["base"] = "invalid_recovery_data"
            except SQLAlchemyError:
                errors["base"] = "repair_database_error"
            else:
                return await self.async_step_repair_targets()
        return self.async_show_form(
            step_id="repair_date", errors=errors,
            data_schema=vol.Schema({vol.Required("date", default=(
                self._repair_day or dt_util.now().date() - timedelta(days=1)
            ).isoformat()): DateSelector()}),
        )

    async def async_step_repair_targets(self, user_input=None):
        from .manual_repair import repair_summary
        from .statistics_repair import replacement_plan

        if self._repair_analysis is None:
            return await self.async_step_repair_date()
        errors = {}
        if user_input is not None:
            try:
                self._repair_plan = replacement_plan(self._repair_analysis, user_input["panels"])
            except RecoveryError as err:
                self._repair_plan = None
                errors["base"] = str(err)
            else:
                return await self.async_step_repair_confirm()
        analysis = self._repair_analysis
        default = (user_input or {}).get("panels", self._repair_plan["selected"] if self._repair_plan else analysis["suggested_repair"])
        formatter = await async_formatter(self.hass, self.context)
        return self.async_show_form(
            step_id="repair_targets", errors=errors,
            data_schema=vol.Schema({vol.Required("panels", default=default): SelectSelector(
                SelectSelectorConfig(multiple=True, mode=SelectSelectorMode.DROPDOWN, options=[
                    {"value": panel, "label": str(details.get("position") or panel)}
                    for panel, details in sorted(analysis["panels"].items())
                ]),
            )}),
            description_placeholders=repair_summary(analysis, formatter),
        )

    async def async_step_repair_confirm(self, user_input=None):
        from .manual_repair import repair_summary

        if self._repair_plan is None:
            return await self.async_step_repair_date()
        errors = {}
        if user_input is not None:
            if user_input.get("edit_allocation"):
                return await self.async_step_repair_targets()
            if not user_input.get("confirm"):
                errors["base"] = "confirmation_required"
            else:
                coordinator = self._coordinator()
                if coordinator is None:
                    return self.async_abort(reason="integration_not_loaded")
                try:
                    await coordinator.async_repair_statistics(self._repair_plan)
                except RecoveryError as err:
                    errors["base"] = str(err)
                except TimeoutError:
                    errors["base"] = "repair_waiting"
                except OSError:
                    errors["base"] = "repair_save_failed"
                except SQLAlchemyError:
                    errors["base"] = "repair_database_error"
                else:
                    self._repair_plan = None
                    self._repair_analysis = None
                    return self.async_abort(reason="repair_completed")
        formatter = await async_formatter(self.hass, self.context)
        return self.async_show_form(
            step_id="repair_confirm", errors=errors,
            data_schema=vol.Schema({
                vol.Required("confirm", default=False): bool,
                vol.Optional("edit_allocation", default=False): bool,
            }),
            description_placeholders=repair_summary(self._repair_plan, formatter, self._repair_plan),
        )

    async def async_step_repair_undo(self, user_input=None):
        errors = {}
        coordinator = self._coordinator()
        if coordinator is None:
            return self.async_abort(reason="integration_not_loaded")
        try:
            backup = await coordinator.async_repair_backup()
            if backup is None:
                return self.async_abort(reason="repair_no_backup")
            if user_input is not None and user_input.get("confirm") and self._undo_token is not None:
                await coordinator.async_undo_statistics(self._undo_token)
                self._undo_token = None
                return self.async_abort(reason="repair_undone")
            if user_input is not None:
                errors["base"] = "confirmation_required"
            self._undo_token = backup["token"]
        except RecoveryError as err:
            return self.async_abort(reason=str(err))
        except TimeoutError:
            return self.async_abort(reason="repair_waiting")
        except OSError:
            return self.async_abort(reason="repair_save_failed")
        except SQLAlchemyError:
            return self.async_abort(reason="repair_database_error")
        return self.async_show_form(
            step_id="repair_undo", errors=errors,
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            description_placeholders={"date": backup["day"]},
        )
