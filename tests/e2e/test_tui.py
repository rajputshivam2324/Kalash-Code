"""End-to-end TUI tests driven through Textual's Pilot.

These cover the interaction failures that shipped in the first TUI build:
a widget method shadowing Textual internals (which broke all painting), `tab`
being claimed by focus navigation (which silently killed every later
keystroke), and the picker only supporting type-to-filter for slash commands.
"""

from __future__ import annotations

import pytest
from textual.widgets import Input

from kalash.tui.app import KalashApp
from kalash.tui.messages import (
    AssistantMessage,
    SystemMessage,
    UserMessage,
    WelcomeBanner,
)
from kalash.tui.picker import Picker, PickerMode


def prompt(app: KalashApp) -> Input:
    return app.query_one("#prompt", Input)


async def submit(pilot, app: KalashApp, text: str) -> None:
    """Type a full line and submit it."""
    prompt(app).value = text
    await pilot.press("enter")
    await pilot.pause()


class TestStartup:
    async def test_mounts_with_banner_and_focus(self):
        app = KalashApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert len(app.query(WelcomeBanner)) == 1
            assert prompt(app).has_focus


class TestSlashCommands:
    async def test_slash_opens_picker(self):
        app = KalashApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("/")
            await pilot.pause()
            picker = app.query_one(Picker)
            assert picker.is_open
            assert picker.mode is PickerMode.COMMAND

    async def test_type_to_filter_commands(self):
        app = KalashApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("/", "s", "t", "a")
            await pilot.pause()
            selected = app.query_one(Picker).selected()
            assert selected is not None
            assert selected.value == "/status"

    async def test_enter_runs_selected_command(self):
        app = KalashApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("/", "s", "t", "a", "enter")
            await pilot.pause()
            assert not app.query_one(Picker).is_open
            assert len(app.query(SystemMessage)) >= 1

    async def test_arrow_keys_move_selection(self):
        app = KalashApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("/")
            await pilot.pause()
            picker = app.query_one(Picker)
            first = picker.selected().value
            await pilot.press("down")
            await pilot.pause()
            second = picker.selected().value
            assert first != second
            await pilot.press("up")
            await pilot.pause()
            assert picker.selected().value == first

    async def test_escape_dismisses_picker(self):
        app = KalashApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("/")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert not app.query_one(Picker).is_open

    async def test_clear_and_new(self):
        app = KalashApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await submit(pilot, app, "hello")
            assert len(app.query(UserMessage)) == 1

            await submit(pilot, app, "/clear")
            assert len(app.query(UserMessage)) == 0

            await submit(pilot, app, "/new")
            assert len(app.query(WelcomeBanner)) == 1


class TestModeToggle:
    async def test_tab_toggles_mode_without_stealing_focus(self):
        """Regression: `tab` used to trigger focus navigation instead."""
        app = KalashApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            start = app.mode

            await pilot.press("tab")
            await pilot.pause()
            assert app.mode != start
            assert prompt(app).has_focus, "focus must stay on the prompt"

            await pilot.press("tab")
            await pilot.pause()
            assert app.mode == start

    async def test_keystrokes_still_land_after_tab(self):
        """Regression: losing focus made every later keypress a no-op."""
        app = KalashApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            await submit(pilot, app, "still works")
            assert len(app.query(UserMessage)) == 1


class TestProviderPicker:
    async def test_connect_lists_providers(self):
        app = KalashApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await submit(pilot, app, "/connect")
            picker = app.query_one(Picker)
            assert picker.mode is PickerMode.PROVIDER
            assert len(picker._shown) > 5

    async def test_type_to_filter_providers(self):
        """Regression: only slash commands supported type-to-filter."""
        app = KalashApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await submit(pilot, app, "/connect")
            before = len(app.query_one(Picker)._shown)

            await pilot.press("o", "l", "l")
            await pilot.pause()

            picker = app.query_one(Picker)
            assert len(picker._shown) < before
            selected = picker.selected()
            assert selected is not None
            assert selected.value == "ollama"


class TestSending:
    async def test_user_message_posted_and_input_cleared(self):
        app = KalashApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await submit(pilot, app, "explain this repo")
            assert len(app.query(UserMessage)) == 1
            assert prompt(app).value == ""

    async def test_markup_in_user_text_is_not_interpreted(self):
        """Square brackets in user text must not be parsed as Rich markup."""
        app = KalashApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await submit(pilot, app, "array[0] and [bold] literal")
            assert app.is_running

    async def test_missing_provider_is_reported(self, monkeypatch):
        monkeypatch.setattr(KalashApp, "_restore_session", lambda self: None)
        app = KalashApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await submit(pilot, app, "hello")
            assert len(app.query(AssistantMessage)) == 0
            blurb = " ".join(str(w.renderable) for w in app.query(SystemMessage)).lower()
            assert "connect" in blurb


class TestHistory:
    async def test_up_recalls_previous_prompt(self):
        app = KalashApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await submit(pilot, app, "first message")
            prompt(app).value = ""
            await pilot.press("up")
            await pilot.pause()
            assert prompt(app).value == "first message"


class TestStreamingWidget:
    def test_accumulates_chunks(self):
        message = AssistantMessage()
        message.append("hel")
        message.append("lo ")
        message.append("[world]")
        assert message.text == "hello [world]"

    def test_finish_and_error_are_terminal(self):
        ok = AssistantMessage()
        ok.append("done")
        ok.finish()
        assert ok.text == "done"

        bad = AssistantMessage()
        bad.set_error("boom")
        assert bad.text == ""


class TestPickerUnit:
    def test_filter_matches_value_and_label(self):
        from kalash.tui.picker import PickerItem

        picker = Picker()
        picker.open(
            PickerMode.MODEL,
            [
                PickerItem(value="gpt-4o", label="gpt-4o"),
                PickerItem(value="claude-sonnet", label="claude-sonnet"),
            ],
        )
        picker.filter("son")
        assert [i.value for i in picker._shown] == ["claude-sonnet"]

    def test_move_clamps_at_bounds(self):
        from kalash.tui.picker import PickerItem

        picker = Picker()
        picker.open(
            PickerMode.MODEL,
            [PickerItem(value=str(n), label=str(n)) for n in range(3)],
        )
        picker.move(-5)
        assert picker.selected().value == "0"
        picker.move(99)
        assert picker.selected().value == "2"

    def test_close_resets_state(self):
        from kalash.tui.picker import PickerItem

        picker = Picker()
        picker.open(PickerMode.COMMAND, [PickerItem(value="/a", label="/a")])
        assert picker.is_open
        picker.close()
        assert not picker.is_open
        assert picker.selected() is None
