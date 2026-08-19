"""Theme switching for the Kalash TUI."""

from __future__ import annotations

import json

from textual.command import CommandPalette
from textual.theme import ThemeProvider

from kalash.tui.theme import (
    KALASH_THEME_PREFIX,
    KalashTheme,
    palette,
    register_kalash_themes,
)


class TestKalashTheme:
    def test_dark_and_light_palettes_differ(self):
        dark = palette(dark=True)
        light = palette(dark=False)
        assert dark["bg"] != light["bg"]
        assert dark["fg"] != light["fg"]

    def test_textual_name_is_searchable_as_theme(self):
        theme = KalashTheme(dark=False, high_contrast=False)
        assert theme.textual_name == f"{KALASH_THEME_PREFIX}Light"
        assert theme.textual_name.startswith("theme")

    def test_preset_round_trip(self):
        theme = KalashTheme(dark=False, high_contrast=True)
        assert theme.preset_id == "light-hc"
        theme.set_preset("dark")
        assert theme.is_dark is True
        assert theme.high_contrast is False
        assert theme.preset_id == "dark"

    def test_save_and_load_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALASH_HOME", str(tmp_path))
        saved = KalashTheme(dark=False, high_contrast=False)
        saved.save()
        loaded = KalashTheme.load()
        assert loaded.is_dark is False
        assert loaded.high_contrast is False

        settings = json.loads((tmp_path / "settings.json").read_text())
        assert settings["tui"]["theme"] == "light"

    def test_apply_to_sets_textual_theme(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        from textual.app import App

        from kalash.tui.theme import APP_CSS

        class Bare(App):
            CSS = APP_CSS

        app = Bare()
        theme = KalashTheme(dark=False, high_contrast=False)
        register_kalash_themes(app, activate=theme.textual_name)
        theme.apply_to(app)
        assert app.theme == theme.textual_name

    def test_register_prunes_builtin_themes(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        from textual.app import App

        app = App()
        register_kalash_themes(app, activate=f"{KALASH_THEME_PREFIX}Light")
        assert all(name.startswith(KALASH_THEME_PREFIX) for name in app.available_themes)
        assert len(app.available_themes) == 4
        assert app.theme == f"{KALASH_THEME_PREFIX}Light"
        assert app.current_theme.background == palette(dark=False)["bg"]


class TestThemePaintsTheScreen:
    async def test_light_theme_whitens_the_background(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALASH_HOME", str(tmp_path))
        monkeypatch.delenv("NO_COLOR", raising=False)
        from kalash.tui.app import KalashApp

        app = KalashApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._theme.set_preset("light")
            app._apply_theme()
            await pilot.pause()
            bg = app.screen.styles.background
            assert bg.rgb[0] > 200
            assert app.theme == f"{KALASH_THEME_PREFIX}Light"

            app._theme.set_preset("dark")
            app._apply_theme()
            await pilot.pause()
            bg = app.screen.styles.background
            assert bg.rgb[0] < 40
            assert app.theme == f"{KALASH_THEME_PREFIX}Dark"

    async def test_ctrl_p_lists_kalash_themes_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALASH_HOME", str(tmp_path))
        monkeypatch.delenv("NO_COLOR", raising=False)
        from kalash.tui.app import KalashApp

        app = KalashApp()
        assert ThemeProvider in app.COMMANDS
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+p")
            await pilot.pause()
            palette_screen = app.screen
            assert isinstance(palette_screen, CommandPalette)
            names = list(app.available_themes)
            assert names == [
                f"{KALASH_THEME_PREFIX}Dark",
                f"{KALASH_THEME_PREFIX}Light",
                f"{KALASH_THEME_PREFIX}Dark · high contrast",
                f"{KALASH_THEME_PREFIX}Light · high contrast",
            ]
            # Built-in Textual themes must not appear in Ctrl+P.
            assert "textual-dark" not in names
            assert "nord" not in names
