"""Theme configuration for the Kalash TUI.

Themes integrate with Textual's native ``App.theme`` so both ``/theme`` and the
built-in command palette (Ctrl+P → search themes) repaint the whole UI. Hardcoded
hex in class CSS was overriding palette changes — that is removed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from textual.theme import Theme

from kalash.core.paths import kalash_home, settings_path

# Prefix so Ctrl+P search for "theme" lists these, matching the /theme labels.
KALASH_THEME_PREFIX = "theme → "

# Layout + Textual design tokens (colors come from the active App.theme).
APP_CSS = """
Screen {
    background: $background;
    color: $foreground;
    layers: base overlay;
}

#transcript {
    height: 1fr;
    width: 100%;
    padding: 0 2;
    scrollbar-size-vertical: 1;
    background: $background;
    color: $foreground;
}

#context-sidebar {
    width: 35;
    dock: right;
    display: none;
    background: $surface;
    border-left: vkey $border;
    padding: 1 2;
}
#context-sidebar.visible {
    display: block;
}

#footer {
    dock: bottom;
    height: auto;
    width: 100%;
    padding: 0 2 0 2;
    background: $background;
}

UserMessage {
    height: auto;
    width: 100%;
    padding: 0 0 0 1;
    margin: 1 0 1 0;
    color: $foreground;
    border-left: thick $secondary;
}

AssistantMessage {
    height: auto;
    width: 100%;
    padding: 0 0 0 1;
    margin: 1 0 1 0;
    color: $foreground;
    transition: opacity 300ms in_out_cubic;
}

SystemMessage {
    height: auto;
    width: 100%;
    padding: 0 0 0 1;
    margin: 1 0 0 0;
    color: $text-muted;
}

.high-contrast SystemMessage {
    color: $foreground;
}

ToolCallLine {
    height: auto;
    width: 100%;
    padding: 0 0 0 1;
    margin: 0 0 1 0;
    color: $foreground;
}

#input-wrap {
    height: auto;
    width: 100%;
    border: none;
    background: $surface;
    padding: 1 0;
    margin-top: 1;
    transition: height 200ms in_out_cubic;
}
#input-wrap.busy {
    border-top: wide $warning;
}
#input-wrap:focus-within {
    border: none;
}

#prompt {
    width: 1fr;
    background: transparent;
    border: none;
    padding: 0 1;
    color: $foreground;
}
#prompt:focus {
    border: none;
}

#statusline {
    height: 1;
    width: 100%;
    padding: 0 1;
    color: $text-muted;
}

#hints {
    height: 1;
    width: 100%;
    padding: 0 1;
    color: $text-muted;
}

WelcomeBanner {
    height: auto;
    width: 100%;
    text-align: center;
    color: $text-muted;
    padding: 2 0 1 0;
}

ToolOutputView {
    height: auto;
    width: 100%;
    padding: 0 0 0 3;
    margin: 0 0 1 0;
    color: $foreground;
    background: $surface;
    border-left: tall $primary-darken-2;
}

SubagentLine {
    color: $accent;
}

Picker {
    height: 3;
    width: 100%;
    display: none;
    padding: 0 1;
    border: round $accent;
}
Picker.visible {
    display: block;
}

DiffReview {
    margin: 1 0;
    padding: 1;
    border: round $accent;
    background: $surface;
}
DiffReview .buttons {
    height: auto;
    align: right middle;
    margin-top: 1;
}

