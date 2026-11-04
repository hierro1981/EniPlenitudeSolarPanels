"""Load the custom component alongside the installed Home Assistant package."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Home Assistant 2026 installs its voluptuous-compatible validator on import.
# Load it before test modules import voluptuous so they share the same classes.
import homeassistant  # noqa: E402,F401
