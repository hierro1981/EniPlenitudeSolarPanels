"""Read the requesting user's locale and render compact recovery summaries."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, localcontext
from math import isfinite

from babel import Locale, UnknownLocaleError
from babel.numbers import format_decimal
from homeassistant.helpers.http import current_request


def _browser_languages(header: str) -> list[str]:
    """Honor the browser's language priorities without using the server OS locale."""
    languages = []
    for part in header.split(","):
        language, *parameters = part.strip().split(";")
        if not language or language == "*":
            continue
        try:
            quality = next((float(value.strip()[2:]) for value in parameters
                            if value.strip().startswith("q=")), 1.0)
        except ValueError:
            continue
        if 0 < quality <= 1:
            languages.append((quality, language))
    return [language for _, language in sorted(languages, key=lambda item: -item[0])]


@dataclass(frozen=True)
class RecoveryFormatter:
    """Format labels only: retain full-precision numbers for all calculations."""

    locale: Locale

    def number(self, value, *, precision=3) -> str:
        if value is None or not isfinite(float(value)):
            return "—"
        # Preserve the locale's primary/secondary grouping (including e.g. India).
        integer_pattern = self.locale.decimal_formats[None].pattern.split(";")[0].split(".")[0]
        with localcontext() as context:
            context.rounding = ROUND_HALF_UP
            return format_decimal(
                value, format=integer_pattern + ("." + "0" * precision if precision else ""), locale=self.locale,
            )


def _build_formatter(browser: list[str], fallback: str) -> RecoveryFormatter:
    for candidate in [*browser, fallback, "en_US"]:
        if not isinstance(candidate, str) or not candidate:
            continue
        try:
            locale = Locale.parse(candidate.replace("-", "_"))
        except (UnknownLocaleError, ValueError):
            continue
        formatter = RecoveryFormatter(locale)
        # Babel lazily reads its locale data. Warm it in the executor, never on
        # Home Assistant's event loop when subsequently rendering the labels.
        formatter.number(0)
        return formatter
    raise ValueError("No valid numeric locale")


async def async_formatter(hass) -> RecoveryFormatter:
    """Use the request's languages, falling back to Home Assistant's language."""
    request = current_request.get(None)
    header = request.headers.get("Accept-Language", "") if request is not None else ""
    fallback = getattr(hass.config, "language", None) or "en"
    return await hass.async_add_executor_job(
        _build_formatter, _browser_languages(header), fallback,
    )


def _recorded_value(item: dict, formatter: RecoveryFormatter) -> str:
    if item.get("value") is not None:
        return formatter.number(item["value"])
    if item.get("observed") is not None:
        return f"≥ {formatter.number(item['observed'])}"
    return "—"


def summary(data: dict, formatter: RecoveryFormatter, allocation: dict | None = None) -> dict:
    """Supply translated forms with numbers and Markdown rows, not English labels."""
    known = data["known"]
    recorded = data.get("recorded", {})
    previous = sum(value["total"] for value in data["prior"].values())
    aggregate = sum(data["aggregate"].values())
    if known is not None:
        known_display = formatter.number(known)
    else:
        minimum = sum(item["value"] if item.get("value") is not None else item.get("observed") or 0
                      for item in recorded.values())
        known_display = f"≥ {formatter.number(minimum)}" if minimum > 0 else "—"
    rows = []
    for panel, details in sorted(data["panels"].items()):
        name = str(details.get("position") or panel).replace("|", "\\|").replace("\n", " ")
        original = _recorded_value(recorded.get(panel, {}), formatter)
        recovered = formatter.number(data["prior"].get(panel, {}).get("total", 0))
        addition = formatter.number(sum(allocation.get(panel, {}).values())) if allocation is not None else "—"
        rows.append(f"| {name} | {original} | {recovered} | {addition} |")
    remaining = max(0, aggregate - known - previous) if known is not None else None
    additional = sum(sum(values.values()) for values in allocation.values()) if allocation is not None else None
    return {
        "date": data["day"], "aggregate": formatter.number(aggregate),
        "known": known_display, "previous": formatter.number(previous),
        "remaining": formatter.number(remaining), "additional": formatter.number(additional),
        "panels": "\n".join(rows),
    }
