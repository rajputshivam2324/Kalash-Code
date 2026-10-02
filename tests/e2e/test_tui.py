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
from tests.render import plain as render_plain


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

            # First /clear shows a confirmation warning (U-5)
            await submit(pilot, app, "/clear")
            assert len(app.query(UserMessage)) >= 1  # not cleared yet

            # Second /clear actually clears
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


# ---------------------------------------------------------------------------
# Agent integration
# ---------------------------------------------------------------------------
#
# The TUI shipped as a one-shot text client: `_stream` attached no tools and
# rebuilt a single-message history on every send, so the model told users it
# "cannot access your filesystem" while sitting in their repository. It also
# crashed on session resume, because `_current_mode` was shadowed by a Textual
# instance attribute. These cover both.

import json

from kalash.models.normalize import (
    BlockDelta,
    BlockStart,
    BlockStop,
    MessageStart,
    MessageStop,
    StopReason,
    UsageUpdate,
)
from kalash.runtime.scratchpad import reset_cache


class _FakeProvider:
    name = "fake/model-1"
    context_window = 200_000

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    async def stream(self, messages, *, system=None, tools=None, **kwargs):
        self.requests.append({"system": system, "tools": tools, "messages": messages})
        events = self.turns.pop(0) if self.turns else [MessageStop(StopReason.END_TURN)]
        for event in events:
            yield event


class _FakeResolution:
    def __init__(self, provider):
        self.ok = True
        self.provider = provider
        self.reason = ""


def _text_turn(text):
    return [
        MessageStart(id="m", model="fake/model-1"),
        BlockStart(index=0, block_type="text"),
        BlockDelta(index=0, delta=text),
        BlockStop(index=0),
        UsageUpdate(input_tokens=200, output_tokens=20),
        MessageStop(StopReason.END_TURN),
    ]


def _tool_turn(name, args):
    return [
        MessageStart(id="m", model="fake/model-1"),
        BlockStart(index=0, block_type="tool_use", tool_use_id="tu_1", tool_name=name),
        BlockDelta(index=0, delta=json.dumps(args)),
        BlockStop(index=0),
        UsageUpdate(input_tokens=200, output_tokens=30),
        MessageStop(StopReason.TOOL_USE),
    ]


