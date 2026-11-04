"""Profile-aware numeric formatting and readable per-panel recovery previews."""

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from custom_components.plenitude_panels.recovery_presentation import (
    _browser_languages,
    _build_formatter,
    async_formatter,
    summary,
)
from homeassistant.helpers.http import current_request


@pytest.fixture
def user_store_module(monkeypatch):
    """Isolate profile storage I/O from unrelated frontend/onboarding platforms."""
    module = ModuleType("homeassistant.components.frontend.storage")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module


@pytest.mark.parametrize("preferences,browser,fallback,expected", [
    ({"language": "it"}, [], "en", "6.973,750"),
    ({"language": "en"}, [], "it", "6,973.750"),
    ({"language": "fr"}, [], "it", "6\u202f973,750"),
    ({"language": "en", "number_format": "decimal_comma"}, [], "en", "6.973,750"),
    ({"language": "it", "number_format": "comma_decimal"}, [], "it", "6,973.750"),
    ({"language": "en", "number_format": "space_comma"}, [], "en", "6\u202f973,750"),
    ({"language": "it", "number_format": "quote_decimal"}, [], "it", "6\u2019973.750"),
    ({"language": "it", "number_format": "none"}, [], "it", "6973.750"),
    ({"language": "it", "number_format": "system"}, ["en-US"], "it", "6,973.750"),
    ({"language": "not-a-locale"}, [], "it", "6.973,750"),
    ({}, ["it-IT"], "en", "6.973,750"),
    ({}, [], "fr", "6\u202f973,750"),
])
def test_number_preferences_override_interface_language(preferences, browser, fallback, expected):
    assert _build_formatter(preferences, browser, fallback).number(6973.75) == expected


def test_grouping_rounding_and_unavailable_values():
    formatter = _build_formatter({"language": "en-IN"}, [], "en")
    assert formatter.number(1234567.89) == "12,34,567.890"
    assert formatter.number(1.2345) == "1.235"  # Intl's default half-expand rounding.
    assert formatter.number(None) == "—"
    assert formatter.number(float("nan")) == "—"
    assert formatter.number(0) == "0.000"


def test_browser_language_quality_order():
    assert _browser_languages("en-US;q=0.5,it-IT;q=1,fr;q=0,*;q=0.8,de;q=oops") == ["it-IT", "en-US"]


def test_current_http_user_profile_is_used_without_context_user_id(user_store_module):
    async def run():
        class Request(dict):
            pass

        request = Request(hass_user=SimpleNamespace(id="current_user"))
        request.headers = {"Accept-Language": "en-US,en;q=0.9"}
        hass = SimpleNamespace(
            config=SimpleNamespace(language="en"),
            async_add_executor_job=AsyncMock(side_effect=lambda func, *args: func(*args)),
        )
        store = SimpleNamespace(data={"language": {"language": "en", "number_format": "decimal_comma"}})
        load = user_store_module.async_user_store = AsyncMock(return_value=store)
        token = current_request.set(request)
        try:
            formatter = await async_formatter(hass, {})
            assert formatter.number(6973.75) == "6.973,750"
            load.assert_awaited_once_with(hass, "current_user")
        finally:
            current_request.reset(token)
        hass.async_add_executor_job.assert_awaited_once()
    asyncio.run(run())


def test_profile_unavailable_uses_browser_language_without_failing_flow(user_store_module):
    async def run():
        class Request(dict):
            pass

        request = Request(hass_user=SimpleNamespace(id="current_user"))
        request.headers = {"Accept-Language": "fr-FR,en;q=0.8"}
        hass = SimpleNamespace(
            config=SimpleNamespace(language="en"),
            async_add_executor_job=AsyncMock(side_effect=lambda func, *args: func(*args)),
        )
        user_store_module.async_user_store = AsyncMock(side_effect=OSError)
        token = current_request.set(request)
        try:
            formatter = await async_formatter(hass, {})
            assert formatter.number(6973.75) == "6\u202f973,750"
        finally:
            current_request.reset(token)
    asyncio.run(run())


def test_summary_distinguishes_zero_partial_unknown_and_allocation():
    data = {
        "day": "2026-10-27", "known": None, "aggregate": {0: 6973.75}, "prior": {},
        "panels": {"a": {"position": "Assemini"}, "b": {"position": "Murcia"},
                   "c": {"position": "Partial"}, "d": {"position": "Unknown | panel"}},
        "recorded": {
            "a": {"value": 0, "observed": None},
            "b": {"value": 3530, "observed": 3530},
            "c": {"value": None, "observed": 10},
            "d": {"value": None, "observed": None},
        },
    }
    formatter = _build_formatter({"language": "it"}, [], "en")
    result = summary(data, formatter)
    assert result["aggregate"] == "6.973,750"
    assert result["known"] == "≥ 3.540,000"
    assert result["remaining"] == "—"
    assert "| Assemini | 0,000 | 0,000 | — |" in result["panels"]
    assert "| Murcia | 3.530,000 | 0,000 | — |" in result["panels"]
    assert "| Partial | ≥ 10,000 |" in result["panels"]
    assert "| Unknown \\| panel | — |" in result["panels"]
    preview = summary({**data, "known": 3540}, formatter, {"a": {0: 3433.75}})
    assert preview["additional"] == "3.433,750"
    assert "| Assemini | 0,000 | 0,000 | 3.433,750 |" in preview["panels"]
    assert "| Murcia | 3.530,000 | 0,000 | 0,000 |" in preview["panels"]