MetricsMessage {
    height: auto;
    margin: 1 0;
    padding: 1;
    background: $surface;
    border-left: tall $success;
}
"""


class KalashTheme:
    """Theme settings with persistence and runtime switching."""

    PRESETS: tuple[tuple[str, str, bool, bool], ...] = (
        ("dark", "Dark", True, False),
        ("light", "Light", False, False),
        ("dark-hc", "Dark · high contrast", True, True),
        ("light-hc", "Light · high contrast", False, True),
    )

    def __init__(
        self,
        *,
        dark: bool | None = None,
        high_contrast: bool | None = None,
    ) -> None:
        saved = _load_saved()
        self.disable_color = bool(os.environ.get("NO_COLOR"))
        self._dark = (
            dark
            if dark is not None
            else saved.get("dark", _env_dark(default=True))
        )
        self._high_contrast = (
            high_contrast
            if high_contrast is not None
            else bool(
                os.environ.get("KALASH_HIGH_CONTRAST")
                or os.environ.get("KALASH_HC")
                or saved.get("high_contrast", False)
            )
        )

    @classmethod
    def load(cls) -> KalashTheme:
        return cls()

    @property
    def is_dark(self) -> bool:
        return self._dark

    @property
    def high_contrast(self) -> bool:
        return self._high_contrast

    @property
    def preset_id(self) -> str:
        for pid, _, dark, hc in self.PRESETS:
            if dark == self._dark and hc == self._high_contrast:
                return pid
        return "dark" if self._dark else "light"

    @property
    def label(self) -> str:
        for pid, label, _, _ in self.PRESETS:
            if pid == self.preset_id:
                return label
        return "Dark" if self._dark else "Light"

    @property
    def textual_name(self) -> str:
        """Registered Textual theme name for this preset."""
        return f"{KALASH_THEME_PREFIX}{self.label}"

    def set_preset(self, preset_id: str) -> None:
        for pid, _, dark, hc in self.PRESETS:
            if pid == preset_id:
                self._dark = dark
                self._high_contrast = hc
                return
        if preset_id in ("dark", "light"):
            self._dark = preset_id == "dark"
            self._high_contrast = False

    @classmethod
    def preset_from_textual_name(cls, name: str) -> str | None:
        for pid, label, _, _ in cls.PRESETS:
            if name == f"{KALASH_THEME_PREFIX}{label}":
                return pid
        return None

    def save(self) -> None:
        _save_settings({"dark": self._dark, "high_contrast": self._high_contrast})

    def apply_to(self, app) -> None:
        """Activate this palette via Textual's theme engine.

        Setting ``App.theme`` before the app is running queues a CSS refresh that
        can be dropped. If the saved theme is already active, the reactive watch
        also skips — so we force a refresh once the DOM is live.
        """
        app.set_class(self._high_contrast, "high-contrast")
        name = self.textual_name
        if name not in app.available_themes:
            return
        changed = app.theme != name
        app.theme = name
        if not changed:
            _refresh_live_css(app)


def palette(*, high_contrast: bool = False, dark: bool = True) -> dict[str, str]:
    """Named colors for Rich inline styles."""
    if dark:
        return {
            "bg": "#0a0a0a",
            "fg": "#ffffff" if high_contrast else "#eeeeee",
            "muted": "#c4c4c4" if high_contrast else "#808080",
            "primary": "#fab283",
            "accent": "#9d7cd8",
            "success": "#7fd88f",
            "warning": "#f5a742",
            "error": "#e06c75",
            "border": "#484848",
            "surface": "#141414",
        }
    return {
        "bg": "#ffffff",
        "fg": "#000000" if high_contrast else "#1a1a1a",
        "muted": "#505050" if high_contrast else "#8a8a8a",
        "primary": "#3b7dd8",
        "accent": "#d68c27",
        "success": "#3d9a57",
        "warning": "#d68c27",
        "error": "#d1383d",
        "border": "#b8b8b8",
        "surface": "#fafafa",
    }


def _to_textual_theme(_preset_id: str, label: str, *, dark: bool, high_contrast: bool) -> Theme:
    p = palette(dark=dark, high_contrast=high_contrast)
    return Theme(
        name=f"{KALASH_THEME_PREFIX}{label}",
        primary=p["primary"],
        accent=p["accent"],
        warning=p["warning"],
        error=p["error"],
        success=p["success"],
        foreground=p["fg"],
        background=p["bg"],
        surface=p["surface"],
        panel=p["surface"],
        dark=dark,
        variables={
            "border": p["border"],
            "text-muted": p["muted"],
        },
    )


def register_kalash_themes(app, *, activate: str | None = None) -> None:
    """Register Kalash palettes and hide unrelated built-in themes from Ctrl+P.

    The active theme is switched *before* built-ins are removed so
    ``App.current_theme`` never points at an unregistered name.
    """
    for pid, label, dark, hc in KalashTheme.PRESETS:
        app.register_theme(_to_textual_theme(pid, label, dark=dark, high_contrast=hc))

    names = [name for name in app.available_themes if name.startswith(KALASH_THEME_PREFIX)]
    chosen = activate if activate in names else names[0]
    app.theme = chosen

    for name in list(app.available_themes):
        if not name.startswith(KALASH_THEME_PREFIX):
            app.unregister_theme(name)


def _refresh_live_css(app) -> None:
    """Repaint tokens when the theme name did not change (watch skipped)."""
    if not getattr(app, "is_running", False):
        return
    invalidate = getattr(app, "_invalidate_css", None)
    if callable(invalidate):
        invalidate()
    refresh = getattr(app, "refresh_css", None)
    if callable(refresh):
        refresh()


def _env_dark(*, default: bool) -> bool:
    forced = os.environ.get("KALASH_THEME", "").strip().lower()
    if forced in ("light", "day"):
        return False
    if forced in ("dark", "night"):
        return True
    return default


def _settings_file() -> Path:
    kalash_home().mkdir(parents=True, exist_ok=True)
    return settings_path("user")


def _load_saved() -> dict[str, bool]:
    path = _settings_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    tui = data.get("tui")
    if not isinstance(tui, dict):
        return {}
    out: dict[str, bool] = {}
    if isinstance(tui.get("dark"), bool):
        out["dark"] = tui["dark"]
    if isinstance(tui.get("high_contrast"), bool):
        out["high_contrast"] = tui["high_contrast"]
    preset = tui.get("theme")
    if isinstance(preset, str):
        for pid, _, dark, hc in KalashTheme.PRESETS:
            if preset == pid:
                out["dark"] = dark
                out["high_contrast"] = hc
                break
    return out


def _save_settings(values: dict[str, bool]) -> None:
    path = _settings_file()
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    tui = data.setdefault("tui", {})
    if not isinstance(tui, dict):
        tui = {}
        data["tui"] = tui
    tui["dark"] = values["dark"]
    tui["high_contrast"] = values["high_contrast"]
    tui["theme"] = (
        "dark-hc"
        if values["dark"] and values["high_contrast"]
        else "light-hc"
        if not values["dark"] and values["high_contrast"]
        else "dark"
        if values["dark"]
        else "light"
    )
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def app_css(theme: KalashTheme | None = None) -> str:
    """Backward-compatible alias."""
    _ = theme
    return APP_CSS
