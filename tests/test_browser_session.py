"""Verify browser identity and rejected-session handling without live credentials."""

import asyncio
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
import voluptuous as vol
import voluptuous_serialize
from multidict import CIMultiDict
from homeassistant.helpers import config_validation as cv

from custom_components.plenitude_panels.api import (
    PlenitudeApi,
    PlenitudeApiError,
    PlenitudeSessionRejectedError,
)
from custom_components.plenitude_panels.config_flow import (
    PlenitudePanelsConfigFlow,
    PlenitudePanelsOptionsFlow,
    _entry_data,
    _reauth_schema,
    _schema,
    _validate,
)
from custom_components.plenitude_panels.const import DEFAULT_USER_AGENT

DATA = {"cookie": "GAESA=private", "account_id": "account", "supply_id": "supply"}


def response(body, status=200, cookies=()):
    headers = CIMultiDict(("Set-Cookie", cookie) for cookie in cookies)
    result = SimpleNamespace(
        status=status, headers=headers, content_type="application/json",
        text=AsyncMock(return_value=json.dumps(body)),
    )
    return AsyncMock(__aenter__=AsyncMock(return_value=result))


@pytest.mark.parametrize(("ua", "version", "platform", "mobile"), [
    (DEFAULT_USER_AGENT, "156", "Windows", "?0"),
    ("Mozilla/5.0 (Linux; Android 14) Chrome/157.0.0.0 Mobile Safari/537.36", "157", "Android", "?1"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X) Chrome/154.0.0.0 Safari/537.36", "154", "macOS", "?0"),
])
def test_identity_headers_follow_browser(ua, version, platform, mobile):
    headers = PlenitudeApi(None, "cookie", "account", "supply", ua)._headers()
    assert headers["user-agent"] == ua
    assert f'"Chromium";v="{version}"' in headers["sec-ch-ua"]
    assert headers["sec-ch-ua-platform"] == f'"{platform}"'
    assert headers["sec-ch-ua-mobile"] == mobile


def test_non_chromium_browser_does_not_claim_chrome():
    ua = "Mozilla/5.0 (Macintosh) Version/18.0 Safari/605.1.15"
    headers = PlenitudeApi(None, "cookie", "account", "supply", ua)._headers()
    assert headers["user-agent"] == ua
    assert not any(key.startswith("sec-ch-ua") for key in headers)


def test_all_endpoints_use_identity_and_successful_cookie_rotation():
    async def run():
        ua = DEFAULT_USER_AGENT.replace("156.", "157.")
        session = Mock()
        session.get.return_value = response({}, cookies=["GAESA=renewed; Path=/"])
        session.request.side_effect = [
            response({"panels": [{"identifier": "one", "power": 400}]}),
            response({"panels": [{"identifier": "one", "production": 20}]}),
            response({"energyProducedConsumed": []}),
        ]
        api = PlenitudeApi(session, DATA["cookie"], "account", "supply", ua)
        await api.async_initialize_session()
        assert (await api.async_fetch_panels())[0]["production"] == 20
        await api.async_fetch_daily_production("POD", date(2026, 11, 1))
        calls = session.get.call_args_list + session.request.call_args_list
        assert len(calls) == 4
        assert all(call.kwargs["headers"]["user-agent"] == ua for call in calls)
        assert all('v="157"' in call.kwargs["headers"]["sec-ch-ua"] for call in calls)
        assert session.request.call_args_list[0].kwargs["headers"]["cookie"] == "GAESA=renewed"
        assert api.session_source_cookie == DATA["cookie"]
    asyncio.run(run())


@pytest.mark.parametrize("initialize", [True, False])
def test_406_diagnostics_do_not_expose_credentials_or_accept_rejected_cookies(initialize, caplog):
    async def run():
        session = Mock()
        rejected = response("private response body", 406, ["TS01dc4fc6=private-token; Path=/"])
        session.get.return_value = rejected
        session.request.return_value = rejected
        api = PlenitudeApi(session, DATA["cookie"], "account", "supply")
        with pytest.raises(PlenitudeSessionRejectedError):
            if initialize:
                await api.async_initialize_session()
            else:
                await api._json_request("GET", "https://example.test", "panel details")
        assert api.session_data["cookie"] == DATA["cookie"]
        assert not api.session_changed
    asyncio.run(run())
    assert "HTTP 406" in caplog.text
    assert "TS01dc4fc6" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.parametrize("initialize", [True, False])
