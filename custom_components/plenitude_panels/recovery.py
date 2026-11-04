"""Persistent daily production reconciliation, independent of live sensor totals."""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from math import isfinite
from typing import Any

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import PlenitudeApiError, PlenitudeAuthError
from .const import (
    CONF_ACCOUNT_ID,
    CONF_POD,
    CONF_RECOVERY_ENABLED,
    CONF_SUPPLY_ID,
    DOMAIN,
    FIVE_MINUTE_HOUR_FRACTION,
)

_LOGGER = logging.getLogger(__name__)


def number(value: Any) -> float | None:
    """Read finite, nonnegative API values, including Italian decimals."""
    try:
        if isinstance(value, str) and "," in value:
            value = value.replace(".", "").replace(",", ".")
        result = float(value)
        return result if isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None


def production(value: Any) -> float | None:
    """Clamp finite negative production to zero; keep missing/invalid data missing."""
    try:
        if isinstance(value, str) and "," in value:
            value = value.replace(".", "").replace(",", ".")
        result = float(value)
        if not isfinite(result):
            return None
        if result < 0:
            _LOGGER.debug("Ignoring negative production sample: %s", result)
        return max(0.0, result)
    except (TypeError, ValueError):
        return None


def day_bounds(day: date, zone) -> tuple[int, int]:
    """Return UTC epoch bounds of a local calendar day, including DST."""
    return (
        int(datetime.combine(day, time(), zone).timestamp()),
        int(datetime.combine(day + timedelta(days=1), time(), zone).timestamp()),
    )


def aggregate_intervals(payload: dict, day: date, zone) -> dict[int, float]:
    """Validate a complete local day without silently accepting missing quarters."""
    factor = {"Wh": 1, "kWh": 1000}.get(payload.get("measureUnit"))
    rows = payload.get("energyProducedConsumed")
    if factor is None or not isinstance(rows, list):
        raise ValueError("invalid aggregate energy unit or series")
    start, end = day_bounds(day, zone)
    expected = set(range(start, end, 900))
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("invalid quarter-hour row")
        stamp = dt_util.parse_datetime(str(row.get("timeUnit", "")))
        energy = production(row.get("energyProduced"))
        if stamp is None or energy is None:
            raise ValueError("invalid quarter-hour timestamp or energy")
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=zone)
            # A naive repeated hour cannot identify the two DST folds safely.
            if stamp.utcoffset() != stamp.replace(fold=1).utcoffset():
                raise ValueError("ambiguous daylight-saving timestamp")
        timestamp = int(stamp.timestamp())
        if timestamp not in expected or timestamp in result:
            raise ValueError("duplicate or misaligned quarter-hour timestamp")
        result[timestamp] = energy * factor
    if set(result) != expected:
        raise ValueError("aggregate day is incomplete")
    return result


