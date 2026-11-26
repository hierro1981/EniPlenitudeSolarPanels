"""Native checkpoints and legacy raw-state fallback for cumulative energy."""

from __future__ import annotations

from dataclasses import dataclass
from math import isclose, isfinite

from homeassistant.components.sensor import SensorExtraStoredData

from .recovery import number


@dataclass
class ProductionCheckpoint(SensorExtraStoredData):
    """Keep live energy and its recovery reference together, even if unavailable."""

    live_energy_wh: float
    recovered_energy_wh: float
    last_sample_id: int | None

    def as_dict(self):
        return {
            **super().as_dict(),
            "live_energy_wh": self.live_energy_wh,
            "recovered_energy_wh": self.recovered_energy_wh,
            "last_sample_id": self.last_sample_id,
        }


def sample_id(value):
    """Read an optional integer sampling window without accepting fractions."""
    parsed = number(value)
    return int(parsed) if parsed is not None and parsed.is_integer() else None


def checkpoint_values(data):
    """Validate an internally consistent native checkpoint; never read a sum."""
    live = number(data.get("live_energy_wh"))
    recovered = number(data.get("recovered_energy_wh"))
    total = number(data.get("native_value"))
    if (live is None or recovered is None or total is None
        or data.get("native_unit_of_measurement") != "Wh"
        or not isclose(total, live + recovered, rel_tol=1e-12, abs_tol=1e-6)):
        return None
    return live, recovered, sample_id(data.get("last_sample_id"))


def state_values(state, attributes):
    """Migrate a valid legacy state, respecting displayed energy units."""
    value = number(state)
    factor = {"Wh": 1, "kWh": 1000, "MWh": 1000000}.get(attributes.get("unit_of_measurement", "Wh"))
    recovered = number(attributes.get("recovered_energy_wh", 0))
    if value is None or factor is None or recovered is None:
        return None
    value *= factor
    if not isfinite(value) or value < recovered - 1e-6:
        return None  # A broken reference cannot safely establish a live baseline.
    return max(0.0, value - recovered), recovered, sample_id(attributes.get("last_sample_id"))


def last_recorded_total(instance, entity_id):
    """Read bounded recent raw history, never a repaired normalized sum.

    If no valid state remains in this window, defer instead of inventing zero.
    """
    from homeassistant.components.recorder.history import get_last_state_changes

    rows = get_last_state_changes(instance.hass, 1000, entity_id).get(entity_id, [])
    for state in reversed(rows):
        if (values := state_values(state.state, state.attributes)) is not None:
            return values
    return None
