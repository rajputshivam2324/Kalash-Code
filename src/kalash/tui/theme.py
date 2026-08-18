"""Theme configuration for the Kalash TUI.

OpenCode-style terminal UX needs readable contrast on every surface. Textual's
default ``$text-muted`` on dark backgrounds is often invisible on real
terminals, so Kalash ships an explicit palette and wires it into the app CSS.
"""

from __future__ import annotations

import os


class KalashTheme:
    """Theme settings respecting NO_COLOR and high-contrast mode."""

    def __init__(self) -> None:
        self.disable_color = bool(os.environ.get("NO_COLOR"))
        self.high_contrast = bool(
            os.environ.get("KALASH_HIGH_CONTRAST")
            or os.environ.get("KALASH_HC")
        )

    @property
    def is_dark(self) -> bool:
        forced = os.environ.get("KALASH_THEME", "").strip().lower()
        if forced in ("light", "day"):
            return False
        if forced in ("dark", "night"):
            return True
        return True


def palette(*, high_contrast: bool = False, dark: bool = True) -> dict[str, str]:
    """Named colors for Rich inline styles and Textual CSS."""
    if dark:
        return {
            "bg": "#0d1117",
            "fg": "#f0f6fc" if high_contrast else "#e6edf3",
            "muted": "#c9d1d9" if high_contrast else "#9da7b3",
            "primary": "#58a6ff",
            "accent": "#79c0ff",
            "success": "#3fb950",
            "warning": "#d29922",
            "error": "#ff7b72",
            "border": "#30363d",
            "surface": "#161b22",
        }
    return {
        "bg": "#ffffff",
        "fg": "#1f2328",
        "muted": "#59636e",
        "primary": "#0969da",
        "accent": "#0550ae",
        "success": "#1a7f37",
        "warning": "#9a6700",
        "error": "#cf222e",
        "border": "#d1d9e0",
        "surface": "#f6f8fa",
    }


def app_css(theme: KalashTheme | None = None) -> str:
    """Return supplemental CSS for :class:`KalashApp`."""
    t = theme or KalashTheme()
    p = palette(high_contrast=t.high_contrast, dark=t.is_dark)
    hc = ".high-contrast" if t.high_contrast else ""

    return f"""
    Screen {{
        background: {p["bg"]};
        color: {p["fg"]};
    }}

    {hc} SystemMessage {{
        color: {p["fg"]};
    }}

    SystemMessage {{
        color: {p["muted"]};
    }}

    ToolCallLine {{
        color: {p["fg"]};
    }}

    ToolOutputView {{
        color: {p["fg"]};
        background: {p["surface"]};
        border-left: tall {p["border"]};
        padding: 0 0 0 2;
        margin: 0 0 1 0;
    }}

    AssistantMessage {{
        color: {p["fg"]};
    }}

    UserMessage {{
        color: {p["fg"]};
    }}

    WelcomeBanner {{
        color: {p["muted"]};
    }}

    #statusline {{
        color: {p["muted"]};
    }}

    #hints {{
        color: {p["muted"]};
    }}

    #input-wrap {{
        border: round {p["primary"]};
        background: {p["surface"]};
    }}
    #input-wrap.busy {{
        border: round {p["warning"]};
    }}
    #input-wrap:focus-within {{
        border: round {p["accent"]};
    }}

    #prompt {{
        color: {p["fg"]};
        background: transparent;
    }}
    """
