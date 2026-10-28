"""Kalash colour themes and the palette widgets use for Rich-rendered text.

Textual themes drive CSS; widgets that render Rich ``Text`` (diffs, tool headers, the status
line) need concrete colours, so :class:`Palette` is rebuilt from the active theme whenever it
changes - one source of truth, no hard-coded colours scattered through widgets.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from textual.app import App
from textual.theme import Theme

DARK = Theme(
    name="kalash-dark",
    primary="#f5a524",  # saffron
    secondary="#2dd4bf",  # teal
    accent="#f5a524",
    warning="#e3b341",
    error="#ff6b6b",
    success="#4ade80",
    foreground="#e6edf3",
    background="#0d1117",
    surface="#161b22",
    panel="#1c2128",
    dark=True,
)

LIGHT = Theme(
    name="kalash-light",
    primary="#b45309",
    secondary="#0f766e",
    accent="#b45309",
    warning="#a16207",
    error="#b91c1c",
    success="#15803d",
    foreground="#1f2328",
    background="#ffffff",
    surface="#f6f8fa",
    panel="#eaeef2",
    dark=False,
)

THEMES = (DARK, LIGHT)
DEFAULT_THEME = DARK.name
# Names settings files used before themes were namespaced (the old UI stored "light"/"dark").
ALIASES = {"dark": DARK.name, "light": LIGHT.name}


def resolve(name: object, available: Iterable[str]) -> str:
    """The theme to use for a configured name: aliases expanded, unknown names fall back."""
    chosen = ALIASES.get(str(name), str(name))
    return chosen if chosen in set(available) else DEFAULT_THEME


# Terminal-native colours (the user's own terminal palette) plus a few popular schemes.
SELECTABLE = (
    "kalash-dark",
    "kalash-light",
    "ansi-dark",
    "ansi-light",
    "tokyo-night",
    "nord",
    "dracula",
    "catppuccin-mocha",
    "gruvbox",
    "solarized-dark",
    "solarized-light",
    "monokai",
)


@dataclass(frozen=True)
class Palette:
    accent: str
    ok: str
    error: str
    warn: str
    info: str
    fg: str
    muted: str
    added_fg: str
    added_bg: str
    removed_fg: str
    removed_bg: str
    hunk: str
    plan: str


def palette_for(app: App[Any]) -> Palette:
    v = app.get_css_variables()
    dark = app.current_theme.dark

    def color(name: str, fallback: str) -> str:
        value = v.get(name, fallback)
        return value if value.startswith("#") else fallback

    return Palette(
        accent=color("primary", "#f5a524"),
        ok=color("success", "#4ade80"),
        error=color("error", "#ff6b6b"),
        warn=color("warning", "#e3b341"),
        info=color("secondary", "#2dd4bf"),
        fg=color("foreground", "#e6edf3"),
        muted=color("foreground-muted", "#8b949e")[:7],
        added_fg="#7ee787" if dark else "#116329",
        added_bg="#12261a" if dark else "#dafbe1",
        removed_fg="#ffa198" if dark else "#82071e",
        removed_bg="#2d1618" if dark else "#ffebe9",
        hunk="#79c0ff" if dark else "#0550ae",
        plan=color("secondary", "#2dd4bf"),
    )


def register(app: App[Any]) -> None:
    for theme in THEMES:
        app.register_theme(theme)