def reconstruct(
    samples: dict, powers: dict, aggregate: dict[int, float]
) -> dict[str, dict[int, float]]:
    """Allocate only positive residuals to panels with no positive daily samples."""
    panel_ids = set(powers)
    if not panel_ids or any(set(sample) != panel_ids for sample in samples.values()):
        raise ValueError("panel inventory changed during the day")
    zero_panels = {
        panel for panel in panel_ids
        if not any((sample.get(panel) or 0) > 0 for sample in samples.values())
    }
    if not zero_panels:
        return {}
    weights = {panel: number(powers[panel]) for panel in zero_panels}
    if not all(weights.values()):
        weights = dict.fromkeys(zero_panels, 1.0)
    weight_sum = sum(weights.values())
    recovered: dict[str, dict[int, float]] = {panel: {} for panel in zero_panels}
    for quarter, total in sorted(aggregate.items()):
        if total == 0:
            continue
        windows = [samples.get(str((quarter + offset) // 300)) for offset in (0, 300, 600)]
        if any(window is None for window in windows):
            raise ValueError("missing live samples during aggregate production")
        known = 0.0
        for panel in panel_ids - zero_panels:
            values = [window.get(panel) for window in windows]
            if any(value is None for value in values):
                raise ValueError("incomplete production on a nonzero panel")
            known += sum(values)
        residual = total - known
        if residual < -0.1:
            _LOGGER.debug("Aggregate energy below live energy at %s: %s Wh", quarter, residual)
        if residual <= 0:
            continue
        for panel in zero_panels:
            recovered[panel][quarter] = residual * weights[panel] / weight_sum
    return {panel: intervals for panel, intervals in recovered.items() if intervals}


class ProductionRecovery:
    """Keep a bounded live history and a durable, idempotent recovery ledger."""

    def __init__(self, hass, entry, api) -> None:
        self.hass = hass
        self.entry = entry
        self.api = api
        identity = f"{entry.data[CONF_ACCOUNT_ID]}:{entry.data[CONF_SUPPLY_ID]}"
        digest = sha256(identity.encode()).hexdigest()[:16]
        self.store = Store(hass, 1, f"{DOMAIN}.recovery.{entry.entry_id}.{digest}")
        self.state: dict = {
            "days": {}, "totals": {}, "pending": [], "last_days": {}, "allocations": {},
        }
        self.entities: dict[str, str] = {}
        self._last_attempt: datetime | None = None
        self.status = "starting"

    async def async_load(self) -> None:
        if stored := await self.store.async_load():
            self.state = stored
        if "allocations" not in self.state:
            self.state["allocations"] = {}
            # Unfinished 0.7 recoveries still contain their original intervals.
            for record in self.state["pending"]:
                self._remember_allocation(record["day"], record["panel"], record["intervals"])
        pod = self.entry.data.get(CONF_POD, "")
        if self.state.get("pod") and pod and self.state["pod"] != pod:
            self.state["days"] = {}
        if pod:
            self.state["pod"] = pod

    def total(self, panel: str) -> float:
        return self.state["totals"].get(panel, 0.0)

    def _remember_allocation(self, day: str, panel: str, intervals: dict) -> None:
        allocations = self.state["allocations"].setdefault(day, {})
        allocation = allocations.setdefault(panel, {"total": 0.0, "intervals": {}})
        for stamp, value in intervals.items():
            key = str(stamp)
            allocation["intervals"][key] = allocation["intervals"].get(key, 0) + value
            allocation["total"] += value

    async def async_allocate(self, day: str, allocation: dict, now: datetime) -> None:
        """Commit totals, per-day allocations and pending statistics together."""
        before = deepcopy(self.state)
        try:
            for panel, intervals in sorted(allocation.items()):
                if not intervals:
                    continue
                target = self.total(panel) + sum(intervals.values())
                self.state["totals"][panel] = target
                self.state["last_days"][panel] = day
                self._remember_allocation(day, panel, intervals)
                self.state["pending"].append({
                    "panel": panel, "day": day, "intervals": intervals,
                    "target": target, "created": now.timestamp(),
                    "day_bounds": day_bounds(date.fromisoformat(day), dt_util.get_default_time_zone()),
                })
            if day in self.state["days"]:
                self.state["days"][day]["done"] = True
            await self.store.async_save(self.state)
        except Exception:
            self.state = before
            raise

    async def async_sample(self, panels: list[dict], sample_id: int, now: datetime) -> None:
        """Persist live samples before exposing any recovered cumulative increase."""
        local_day = dt_util.as_local(now).date()
        # The instantaneous API describes the *last* five minutes. Its reading
        # at midnight therefore belongs to the previous calendar day.
        interval_id = sample_id - 1
        interval_start = datetime.fromtimestamp(interval_id * 300, UTC)
        key = dt_util.as_local(interval_start).date().isoformat()
        days = self.state["days"]
        day = days.setdefault(key, {
            "samples": {}, "powers": {}, "done": key in self.state["allocations"],
        })
        values = {}
        for panel in panels:
            identifier = panel["identifier"]
            power = production(panel.get("instantProductionWh"))
            values[identifier] = power * FIVE_MINUTE_HOUR_FRACTION if power is not None else None
            day["powers"][identifier] = panel.get("power")
        previous = day["samples"].get(str(interval_id))
        if previous is None:
            day["samples"][str(interval_id)] = values
        else:
            # Match cumulative sensors: accept the first valid value per window,
            # allowing an initially missing reading to become available later.
            for identifier, value in values.items():
                if previous.get(identifier) is None:
                    previous[identifier] = value
        cutoff = (local_day - timedelta(days=7)).isoformat()
        self.state["days"] = {key: value for key, value in days.items() if key >= cutoff}
        enabled = self.entry.data.get(CONF_RECOVERY_ENABLED, True)
        pod = self.entry.data.get(CONF_POD, "")
        self.status = "disabled" if not enabled else "pod_required" if not pod else "monitoring"
        if enabled and pod and (
            self._last_attempt is None or now - self._last_attempt >= timedelta(hours=1)
        ):
            self._last_attempt = now
            for day_key, history in sorted(self.state["days"].items()):
                if day_key >= local_day.isoformat() or history["done"]:
                    continue
                try:
                    requested_day = date.fromisoformat(day_key)
                    payload = await self.api.async_fetch_daily_production(pod, requested_day)
                    aggregate = aggregate_intervals(
                        payload, requested_day, dt_util.get_default_time_zone()
                    )
                    # An all-zero response can still be a delayed publication: retry it.
                    if not any(aggregate.values()):
                        self.status = "waiting_for_aggregate"
                        continue
                    allocation = reconstruct(history["samples"], history["powers"], aggregate)
                except PlenitudeAuthError:
                    raise
                except (PlenitudeApiError, TimeoutError, TypeError, ValueError) as err:
                    self.status = "waiting_for_complete_data"
                    _LOGGER.debug("Production recovery deferred for %s: %s", day_key, err)
                    continue
                # A later day's authentication failure must not leave an
                # allocated total visible in memory without its durable ledger.
                await self.async_allocate(day_key, allocation, now)
                self.status = "reconciled"
        await self.store.async_save(self.state)

    async def async_correct_statistics(self) -> None:
        """Finish durable pending corrections, even if new recovery is disabled."""
        if "recorder" not in self.hass.config.components:
            return
        if self.state.get("repair_backup"):
            from .manual_repair import async_reconcile_journal

            try:
                await async_reconcile_journal(self.hass, self)
            except Exception:
                _LOGGER.exception("Unable to reconcile statistics repair; retaining pending corrections")
                return
        for record in list(self.state["pending"]):
            entity_id = self.entities.get(record["panel"])
            if entity_id is None:
                continue
            try:
                from .recovery_statistics import async_move_recovery

                done = await async_move_recovery(self.hass, self.entry.entry_id, entity_id, record)
            except Exception:  # Recorder failures must not interrupt live production.
                _LOGGER.exception("Unable to correct recovered production statistics")
                continue
            if done:
                self.state["pending"].remove(record)
                await self.store.async_save(self.state)
