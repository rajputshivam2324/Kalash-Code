"""Theme configuration for the Kalash TUI."""

from __future__ import annotations

import os


class KalashTheme:
    """Theme settings respecting NO_COLOR and terminal capabilities."""

    def __init__(self) -> None:
        self.no_color = bool(os.environ.get("NO_COLOR"))
        self.high_contrast = bool(os.environ.get("KALASH_HIGH_CONTRAST"))

    @property
    def is_dark(self) -> bool:
        """Detect if terminal is using dark mode (default: True)."""
        return True