def test_request_timeouts_become_connection_errors(initialize):
    async def run():
        session = Mock()
        session.get.side_effect = TimeoutError
        session.request.side_effect = TimeoutError
        api = PlenitudeApi(session, "cookie", "account", "supply")
        with pytest.raises(PlenitudeApiError, match="timed out"):
            if initialize:
                await api.async_initialize_session()
            else:
                await api._json_request("GET", "https://example.test", "panel details")
    asyncio.run(run())


def test_legacy_defaults_and_header_validation():
    assert _schema()(DATA)["user_agent"] == DEFAULT_USER_AGENT
    assert _entry_data(DATA)["user_agent"] == DEFAULT_USER_AGENT
    assert _reauth_schema()({"cookie": "new"})["user_agent"] == DEFAULT_USER_AGENT
    for schema, values in ((_schema(), DATA), (_reauth_schema(), {"cookie": "new"})):
        with pytest.raises(vol.Invalid):
            schema({**values, "user_agent": "   "})
        serialized = voluptuous_serialize.convert(schema, custom_serializer=cv.custom_serializer)
        assert next(field for field in serialized if field["name"] == "user_agent")["type"] == "string"
    with pytest.raises(PlenitudeApiError, match="single header"):
        PlenitudeApi(None, "cookie", "account", "supply", "Chrome/156\r\nInjected: header")


def test_validation_passes_configured_identity():
    async def run():
        api = SimpleNamespace(
            async_initialize_session=AsyncMock(), async_fetch_panels=AsyncMock(),
            session_data={"cookie": "renewed"},
        )
        with (
            patch("custom_components.plenitude_panels.config_flow.async_get_clientsession"),
            patch("custom_components.plenitude_panels.config_flow.PlenitudeApi", return_value=api) as client,
        ):
            assert await _validate(Mock(), {**DATA, "user_agent": "custom browser"}) == {"cookie": "renewed"}
            assert client.call_args.kwargs["user_agent"] == "custom browser"
    asyncio.run(run())


@pytest.mark.parametrize("step", ["user", "settings", "reauth_confirm"])
@pytest.mark.parametrize(("error", "message"), [
    (PlenitudeSessionRejectedError(), "session_rejected"),
    (TimeoutError(), "cannot_connect"),
])
def test_failed_connection_forms_keep_submitted_identity(step, error, message):
    async def run():
        entry = SimpleNamespace(data=DATA)
        if step == "settings":
            flow = PlenitudePanelsOptionsFlow(entry)
        else:
            flow = PlenitudePanelsConfigFlow()
            flow._reauth_entry = entry
            flow._validate = AsyncMock(side_effect=error)
        flow.hass = Mock()
        submitted = {**DATA, "cookie": "fresh cookie", "user_agent": "fresh browser"}
        with patch("custom_components.plenitude_panels.config_flow._validate", side_effect=error):
            result = await getattr(flow, f"async_step_{step}")(submitted)
        assert result["errors"] == {"base": message}
        defaults = result["data_schema"]({})
        assert defaults["cookie"] == "fresh cookie"
        assert defaults["user_agent"] == "fresh browser"
    asyncio.run(run())


@pytest.mark.parametrize("step", ["settings", "reauth_confirm"])
def test_successful_connection_persists_identity_and_rotated_cookie(step):
    async def run():
        entry = SimpleNamespace(data=DATA, entry_id="entry", unique_id="account_supply")
        flow = (
            PlenitudePanelsOptionsFlow(entry) if step == "settings"
            else PlenitudePanelsConfigFlow()
        )
        flow._reauth_entry = entry
        flow.hass = Mock()
        flow.hass.config_entries.async_entries.return_value = [entry]
        flow.hass.config_entries.async_reload = AsyncMock()
        flow._validate = AsyncMock(return_value={"cookie": "renewed"})
        submitted = {**DATA, "user_agent": "custom browser"}
        with patch(
            "custom_components.plenitude_panels.config_flow._validate",
            return_value={"cookie": "renewed"},
        ):
            await getattr(flow, f"async_step_{step}")(submitted)
        saved = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
        assert saved["user_agent"] == "custom browser"
        assert saved["cookie"] == "renewed"
        assert saved["account_id"] == DATA["account_id"]
        flow.hass.config_entries.async_reload.assert_awaited_once_with("entry")
    asyncio.run(run())
