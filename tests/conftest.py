"""Load the custom component alongside the installed Home Assistant package."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
