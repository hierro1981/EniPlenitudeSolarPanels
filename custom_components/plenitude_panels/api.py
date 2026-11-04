"""API client for Plenitude Solar Panels."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from secrets import choice
from string import ascii_letters, digits
from typing import Any
from urllib.parse import quote

import aiohttp

from .const import (
    BASE_URL,
    CONF_COOKIE,
    DEFAULT_USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)


class PlenitudeApiError(Exception):
    """Base exception for a Plenitude API error."""


class PlenitudeAuthError(PlenitudeApiError):
    """Raised when Plenitude credentials are invalid or expired."""


class PlenitudeSessionRejectedError(PlenitudeAuthError):
    """HTTP 406 rejects the request without proving the cookie has expired."""


class PlenitudeApi:
    """Client which retrieves and refreshes the Plenitude panel session."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        cookie: str,
        account_id: str,
        supply_id: str,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._session = session
        self._cookie = cookie
        self._account_id = account_id
        self._supply_id = supply_id
        if "\r" in user_agent or "\n" in user_agent:
            raise PlenitudeApiError("User-Agent must be a single header value")
        self._user_agent = user_agent.strip() or DEFAULT_USER_AGENT
        self._session_changed = False
        self._session_source_cookie: str | None = None

    @property
    def session_data(self) -> dict[str, str]:
        """Return updated session values suitable for ConfigEntry persistence."""
        return {
            CONF_COOKIE: self._cookie,
        }

    @property
    def session_changed(self) -> bool:
        """Whether a renewed session still needs persistence."""
        return self._session_changed

    def mark_session_persisted(self) -> None:
        """Mark the in-memory credentials as persisted."""
        self._session_changed = False
        self._session_source_cookie = None

    @property
    def session_source_cookie(self) -> str | None:
        """Return the cookie from which the pending GAESA rotation started."""
        return self._session_source_cookie

    @staticmethod
    def _merge_cookies(cookie: str, updates: dict[str, str | None]) -> str:
        cookies: dict[str, str] = {}
        for item in cookie.split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name:
                cookies[name] = value
        for name, value in updates.items():
            if value is None:
                cookies.pop(name, None)
            else:
                cookies[name] = value
        return "; ".join(f"{name}={value}" for name, value in cookies.items())

    @staticmethod
    def _cookie_is_deleted(attributes: dict[str, str]) -> bool:
        """Return whether a Set-Cookie instruction removes a cookie."""
        max_age = attributes.get("max-age", "")
        if max_age:
            try:
                return int(max_age) <= 0
            except ValueError:
                pass
        expires = attributes.get("expires", "")
        if expires:
            try:
                return parsedate_to_datetime(expires).astimezone(UTC) <= datetime.now(UTC)
            except (TypeError, ValueError):
                pass
        return False

    @classmethod
    def _set_cookie_updates(
        cls, response: aiohttp.ClientResponse
    ) -> tuple[dict[str, str | None], int]:
        """Read raw Set-Cookie headers without rejecting nonstandard Expires dates."""
        updates: dict[str, str | None] = {}
        parse_failures = 0
        for header in response.headers.getall("Set-Cookie", []):
            pair, _, attributes_text = header.partition(";")
            name, separator, value = pair.strip().partition("=")
            if not separator or not name:
                parse_failures += 1
                continue
            attributes: dict[str, str] = {}
            for attribute in attributes_text.split(";"):
                attribute_name, attribute_separator, attribute_value = attribute.strip().partition("=")
                if attribute_name:
                    attributes[attribute_name.lower()] = (
                        attribute_value if attribute_separator else ""
                    )
            updates[name] = None if cls._cookie_is_deleted(attributes) else value
        return updates, parse_failures

    def _log_cookie_response(
        self,
        endpoint: str,
        response: aiohttp.ClientResponse,
        updates: dict[str, str | None],
        parse_failures: int,
    ) -> None:
        """Log safe cookie-change metadata for successful and failed requests."""
        changed = sorted(updates)
        removed = sorted(name for name, value in updates.items() if value is None)
        _LOGGER.debug(
            "Plenitude %s response: HTTP %s, Set-Cookie names: %s, "
            "parsed cookie names: %s, removed cookie names: %s, parse failures: %s",
            endpoint,
            response.status,
            self._set_cookie_names(response),
            changed,
            removed,
            parse_failures,
        )

    def _apply_cookie_updates(self, updates: dict[str, str | None]) -> None:
        """Apply browser cookie changes while retaining the original persistence guard."""
        updated_cookie = self._merge_cookies(self._cookie, updates)
        if updated_cookie == self._cookie:
            return
        if not self._session_changed:
            self._session_source_cookie = self._cookie
        self._cookie = updated_cookie
        self._session_changed = True

    @staticmethod
    def _set_cookie_names(response: aiohttp.ClientResponse) -> list[str]:
        """Return Set-Cookie names without exposing their sensitive values."""
        names = []
        for header in response.headers.getall("Set-Cookie", []):
            name, separator, _ = header.split(";", 1)[0].partition("=")
            if separator:
                names.append(name)
        return names

    @staticmethod
    def _log_hash() -> str:
        """Generate the 22-character alphanumeric value used by the portal."""
        return "".join(choice(ascii_letters + digits) for _ in range(22))

    def _headers(self) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
            "cache-control": "no-cache, no-store",
            "cookie": self._cookie,
            "pragma": "no-cache",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": self._user_agent,
        }
        chromium = re.search(r"(?:Chrome|Chromium)/(\d+)", self._user_agent)
        # Safari and Firefox do not send Chromium client hints.
        if chromium:
            version = chromium.group(1)
            edge = re.search(r"Edg(?:A)?/(\d+)", self._user_agent)
            brand, brand_version = (
                ("Microsoft Edge", edge.group(1)) if edge else ("Google Chrome", version)
            )
            headers["sec-ch-ua"] = (
                f'"{brand}";v="{brand_version}", "Chromium";v="{version}", '
                '"Not(A:Brand";v="24"'
            )
            headers["sec-ch-ua-mobile"] = "?1" if "Mobile" in self._user_agent else "?0"
            for marker, platform in (
                ("Android", "Android"), ("Windows", "Windows"),
                ("CrOS", "Chrome OS"), ("Macintosh", "macOS"), ("Linux", "Linux"),
            ):
                if marker in self._user_agent:
                    headers["sec-ch-ua-platform"] = f'"{platform}"'
                    break
        return headers

    def _check_session_response(
        self, endpoint: str, response: aiohttp.ClientResponse, body: str
    ) -> None:
        """Diagnose rejected requests without logging credentials or response bodies."""
        if response.status == 406:
            _LOGGER.warning(
                "Plenitude %s rejected the request: HTTP 406, content type: %s, "
                "Set-Cookie names: %s. Cookie expiry is not confirmed; verify Cookie "
                "and User-Agent from the same browser session. Server protection "
                "may also reject the client",
                endpoint, response.content_type, self._set_cookie_names(response),
            )
            raise PlenitudeSessionRejectedError("Plenitude rejected the browser request (HTTP 406)")
        if self._is_session_error(response.status, body):
            raise PlenitudeAuthError("Plenitude session rejected")

    async def _json_request(
        self, method: str, url: str, endpoint: str, **kwargs: Any
    ) -> Any:
        try:
            async with self._session.request(
                method,
                url,
                timeout=aiohttp.ClientTimeout(total=30),
                **kwargs,
            ) as response:
                body = await response.text()
                updates, parse_failures = self._set_cookie_updates(response)
                self._log_cookie_response(endpoint, response, updates, parse_failures)
                self._check_session_response(endpoint, response, body)
                if response.status >= 400:
                    raise PlenitudeApiError(f"Plenitude returned HTTP {response.status}")
                self._apply_cookie_updates(updates)
                try:
                    return json.loads(body)
                except ValueError as err:
                    raise PlenitudeApiError("Plenitude returned invalid JSON") from err
        except aiohttp.ClientError as err:
            raise PlenitudeApiError("Unable to connect to Plenitude") from err
        except TimeoutError as err:
            raise PlenitudeApiError(f"Plenitude {endpoint} request timed out") from err

    @staticmethod
    def _is_session_error(status: int, body: str) -> bool:
        """Identify the session-expiry responses returned by the portal."""
        return status in (401, 403, 406) or any(
            marker in body.lower() for marker in ("jwe scaduto", "request timeout")
        )

    async def async_initialize_session(self) -> None:
        """Apply cookie updates from c360/init using the active browser session."""
        log_hash = self._log_hash()
        url = f"{BASE_URL}/serviceDAp/api/c360/init?logHash={log_hash}&channel=PORTAL"
        headers = {**self._headers(), "referer": f"{BASE_URL}/my-eni/home"}
        try:
            async with self._session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                body = await response.text()
                updates, parse_failures = self._set_cookie_updates(response)
                self._log_cookie_response("c360/init", response, updates, parse_failures)
                self._check_session_response("c360/init", response, body)
                if response.status >= 400:
                    _LOGGER.warning(
                        "Plenitude c360/init failed: HTTP %s, content type: %s, "
                        "Set-Cookie names: %s",
                        response.status,
                        response.content_type,
                        self._set_cookie_names(response),
                    )
                    raise PlenitudeApiError(f"Session refresh returned HTTP {response.status}")
        except aiohttp.ClientError as err:
            raise PlenitudeApiError("Unable to refresh Plenitude session") from err
        except TimeoutError as err:
            raise PlenitudeApiError("Plenitude c360/init request timed out") from err

        if not updates:
            _LOGGER.debug("Plenitude c360/init completed without updating cookies")
            return
        self._apply_cookie_updates(updates)

    def _endpoint_url(self, endpoint: str) -> str:
        account_id = quote(self._account_id, safe="")
        supply_id = quote(self._supply_id, safe="")
        return (
            f"{BASE_URL}/serviceDAp/api/pannelli-services/v1/accounts/{account_id}"
            f"/supply/{supply_id}/{endpoint}?logHash={self._log_hash()}&channel=PORTAL"
        )

    async def _async_fetch_panels_once(self) -> list[dict[str, Any]]:
        details = await self._json_request(
            "GET",
            self._endpoint_url("details"),
            "panel details",
            headers={**self._headers(), "referer": f"{BASE_URL}/my-eni/home"},
        )
        if not isinstance(details, dict) or not isinstance(details.get("panels"), list):
            raise PlenitudeApiError("Details response does not contain panels")

        requested_panels = [
            {key: panel.get(key) for key in ("identifier", "position", "power")}
            for panel in details["panels"]
            if panel.get("identifier")
        ]
        production = await self._json_request(
            "POST",
            self._endpoint_url("instant-production"),
            "instant production",
            headers={
                **self._headers(),
                "content-type": "application/json",
                "referer": f"{BASE_URL}/my-eni/home",
            },
            json=requested_panels,
        )
        production_panels = production.get("panels", []) if isinstance(production, dict) else production
        if not isinstance(production_panels, list):
            raise PlenitudeApiError("Production response does not contain a panel list")
        production_by_identifier = {
            panel.get("identifier"): panel
            for panel in production_panels
            if isinstance(panel, dict) and panel.get("identifier")
        }
        return [
            {**panel, **production_by_identifier.get(panel["identifier"], {})}
            for panel in details["panels"]
            if panel.get("identifier")
        ]

    async def async_fetch_panels(self) -> list[dict[str, Any]]:
        """Fetch panel metadata and production, retrying once after session renewal."""
        try:
            return await self._async_fetch_panels_once()
        except PlenitudeAuthError:
            _LOGGER.debug("Plenitude session rejected; initializing it")
            await self.async_initialize_session()
            return await self._async_fetch_panels_once()

    async def async_fetch_daily_production(self, pod: str, day: date) -> dict[str, Any]:
        """Fetch the portal's aggregate quarter-hour energy for a completed day."""
        url = (
            f"{BASE_URL}/serviceDAp/api/pannelli-services/v1/accounts/"
            f"{quote(self._account_id, safe='')}/pod/{quote(pod, safe='')}"
            "/productions-consumes"
        )
        params = {
            "granularity": "QUARTER_HOURS",
            "start-date": f"{day.isoformat()}T00:00:00.000Z",
            "end-date": f"{day.isoformat()}T00:00:00.000Z",
            "logHash": self._log_hash(),
            "channel": "PORTAL",
        }
        for attempt in range(2):
            try:
                result = await self._json_request(
                    "GET", url, "daily aggregate production", params=params,
                    headers={**self._headers(), "referer": f"{BASE_URL}/my-eni/home"},
                )
                if not isinstance(result, dict):
                    raise PlenitudeApiError("Invalid aggregate production response")
                return result
            except PlenitudeAuthError:
                if attempt:
                    raise
                await self.async_initialize_session()
        raise PlenitudeApiError("Unable to fetch aggregate production")