@pytest.fixture
def tui_env(tmp_path, monkeypatch):
    """Isolated home and cwd, with a connected fake provider."""
    monkeypatch.setenv("KALASH_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    reset_cache()
    yield project
    reset_cache()


def _connect(monkeypatch, app, turns):
    provider = _FakeProvider(turns)
    monkeypatch.setattr(
        "kalash.models.resolve.build_provider",
        lambda *a, **k: _FakeResolution(provider),
    )
    app.provider_id = "anthropic"
    app.model_id = "fake/model-1"
    return provider


class TestAgentIntegration:
    async def test_send_writes_a_real_file(self, tui_env, monkeypatch):
        app = KalashApp()
        provider = _connect(
            monkeypatch,
            app,
            [
                _tool_turn("write", {"path": "made.txt", "content": "hello\n"}),
                _text_turn("Created made.txt."),
            ],
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await submit(pilot, app, "create made.txt")
            for _ in range(30):
                await pilot.pause()
                if (tui_env / "made.txt").exists():
                    break

        assert (tui_env / "made.txt").exists()
        assert provider.requests[0]["tools"], "the TUI must attach tools"

    async def test_history_persists_between_sends(self, tui_env, monkeypatch):
        app = KalashApp()
        provider = _connect(monkeypatch, app, [_text_turn("first"), _text_turn("second")])
        async with app.run_test(size=(100, 30)) as pilot:
            await submit(pilot, app, "one")
            for _ in range(20):
                await pilot.pause()
                if provider.requests:
                    break
            await submit(pilot, app, "two")
            for _ in range(20):
                await pilot.pause()
                if len(provider.requests) >= 2:
                    break

        assert len(provider.requests) >= 2
        # Turn two must see turn one; the old code rebuilt a 1-message history.
        assert len(provider.requests[1]["messages"]) > len(provider.requests[0]["messages"])

    async def test_agent_mode_helper_is_callable(self):
        """Regression: shadowed by Textual's App._current_mode instance attr."""
        app = KalashApp()
        assert callable(app._agent_mode)
        assert app._agent_mode() == "build"

    async def test_plan_mode_toggle_reaches_the_agent(self, tui_env, monkeypatch):
        app = KalashApp()
        _connect(monkeypatch, app, [_text_turn("plan only")])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("tab")
            await pilot.pause()
            assert app._agent_mode() == "plan"
            agent = await app._ensure_agent()
            assert agent is not None
            names = {s["name"] for s in agent.host.schemas()}
            assert "write" not in names


class TestSessionResume:
    async def test_sessions_command_does_not_crash(self, tui_env, monkeypatch):
        """`/sessions` then picking one raised TypeError before the rename."""
        app = KalashApp()
        _connect(monkeypatch, app, [_text_turn("hi"), _text_turn("resumed")])

        async with app.run_test(size=(100, 30)) as pilot:
            await submit(pilot, app, "hello")
            for _ in range(20):
                await pilot.pause()
                if app._agent is not None:
                    break
            session_id = app._agent.session_id

            await app._session_chosen(session_id)
            await pilot.pause()

        assert app._agent is not None
        assert app._agent.session_id == session_id

    async def test_status_reports_agent_state(self, tui_env, monkeypatch):
        app = KalashApp()
        _connect(monkeypatch, app, [_text_turn("hi")])
        async with app.run_test(size=(100, 30)) as pilot:
            await submit(pilot, app, "hello")
            for _ in range(20):
                await pilot.pause()
                if app._agent is not None:
                    break
            lines = app._status_lines()

        joined = "\n".join(lines)
        assert "session" in joined
        assert "tools" in joined


class TestNewSlashCommands:
    @pytest.mark.parametrize(
        "command",
        [
            "/tools",
            "/notes",
            "/scratch",
            "/cost",
            "/status",
            "/mode",
            "/sessions",
            "/mcp",
            "/skills",
        ],
    )
    async def test_command_runs_without_error(self, tui_env, monkeypatch, command):
        app = KalashApp()
        _connect(monkeypatch, app, [_text_turn("hi")])
        async with app.run_test(size=(100, 30)) as pilot:
            await submit(pilot, app, command)
            await pilot.pause()
            errors = [w for w in app.query(SystemMessage) if "unknown command" in str(w.renderable)]
            assert not errors, f"{command} was not handled"

    async def test_init_writes_a_starter_file(self, tui_env, monkeypatch):
        app = KalashApp()
        _connect(monkeypatch, app, [_text_turn("hi")])
        async with app.run_test(size=(100, 30)) as pilot:
            await submit(pilot, app, "/init")
            await pilot.pause()

        assert (tui_env / "KALASH.md").exists()


# ---------------------------------------------------------------------------
# Getting text out, and seeing the work
# ---------------------------------------------------------------------------
#
# Textual puts the terminal into mouse-reporting mode, so a drag is delivered to
# the app rather than to the terminal's own selection — which is why transcript
# text could not be copied and looked "rendered as an image". OSC 52 and a file
# export are the two ways out that do not require abandoning the TUI.


class TestCopyAndExport:
    async def test_copy_reports_when_there_is_nothing_yet(self, tui_env, monkeypatch):
        app = KalashApp()
        _connect(monkeypatch, app, [_text_turn("hi")])
        async with app.run_test(size=(100, 30)) as pilot:
            await submit(pilot, app, "/copy")
            await pilot.pause()
            assert "nothing to copy" in app._copy_last_reply()

    async def test_copy_captures_the_last_reply(self, tui_env, monkeypatch):
        app = KalashApp()
        _connect(monkeypatch, app, [_text_turn("the answer is 42")])
        async with app.run_test(size=(100, 30)) as pilot:
            await submit(pilot, app, "question")
            for _ in range(30):
                await pilot.pause()
                if app._last_reply_text():
                    break
            assert "42" in app._last_reply_text()

    async def test_export_writes_a_readable_file(self, tui_env, monkeypatch):
        app = KalashApp()
        _connect(monkeypatch, app, [_text_turn("exported content here")])
        async with app.run_test(size=(100, 30)) as pilot:
            await submit(pilot, app, "hello")
            for _ in range(30):
                await pilot.pause()
                if app._last_reply_text():
                    break
            message = app._export_transcript()

        assert "wrote" in message
        written = list(tui_env.glob("kalash-transcript-*.txt"))
        assert written, "an export file must exist"
        body = written[0].read_text()
        assert "hello" in body
        assert "exported content here" in body

    async def test_export_declines_when_empty(self, tui_env, monkeypatch):
        app = KalashApp()
        _connect(monkeypatch, app, [_text_turn("x")])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert "nothing to export" in app._export_transcript()

    async def test_copy_binding_exists(self):
        app = KalashApp()
        keys = {binding.key for binding in app.BINDINGS}
        assert "ctrl+y" in keys
        assert "ctrl+o" in keys
        assert callable(app.action_copy_reply)
        assert callable(app.action_toggle_output)


class TestWebSearchIsAvailable:
    """Regression: the agent answered "I don't have a web search tool".

    True at the time — `web_search` was excluded from the reduced tool profile a
    throughput-limited model receives.
    """

    async def test_small_model_still_gets_web_search(self, tui_env, monkeypatch):
        app = KalashApp()
        _connect(monkeypatch, app, [_text_turn("ok")])
        app.model_id = "openai/gpt-oss-20b"

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            agent = await app._ensure_agent()
            assert agent is not None
            names = {schema["name"] for schema in agent.host.schemas()}

        assert "web_search" in names
        assert "fetch" in names

    async def test_tools_command_lists_web_search(self, tui_env, monkeypatch):
        app = KalashApp()
        _connect(monkeypatch, app, [_text_turn("ok")])
        app.model_id = "openai/gpt-oss-20b"

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app._ensure_agent()
            lines = "\n".join(app._tool_lines())

        assert "web_search" in lines


class TestDiffsAppearInTheTranscript:
    async def test_a_write_renders_a_diff(self, tui_env, monkeypatch):
        from kalash.tui.messages import ToolCallLine

        app = KalashApp()
        _connect(
            monkeypatch,
            app,
            [
                _tool_turn("write", {"path": "made.py", "content": "print(1)\n"}),
                _text_turn("done"),
            ],
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await submit(pilot, app, "create made.py")
            for _ in range(40):
                await pilot.pause()
                lines = list(app.query(ToolCallLine))
                if any("+" in render_plain(line.renderable) for line in lines):
                    break
            lines = list(app.query(ToolCallLine))
            assert lines
            plain = "\n".join(render_plain(line.renderable) for line in lines)
            assert "made.py" in plain or "Wrote" in plain
            assert "+" in plain or "print" in plain
