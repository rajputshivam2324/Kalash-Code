"""Shared fixtures for Kalash test suite."""

import sys
from pathlib import Path

# Ensure the source is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
