"""The Textual UI driven through its real event loop over a scripted model."""

from __future__ import annotations

import ast
import asyncio
import inspect
import io
import subprocess
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from rich.text import Text
from textual import events
from textual.app import App
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Markdown, OptionList, Static, TextArea

from kalash.agent.events import Notice
from kalash.config import McpServer, Settings, load_settings
from kalash.llm.errors import ErrorCode
from kalash.mcp import MCPManager, MCPTool
from kalash.mcp_http import HttpMCPClient
from kalash.tui.app import KalashApp
from kalash.tui.screens import ApprovalScreen, HelpScreen, PickerScreen, PlanScreen
from kalash.tui.theme import Palette
from kalash.tui.widgets import (
    ActivityBar,
    AssistantMessage,
    ErrorCard,
    ExploreGroup,
    NoticeRow,
    PromptArea,
    ReasoningRow,
    StatusBar,
    Suggestions,
    ToolBlock,
    Transcript,
    TurnFooter,
    UserMessage,
    WelcomeBanner,
)
from tests.harness.scripted import Reply, ScriptedAdapter, call, calls, fail, say

SIZE = (110, 40)
REF = "scripted/claude-opus-5-5"  # the scripted adapter reports this model id


def make_app(
    work: Path, script: list[Reply], **overrides: Any
) -> tuple[KalashApp, ScriptedAdapter]:
    settings: Settings = load_settings(work, {"model": "scripted/model", **overrides})[0]
    adapter = ScriptedAdapter(script, model="claude-opus-5-5")
    app = KalashApp(cwd=work, settings=settings, model="scripted/model", adapter=adapter)
    return app, adapter


def plain(widget: Widget) -> str:
    content = getattr(widget, "content", "")
    return content.plain if isinstance(content, Text) else str(content)


def notes(app: KalashApp) -> list[str]:
    return [w.plain for w in app.query(NoticeRow)]


async def settle(pilot: Any, app: KalashApp, timeout: float = 10.0) -> None:
    """Wait until the running turn (if any) has finished and the UI has repainted."""
    deadline = asyncio.get_running_loop().time() + timeout
    await pilot.pause(0.05)
    while app._turn_active and asyncio.get_running_loop().time() < deadline:
        await pilot.pause(0.05)
    assert not app._turn_active, "turn did not finish in time"
    await pilot.pause(0.2)


async def until(pilot: Any, predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition never became true"
        await pilot.pause(0.05)


async def send(pilot: Any, app: KalashApp, text: str) -> None:
    app._prompt.set_text(text)
    await pilot.press("enter")


async def screen_is(pilot: Any, app: KalashApp, kind: type) -> None:
    await until(pilot, lambda: isinstance(app.screen, kind))
    await pilot.pause(0.1)


def key_on_dialog_mount(kind: type) -> tuple[Callable[[Message], None], list[bool]]:
    """Queue an actual key before the real dialog starts its arming timer.

    Pilot waits for rendering, which may itself outlast the guard on a busy host.
    Observe the state when Textual dispatches this key; don't alter the clock or guard.
    """
    target: Any = None
    key: events.Key | None = None
    armed_at_dispatch: list[bool] = []

    def hook(message: Message) -> None:
        nonlocal target, key
        if key is None and isinstance(message, events.Mount) and isinstance(message._sender, kind):
            target = message._sender
            key = events.Key("y", "y")
            target.post_message(key)
        elif message is key and not armed_at_dispatch:
            armed_at_dispatch.append(target._armed)

    return hook, armed_at_dispatch


# ------------------------------------------------------------------------------- start-up


async def test_welcome_banner_states_model_mode_folder_and_tips(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        banner = app.query_one(WelcomeBanner)
        assert REF in banner.plain
        assert "Ask" in banner.plain
        assert "shift+tab" in banner.plain
        assert app._status.data.model == REF
        assert app._prompt.has_focus


async def test_no_model_configured_explains_the_next_step_instead_of_failing(work: Path):
    app = KalashApp(cwd=work, settings=load_settings(work)[0])
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        assert app.runtime is None
        banner = app.query_one(WelcomeBanner)
        assert "Add an API key" in banner.plain and "/provider" in banner.plain
        await send(pilot, app, "hello")
        await pilot.pause(0.1)
        assert any("No model is configured" in n for n in notes(app))
        assert not list(app.query(UserMessage))


async def test_onboarding_with_a_key_offers_model_selection(work: Path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    app = KalashApp(cwd=work, settings=load_settings(work)[0])
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        banner = app.query_one(WelcomeBanner)
        assert "Pick a model" in banner.plain and "Anthropic" in banner.plain


async def test_a_bad_configured_model_is_reported_on_the_banner(work: Path):
    settings = load_settings(work, {"model": "nowhere/model"})[0]
    app = KalashApp(cwd=work, settings=settings)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        assert app.runtime is None
        assert "nowhere" in app.query_one(WelcomeBanner).plain


# --------------------------------------------------------------------------- conversation


async def test_streaming_answer_renders_user_and_assistant_messages(work: Path):
    app, adapter = make_app(work, [say("Hello **there**, friend.")])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "hi")
        await settle(pilot, app)
        assert [w.plain for w in app.query(UserMessage)] == ["hi"]
        assistants = list(app.query(AssistantMessage))
        assert len(assistants) == 1 and assistants[0].text == "Hello **there**, friend."
        assert app._prompt.text == ""
        assert not app._activity.active
    adapter.assert_exhausted()


async def test_answer_is_rendered_as_markdown_not_raw_text(work: Path):
    app, _ = make_app(work, [say("# Title\n\nSome `code` and **bold**.\n\n- one\n- two\n")])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "format it")
        await settle(pilot, app)
        markdown = app.query_one(AssistantMessage).query_one(Markdown)
        assert "Title" in markdown.source and "**bold**" in markdown.source
        kinds = {type(w).__name__ for w in markdown.query("*")}
        assert {"MarkdownH1", "MarkdownBulletList"} <= kinds


async def test_welcome_banner_stays_above_the_conversation(work: Path):
    app, _ = make_app(work, [say("ok")])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "hi")
        await settle(pilot, app)
        order = [type(w).__name__ for w in app._transcript.children]
        assert order == ["WelcomeBanner", "UserMessage", "AssistantMessage"]


async def test_multiline_prompt_keeps_every_line_and_ctrl_j_inserts_newline(work: Path):
    app, adapter = make_app(work, [say("ok")])
    async with app.run_test(size=SIZE) as pilot:
        app._prompt.set_text("line one")
        await pilot.press("ctrl+j")
        await pilot.press("l", "i", "n", "e", "space", "t", "w", "o")
        assert app._prompt.text == "line one\nline two"
        await pilot.press("enter")
        await settle(pilot, app)
    assert adapter.requests[0].messages[0].visible_text.strip() == "line one\nline two"


async def test_failed_turn_shows_an_error_card_with_the_next_step(work: Path):
    app, _ = make_app(work, [fail(ErrorCode.AUTH, "Incorrect API key provided")])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "hello")
        await settle(pilot, app)
        card = app.query_one(ErrorCard)
        assert "The turn failed" in card.plain and "Incorrect API key" in card.plain
        assert "/provider" in card.plain
        assert not app._turn_active
        # the app is usable afterwards
        app._adapter.script.append(say("recovered"))  # type: ignore[union-attr]
        await send(pilot, app, "again")
        await settle(pilot, app)
        assert [a.text for a in app.query(AssistantMessage)] == ["recovered"]


# ---------------------------------------------------------------------------- tool blocks


async def test_tool_blocks_show_progress_and_results_in_order(work: Path):
    (work / "a.txt").write_text("alpha\n")
    app, _ = make_app(work, [call("read", {"file_path": "a.txt"}), say("It says alpha.")])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "read a.txt")
        await settle(pilot, app)
        blocks = list(app.query(ToolBlock))
        assert len(blocks) == 1 and blocks[0].data.state == "ok"
        head = plain(blocks[0]._head)
        assert head.startswith("✓ Read") and "a.txt" in head
        order = [type(w).__name__ for w in app._transcript.children]
        assert order == [
            "WelcomeBanner",
            "UserMessage",
            "ToolBlock",
            "AssistantMessage",
            "TurnFooter",
        ]


async def test_failed_tool_is_marked_and_shows_its_error(work: Path):
    app, _ = make_app(work, [call("read", {"file_path": "missing.txt"}), say("not there")])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "read it")
        await settle(pilot, app)
        block = app.query_one(ToolBlock)
        assert block.data.state == "error"
        assert plain(block._head).startswith("✗")
        assert block._body.display and "missing.txt" in plain(block._body)


async def test_edit_shows_a_coloured_diff_and_counts(work: Path):
    (work / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    app, _ = make_app(
        work,
        [
            call("read", {"file_path": "calc.py"}),
            call(
                "edit",
                {"file_path": "calc.py", "old_string": "a - b", "new_string": "a + b"},
            ),
            say("fixed"),
        ],
    )
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "fix add")
        await settle(pilot, app)
        edit = list(app.query(ToolBlock))[1]
        assert edit.data.diff and edit.data.diff["added"] == 1 and edit.data.diff["removed"] == 1
        head = plain(edit._head)
        assert "Edit" in head and "+1" in head and "-1" in head
        body = edit._body.content
        assert isinstance(body, Text)
        assert "return a - b" in body.plain and "return a + b" in body.plain
        styles = {str(span.style) for span in body.spans}
        assert any("on #" in s or " on " in s for s in styles), "added/removed lines are tinted"
    assert (work / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"


async def test_ctrl_o_toggles_details_for_every_tool_block(work: Path):
    (work / "a.txt").write_text("".join(f"line {i}\n" for i in range(30)))
    app, _ = make_app(work, [call("read", {"file_path": "a.txt"}), say("done")])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "read it")
        await settle(pilot, app)
        block = app.query_one(ToolBlock)
        assert not block._body.display, "successful reads stay collapsed by default"
        await pilot.press("ctrl+o")
        await pilot.pause(0.1)
        assert app._verbose and block._body.display and "line 3" in plain(block._body)
        assert app._status.data.verbose
        await pilot.press("ctrl+o")
        await pilot.pause(0.1)
        assert not app._verbose and not block._body.display


async def test_clicking_a_tool_header_expands_it(work: Path):
    (work / "a.txt").write_text("alpha\nbeta\n")
    app, _ = make_app(work, [call("read", {"file_path": "a.txt"}), say("done")])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "read it")
        await settle(pilot, app)
        block = app.query_one(ToolBlock)
        assert not block._body.display
        block.on_click()
        assert block._body.display and "alpha" in plain(block._body)


async def test_todo_list_renders_inside_its_block_and_in_the_status_bar(work: Path):
    todos = [
        {"content": "Write parser", "status": "completed"},
        {"content": "Add tests", "status": "in_progress"},
        {"content": "Ship", "status": "pending"},
    ]
    app, _ = make_app(work, [call("todo_write", {"todos": todos}), say("planned")])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "plan the work")
        await settle(pilot, app)
        block = app.query_one(ToolBlock)
        body = plain(block._body)
        assert "Write parser" in body and "Add tests" in body and "Ship" in body
        assert app._status.data.todos == (1, 3)
        assert "1/3" in plain(app._status)


async def test_user_shell_command_runs_in_the_sandbox_and_shows_output(work: Path):
    app, adapter = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "!echo from-the-user")
        await until(pilot, lambda: bool(list(app.query(ToolBlock))))
        await until(pilot, lambda: app.query_one(ToolBlock).data.state == "ok")
        assert "from-the-user" in app.query_one(ToolBlock).data.output
        assert [w.plain for w in app.query(UserMessage)] == ["!echo from-the-user"]
    assert adapter.requests == [], "a ! command never calls the model"


# ------------------------------------------------------------------------------ approvals


async def test_approval_dialog_ignores_keys_until_armed_then_allows(work: Path):
    (work / ".kalash").mkdir()
    app, _ = make_app(
        work, [call("write", {"file_path": ".kalash/n.txt", "content": "ok"}), say("written")]
    )
    hook, armed_at_dispatch = key_on_dialog_mount(ApprovalScreen)
    async with app.run_test(size=SIZE, message_hook=hook) as pilot:
        await send(pilot, app, "write config")
        await screen_is(pilot, app, ApprovalScreen)
        assert armed_at_dispatch == [False], "stray typing reached the actual unarmed dialog"
        assert isinstance(app.screen, ApprovalScreen) and not (work / ".kalash" / "n.txt").exists()
        await until(pilot, lambda: app.screen._armed)
        await pilot.press("y")
        await settle(pilot, app)
        assert (work / ".kalash" / "n.txt").read_text() == "ok"
        assert not isinstance(app.screen, ApprovalScreen)


async def test_approval_escape_denies_and_the_model_is_told(work: Path):
    (work / ".kalash").mkdir()
    app, adapter = make_app(
        work, [call("write", {"file_path": ".kalash/n.txt", "content": "x"}), say("understood")]
    )
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "write config")
        await screen_is(pilot, app, ApprovalScreen)
        await pilot.press("escape")
        await settle(pilot, app)
        assert not (work / ".kalash" / "n.txt").exists()
        assert any("Denied" in n for n in notes(app))
        assert app.query_one(ToolBlock).data.state == "denied"
    assert "declined" in adapter.requests[1].messages[-1].text


async def test_approval_dialog_previews_the_change_as_a_diff(work: Path):
    (work / ".kalash").mkdir()
    (work / ".kalash" / "settings.local.json").write_text('{"model": "a/b"}\n')
    app, _ = make_app(
        work,
        [
            call("read", {"file_path": ".kalash/settings.local.json"}),
            call(
                "edit",
                {
                    "file_path": ".kalash/settings.local.json",
                    "old_string": '"a/b"',
                    "new_string": '"c/d"',
                },
            ),
            say("done"),
        ],
    )
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "change the model")
        await screen_is(pilot, app, ApprovalScreen)
        shown = plain(app.screen.query_one("#approval-detail Static"))
        assert '"a/b"' in shown and '"c/d"' in shown
        await pilot.pause(0.6)
        await pilot.press("n")
        await settle(pilot, app)
    assert '"a/b"' in (work / ".kalash" / "settings.local.json").read_text()


async def test_events_arriving_behind_an_open_dialog_do_not_crash_the_app(work: Path):
    (work / ".git" / "hooks").mkdir(parents=True)
    app, _ = make_app(
        work,
        [
            call(
                "write", {"file_path": ".git/hooks/pre-commit", "content": "x"}, text="Installing."
            ),
            say("done"),
        ],
    )
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "go")
        await screen_is(pilot, app, ApprovalScreen)
        app._on_event(Notice("behind"))
        await pilot.pause(0.2)
        await pilot.press("escape")
        await settle(pilot, app)
        assert "behind" in notes(app)


# ----------------------------------------------------------------------------- plan mode


async def test_plan_mode_review_approve_leaves_plan_mode(work: Path):
    app, adapter = make_app(
        work, [call("exit_plan_mode", {"plan": "## Plan\n\n1. Do it"}), say("implementing")]
    )
    async with app.run_test(size=SIZE) as pilot:
        app.runtime.agent.set_mode("plan")
        await send(pilot, app, "plan something")
        await screen_is(pilot, app, PlanScreen)
        assert "Do it" in "".join(w.source for w in app.screen.query(Markdown))
        await pilot.press("y")
        await settle(pilot, app)
        assert app.runtime.agent.mode == "ask"
    assert "approved" in adapter.requests[1].messages[-1].text.lower()


async def test_plan_mode_review_can_approve_into_auto_mode(work: Path):
    app, _ = make_app(work, [call("exit_plan_mode", {"plan": "step"}), say("go")])
    async with app.run_test(size=SIZE) as pilot:
        app.runtime.agent.set_mode("plan")
        await send(pilot, app, "plan")
        await screen_is(pilot, app, PlanScreen)
        await pilot.press("a")
        await settle(pilot, app)
        assert app.runtime.agent.mode == "auto"
        assert app._status.data.mode_id == "auto"


async def test_plan_mode_keep_planning_sends_feedback_to_the_model(work: Path):
    app, adapter = make_app(work, [call("exit_plan_mode", {"plan": "step"}), say("revising")])
    async with app.run_test(size=SIZE) as pilot:
        app.runtime.agent.set_mode("plan")
        await send(pilot, app, "plan")
        await screen_is(pilot, app, PlanScreen)
        app.screen.query_one("#plan-feedback", Input).value = "also cover the tests"
        await pilot.press("escape")
        await settle(pilot, app)
        assert app.runtime.agent.mode == "plan"
    assert "also cover the tests" in adapter.requests[1].messages[-1].text


async def test_shift_tab_cycles_modes_and_the_status_bar_follows(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        seen = [app._status.data.mode_id]
        for _ in range(3):
            await pilot.press("shift+tab")
            await pilot.pause(0.1)
            seen.append(app._status.data.mode_id)
        assert seen == ["ask", "auto", "plan", "ask"]
        await pilot.press("shift+tab", "shift+tab")
        await pilot.pause(0.1)
        assert app.screen.has_class("mode-plan")
        assert "PLAN" in plain(app._status)


# ------------------------------------------------------------------------------ interrupts


async def test_escape_cancels_a_hanging_turn_and_the_ui_recovers(work: Path):
    app, _ = make_app(work, [Reply(text="partial", hang=True), say("second works")])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "first")
        await until(pilot, lambda: app._turn_active and bool(list(app.query(AssistantMessage))))
        assert app._activity.active
        await pilot.press("escape")
        await settle(pilot, app)
        assert any("Interrupted" in n for n in notes(app))
        assert not app._activity.active
        await send(pilot, app, "second")
        await settle(pilot, app)
        assert [a.text for a in app.query(AssistantMessage)][-1] == "second works"


async def test_a_message_sent_while_busy_is_queued_and_runs_after_the_turn(work: Path):
    app, adapter = make_app(
        work,
        [
            call("bash", {"description": "wait", "command": "sleep 0.8"}),
            say("first done"),
            say("second done"),
        ],
    )
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "first")
        await until(pilot, lambda: app._turn_active)
        await send(pilot, app, "second")
        await pilot.pause(0.1)
        queued = [w for w in app.query(UserMessage) if w.has_class("queued")]
        assert [w.plain for w in queued] == ["second"]
        assert "1 queued" in plain(app._activity)
        await until(pilot, lambda: not app._queue and not app._turn_active, timeout=15)
        await settle(pilot, app)
        assert [a.text for a in app.query(AssistantMessage)] == ["first done", "second done"]
        assert not list(app.query(".queued"))
    adapter.assert_exhausted()


async def test_interrupting_returns_queued_messages_to_the_prompt_instead_of_sending(work: Path):
    app, adapter = make_app(work, [Reply(text="working", hang=True)])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "first")
        await until(pilot, lambda: app._turn_active)
        await send(pilot, app, "second thought")
        await pilot.pause(0.1)
        await pilot.press("escape")
        await settle(pilot, app)
        assert app._prompt.text == "second thought"
        assert not app._queue and not list(app.query(".queued"))
    assert len(adapter.requests) == 1, "the queued message was not sent after Esc"


async def test_ctrl_c_copies_interrupts_clears_then_quits_on_the_second_press(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        app._prompt.set_text("half typed")
        await pilot.press("ctrl+c")
        assert app._prompt.text == ""
        await pilot.press("ctrl+c")
        await pilot.pause(0.1)
        assert app.is_running
        await pilot.press("ctrl+c")
        await pilot.pause(0.5)
    assert not app.is_running


# ---------------------------------------------------------------------------- prompt input


async def test_slash_popup_ranks_by_name_prefix_and_tab_completes(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("slash", "m")
        await pilot.pause(0.1)
        assert app._suggest.display
        values = [
            str(app._suggest.get_option_at_index(i).id) for i in range(app._suggest.option_count)
        ]
        assert values[:3] == ["/model", "/memory", "/mcp"]
        assert "/permissions" in values, "reachable through its /mode alias"
        await pilot.press("tab")
        await pilot.pause(0.1)
        assert app._prompt.text == "/model "
        assert not app._suggest.display


async def test_enter_on_a_slash_popup_runs_the_command(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("slash", "p", "e", "r", "m")
        await pilot.pause(0.1)
        await pilot.press("enter")
        await screen_is(pilot, app, PickerScreen)
        assert app._prompt.text == ""


async def test_escape_closes_the_popup_without_touching_the_text(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("slash", "h")
        await pilot.pause(0.1)
        assert app._suggest.display
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not app._suggest.display and app._prompt.text == "/h"


async def test_at_sign_completes_file_names(work: Path):
    (work / "src").mkdir()
    (work / "src" / "calc.py").write_text("x = 1\n")
    (work / "README.md").write_text("hi\n")
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    subprocess.run(["git", "add", "."], cwd=work, check=True)
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("f", "i", "x", "space", "at", "c", "a")
        await until(pilot, lambda: app._suggest.display and app._suggest.option_count > 0)
        assert str(app._suggest.get_option_at_index(0).id) == "@src/calc.py"
        await pilot.press("tab")
        await pilot.pause(0.1)
        assert app._prompt.text == "fix @src/calc.py "


async def test_prompt_history_walks_with_up_and_down_and_keeps_the_draft(work: Path):
    app, _ = make_app(work, [say("1"), say("2")])
    async with app.run_test(size=SIZE) as pilot:
        for text in ("one", "two"):
            await send(pilot, app, text)
            await settle(pilot, app)
        app._prompt.set_text("draft")
        await pilot.press("up")
        assert app._prompt.text == "two"
        await pilot.press("up")
        assert app._prompt.text == "one"
        await pilot.press("down")
        assert app._prompt.text == "two"
        await pilot.press("down")
        assert app._prompt.text == "draft"


async def test_prompt_history_survives_a_restart(work: Path):
    app, _ = make_app(work, [say("ok")])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "remember me")
        await settle(pilot, app)
    again, _ = make_app(work, [])
    async with again.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        await pilot.press("up")
        assert again._prompt.text == "remember me"


async def test_large_paste_collapses_to_a_token_and_expands_on_send(work: Path):
    app, adapter = make_app(work, [say("got it")])
    big = "\n".join(f"row {i}" for i in range(40))
    async with app.run_test(size=SIZE) as pilot:
        app._prompt.focus()
        app._prompt.post_message(events.Paste(big))
        await pilot.pause(0.2)
        assert app._prompt.text.startswith("[Pasted text #1 +40 lines]")
        await pilot.press("enter")
        await settle(pilot, app)
        assert [w.plain for w in app.query(UserMessage)] == [big]
    assert adapter.requests[0].messages[0].visible_text.strip() == big


async def test_small_paste_is_inserted_verbatim(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        app._prompt.focus()
        app._prompt.post_message(events.Paste("two\nlines"))
        await pilot.pause(0.1)
        assert app._prompt.text == "two\nlines"


async def test_question_mark_on_an_empty_prompt_opens_help(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("question_mark")
        await screen_is(pilot, app, HelpScreen)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, HelpScreen)
        await pilot.press("f", "question_mark")
        assert app._prompt.text == "f?", "'?' is an ordinary character mid-prompt"


# ------------------------------------------------------------------------------- commands


async def test_info_commands_report_without_calling_the_model(work: Path):
    app, adapter = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        for command in ("/status", "/cost", "/todos", "/context", "/nonsense"):
            await send(pilot, app, command)
            await pilot.pause(0.15)
        text = "\n".join(notes(app))
        assert "sandbox" in text and REF in text
        assert "Tokens: 0 in" in text and "No task list yet" in text
        assert "context ≈" in text and "Unknown command /nonsense" in text
    assert adapter.requests == []


async def test_help_lists_every_builtin_command(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "/help")
        await screen_is(pilot, app, HelpScreen)
        from kalash.tui.slash import builtin_commands

        rendered = _render_text(app.screen.content)
        for command in builtin_commands():
            assert f"/{command.name}" in rendered


def _render_text(renderable: Any) -> str:
    console = Console(width=200, record=True, file=io.StringIO())
    console.print(renderable)
    return console.export_text()


async def test_permissions_command_sets_the_mode_and_rejects_unknown_ones(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "/permissions read-only")
        await pilot.pause(0.2)
        assert app.runtime.agent.mode == "read-only"
        assert app._status.data.mode_id == "read-only"
        await send(pilot, app, "/permissions banana")
        await pilot.pause(0.2)
        assert app.runtime.agent.mode == "read-only"
        assert any("Unknown mode" in n for n in notes(app))


async def test_permissions_picker_selects_with_the_keyboard(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "/permissions")
        await screen_is(pilot, app, PickerScreen)
        await pilot.press("down", "enter")
        await pilot.pause(0.2)
        assert app.runtime.agent.mode == "auto"


async def test_commands_that_need_an_idle_agent_are_refused_mid_turn(work: Path):
    app, _ = make_app(work, [Reply(text="busy", hang=True)])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "work")
        await until(pilot, lambda: app._turn_active)
        await send(pilot, app, "/new")
        await pilot.pause(0.2)
        assert any("can't run while the agent is working" in n for n in notes(app))
        await send(pilot, app, "/status")
        await pilot.pause(0.2)
        assert any("sandbox" in n for n in notes(app)), "read-only commands still work"
        await pilot.press("escape")
        await settle(pilot, app)


async def test_new_starts_a_fresh_session_and_clears_the_transcript(work: Path):
    app, _ = make_app(work, [say("first answer")])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "hello")
        await settle(pilot, app)
        before = app.runtime.session.id
        await send(pilot, app, "/new")
        await pilot.pause(0.4)
        assert app.runtime.session.id != before
        assert not list(app.query(UserMessage)) and not list(app.query(AssistantMessage))
        assert list(app.query(WelcomeBanner))


async def test_resume_restores_the_conversation_with_its_tool_blocks(work: Path):
    (work / "a.txt").write_text("x\n")
    app, _ = make_app(work, [call("read", {"file_path": "a.txt"}), say("first run done")])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "read it")
        await settle(pilot, app)
        session_id = app.runtime.session.id
    resumed, _ = make_app(work, [])
    resumed.session_ref = session_id
    async with resumed.run_test(size=SIZE) as pilot:
        await pilot.pause(0.4)
        assert [w.plain for w in resumed.query(UserMessage)] == ["read it"]
        assert [b.data.state for b in resumed.query(ToolBlock)] == ["ok"]
        assert [a.text for a in resumed.query(AssistantMessage)] == ["first run done"]


async def test_resume_does_not_show_the_empty_reply_nudge_as_something_the_user_said(work: Path):
    app, _ = make_app(
        work,
        [
            call("bash", {"description": "x", "command": "true"}),
            *[fail(ErrorCode.EMPTY_RESPONSE)] * 2,
            say("all done"),
        ],
    )
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "run it")
        await settle(pilot, app)
        session_id = app.runtime.session.id
        assert any("empty reply" in n for n in notes(app))
    resumed, _ = make_app(work, [])
    resumed.session_ref = session_id
    async with resumed.run_test(size=SIZE) as pilot:
        await pilot.pause(0.4)
        assert [w.plain for w in resumed.query(UserMessage)] == ["run it"]


async def test_compact_on_a_short_conversation_says_there_is_nothing_to_do_yet(work: Path):
    app, _ = make_app(work, [say("hi there")])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "hello")
        await settle(pilot, app)
        await send(pilot, app, "/compact")
        await pilot.pause(0.4)
        texts = notes(app)
        assert any("Nothing to compact yet" in n for n in texts), texts
        assert not any("too large" in n or "failed" in n for n in texts)


async def test_model_picker_lists_only_providers_with_keys_and_filters(
    work: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "/model")
        await screen_is(pilot, app, PickerScreen)
        options = app.screen.query_one(OptionList)
        ids = [str(options.get_option_at_index(i).id) for i in range(options.option_count)]
        providers = {i.split("/")[0] for i in ids}
        assert "anthropic" in providers
        assert providers <= {"anthropic", "ollama"}, "only keyed (or keyless local) providers"
        await pilot.press("o", "p", "u", "s")
        await pilot.pause(0.1)
        filtered = [str(options.get_option_at_index(i).id) for i in range(options.option_count)]
        assert filtered and all("opus" in i for i in filtered)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, PickerScreen)


async def test_switching_model_keeps_the_session_and_rejects_unknown_ones(
    work: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    app, _ = make_app(work, [say("one")])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "hello")
        await settle(pilot, app)
        session_id = app.runtime.session.id
        await send(pilot, app, "/model anthropic/claude-sonnet-5-5")
        await pilot.pause(0.5)
        assert str(app.runtime.ref) == "anthropic/claude-sonnet-5-5"
        assert app.runtime.session.id == session_id
        assert app.runtime.session.messages()[0].visible_text.strip() == "hello"
        assert app._status.data.model == "anthropic/claude-sonnet-5-5"
        await send(pilot, app, "/model nonsense/model")
        await pilot.pause(0.3)
        assert str(app.runtime.ref) == "anthropic/claude-sonnet-5-5"
        assert any("nonsense" in n for n in notes(app))


async def test_model_picker_shows_both_sarvam_glms_without_refresh(work: Path, monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "test-key")
    monkeypatch.setattr("kalash.llm.discovery.load_cache", dict)
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "/model")
        await screen_is(pilot, app, PickerScreen)
        await pilot.press("g", "l", "m")
        await pilot.pause(0.1)
        options = app.screen.query_one(OptionList)
        ids = [str(options.get_option_at_index(i).id) for i in range(options.option_count)]
        assert {"sarvam/glm5.3", "sarvam/glm5.3-flash"} <= set(ids)
        flash = ids.index("sarvam/glm5.3-flash")
        options.highlighted = flash
        await pilot.press("enter")
        await pilot.pause(0.5)
        assert str(app.runtime.ref) == "sarvam/glm5.3-flash"
        assert app.runtime.agent.adapter.base_url == "https://api.sarvam.ai/v2"


async def test_undo_reverts_the_agents_last_file_change(work: Path):
    (work / "f.txt").write_text("original\n")
    app, _ = make_app(
        work,
        [
            call("read", {"file_path": "f.txt"}),
            call("edit", {"file_path": "f.txt", "old_string": "original", "new_string": "changed"}),
            say("edited"),
        ],
    )
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "edit it")
        await settle(pilot, app)
        assert (work / "f.txt").read_text() == "changed\n"
        await send(pilot, app, "/undo")
        await pilot.pause(0.3)
        assert (work / "f.txt").read_text() == "original\n"
        assert any("restored" in n for n in notes(app))


async def test_theme_command_switches_and_persists(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "/theme kalash-light")
        await pilot.pause(0.3)
        assert app.theme == "kalash-light"
        assert app.palette.accent  # palette follows the theme
        await send(pilot, app, "/theme nope")
        await pilot.pause(0.2)
        assert any("Unknown theme" in n for n in notes(app))
    from kalash.config import load_settings as reload

    assert reload(work)[0].tui.get("theme") == "kalash-light"


async def test_copy_puts_the_last_answer_on_the_clipboard(work: Path, monkeypatch):
    app, _ = make_app(work, [say("copy **me**")])
    copied: list[str] = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "/copy")
        await pilot.pause(0.2)
        assert copied == []
        await send(pilot, app, "say it")
        await settle(pilot, app)
        await send(pilot, app, "/copy")
        await pilot.pause(0.2)
        assert copied == ["copy **me**"]


async def test_export_writes_the_conversation_as_markdown(work: Path):
    app, _ = make_app(work, [say("the answer")])
    target = work / "out.md"
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "question?")
        await settle(pilot, app)
        await send(pilot, app, f"/export {target}")
        await pilot.pause(0.3)
    text = target.read_text()
    assert "## You" in text and "question?" in text and "the answer" in text
    assert "system-reminder" not in text, "harness context is not part of the conversation"


async def test_custom_commands_expand_into_a_prompt(work: Path):
    (work / ".kalash" / "commands").mkdir(parents=True)
    (work / ".kalash" / "commands" / "review.md").write_text(
        "---\ndescription: review a file\n---\nReview $ARGUMENTS carefully.\n"
    )
    app, adapter = make_app(work, [say("reviewed")])
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("slash", "r", "e", "v")
        await pilot.pause(0.1)
        assert any(
            str(app._suggest.get_option_at_index(i).id) == "/review"
            for i in range(app._suggest.option_count)
        )
        await send(pilot, app, "/review main.py")
        await settle(pilot, app)
        assert [w.plain for w in app.query(UserMessage)] == ["/review main.py"]
    assert "Review main.py carefully." in adapter.requests[0].messages[0].text


# --------------------------------------------------------------------------------- layout


async def test_status_bar_degrades_gracefully_on_a_narrow_terminal(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=(48, 24)) as pilot:
        await pilot.pause(0.3)
        line = plain(app._status)
        assert "ASK" in line and REF in line
        assert len(line) <= 48


async def test_paging_keys_stop_following_until_the_user_returns_to_the_end(work: Path):
    app, _ = make_app(work, [say("\n".join(f"line {i}" for i in range(200)))])
    async with app.run_test(size=(110, 20)) as pilot:
        await send(pilot, app, "long please")
        await settle(pilot, app)
        assert app._stick is True
        assert app._transcript.max_scroll_y > 0
        await pilot.press("pageup")
        assert app._stick is False
        await pilot.press("ctrl+end")
        assert app._stick is True
        assert app._at_bottom()


async def test_mouse_wheel_up_over_an_answer_stays_scrolled_away(work: Path):
    app, _ = make_app(work, [say("\n\n".join(f"paragraph {i}" for i in range(80)))])
    async with app.run_test(size=(110, 20)) as pilot:
        await send(pilot, app, "long please")
        await settle(pilot, app)
        bottom = app._transcript.scroll_y
        await pilot._post_mouse_events([events.MouseScrollUp], "#transcript", offset=(10, 3))
        await pilot.pause(0.3)  # follow's periodic tick must not undo a wheel movement
        assert app._stick is False
        assert app._transcript.scroll_y < bottom
        app._on_event(Notice("new output below"))
        await pilot.pause(0.2)
        assert app._activity.behind and app._transcript.scroll_y < bottom


async def test_mouse_wheel_down_resumes_follow_on_reaching_the_end(work: Path):
    app, _ = make_app(work, [say("\n\n".join(f"paragraph {i}" for i in range(80)))])
    async with app.run_test(size=(110, 20)) as pilot:
        await send(pilot, app, "long please")
        await settle(pilot, app)
        await pilot.press("pageup")
        for _ in range(30):
            await pilot._post_mouse_events([events.MouseScrollDown], "#transcript", offset=(10, 3))
            if app._at_bottom():
                break
        assert app._at_bottom()
        assert app._stick is True


async def test_mouse_wheel_over_empty_prompt_scrolls_the_transcript(work: Path):
    app, _ = make_app(work, [say("\n\n".join(f"paragraph {i}" for i in range(80)))])
    async with app.run_test(size=(110, 20)) as pilot:
        await send(pilot, app, "long please")
        await settle(pilot, app)
        bottom = app._transcript.scroll_y
        await pilot._post_mouse_events([events.MouseScrollUp], "#prompt", offset=(5, 1))
        await pilot.pause(0.2)
        assert app._transcript.scroll_y < bottom
        assert not app._stick and app._prompt.has_focus


@pytest.mark.parametrize("modifier", ["shift", "control"])
async def test_modified_mouse_wheel_does_not_disable_follow(work: Path, modifier):
    app, _ = make_app(work, [say("\n\n".join(f"paragraph {i}" for i in range(80)))])
    async with app.run_test(size=(110, 20)) as pilot:
        await send(pilot, app, "long please")
        await settle(pilot, app)
        bottom = app._transcript.scroll_y
        await pilot._post_mouse_events(
            [events.MouseScrollUp], "#transcript", offset=(10, 3), **{modifier: True}
        )
        await pilot.pause(0.2)
        assert app._stick and app._transcript.scroll_y == bottom


async def test_mouse_wheel_in_a_dialog_does_not_change_transcript_follow(work: Path):
    app, _ = make_app(work, [say("\n\n".join(f"paragraph {i}" for i in range(80)))])
    async with app.run_test(size=(110, 20)) as pilot:
        await send(pilot, app, "long please")
        await settle(pilot, app)
        bottom = app._transcript.scroll_y
        await send(pilot, app, "/model")
        await screen_is(pilot, app, PickerScreen)
        await pilot._post_mouse_events([events.MouseScrollUp], offset=(0, 0))
        await pilot.pause(0.2)
        assert app._stick and app._transcript.scroll_y == bottom


@pytest.mark.parametrize("target", ["#transcript", "#prompt"])
async def test_trackpad_direction_changes_in_a_single_input_burst(work: Path, target):
    from textual._xterm_parser import XTermParser

    app, _ = make_app(work, [say("\n\n".join(f"paragraph {i}" for i in range(80)))])
    async with app.run_test(size=(110, 20)) as pilot:
        await send(pilot, app, "long please")
        await settle(pilot, app)
        bottom = app._transcript.scroll_y
        region = app.query_one(target).region
        parser = XTermParser()
        for _ in range(3):
            # A trackpad delivers many SGR wheel reports without a repaint between
            # them, including horizontal motion and a reversal of direction.
            codes = [64] * 12 + [65] * 12 + [66, 67] + [64] * 8
            for event in parser.feed(
                "".join(f"\x1b[<{code};{region.x + 6};{region.y + 2}M" for code in codes)
            ):
                app.post_message(event)
            await pilot.pause(0.3)
            assert not app._stick
            assert app._transcript.scroll_y < bottom
            for event in parser.feed(f"\x1b[<65;{region.x + 6};{region.y + 2}M" * 100):
                app.post_message(event)
            await pilot.pause(0.3)
            assert app._at_bottom() and app._stick


async def test_queued_follow_does_not_override_a_new_scroll_away(work: Path):
    app, _ = make_app(work, [say("\n\n".join(f"paragraph {i}" for i in range(80)))])
    async with app.run_test(size=(110, 20)) as pilot:
        await send(pilot, app, "long please")
        await settle(pilot, app)
        app._on_event(Notice("new output below"))
        app.action_page_up()
        await pilot.pause(0.3)
        assert not app._stick
        assert not app._at_bottom()


async def test_follow_tick_cannot_overtake_a_wheel_gesture(work: Path):
    app, _ = make_app(work, [say("\n\n".join(f"paragraph {i}" for i in range(80)))])
    async with app.run_test(size=(110, 20)) as pilot:
        await send(pilot, app, "long please")
        await settle(pilot, app)
        event = events.MouseScrollUp(None, 10, 3, 0, 0, 0, False, False, False)
        # Dispatch through the real widget handlers, then run the periodic tick
        # before the app can process any messages posted by those handlers.
        await app._transcript._dispatch_message(event)
        assert not app._at_bottom(), "Textual applied the wheel movement"
        app._tick()
        assert not app._stick
        assert not app._at_bottom(), "the timer must not undo that movement"


async def test_new_output_keeps_the_view_pinned_to_the_bottom_while_following(work: Path):
    app, _ = make_app(work, [say("\n\n".join(f"paragraph {i}" for i in range(60)))])
    async with app.run_test(size=(110, 20)) as pilot:
        await send(pilot, app, "write a lot")
        await settle(pilot, app)
        await pilot.pause(0.3)
        assert app._at_bottom()


async def test_layout_survives_a_resize(work: Path):
    app, _ = make_app(work, [say("fine")])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "hi")
        await settle(pilot, app)
        await pilot.resize_terminal(60, 20)
        await pilot.pause(0.2)
        await pilot.resize_terminal(140, 50)
        await pilot.pause(0.2)
        assert app._prompt.region.width > 100 and app._status.region.width == 140


# --------------------------------------------------------------------------------- lifecycle


async def test_quit_closes_the_runtime_and_background_jobs(work: Path):
    app, adapter = make_app(
        work,
        [
            call("bash", {"description": "bg", "command": "sleep 60", "run_in_background": True}),
            say("started"),
        ],
    )
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "start server")
        await settle(pilot, app)
        runtime = app.runtime
        job = runtime.jobs.get("job1")
        assert job is not None and job.running
        await pilot.press("ctrl+d")
        await pilot.pause(0.5)
    assert not job.running
    assert adapter.closed is False, "an injected adapter belongs to the caller"


async def test_quit_while_a_turn_hangs_does_not_deadlock(work: Path):
    app, _ = make_app(work, [Reply(text="never ends", hang=True)])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "go")
        await until(pilot, lambda: app._turn_active)
        await pilot.press("ctrl+d")
        await pilot.pause(0.5)
    assert not app.is_running


async def test_quit_command_exits(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "/quit")
        await pilot.pause(0.5)
    assert not app.is_running


# --------------------------------------------------------------------------- guard rails


def _assigned_names(cls: type) -> set[str]:
    """Every ``self.<name> = ...`` assignment in a class body."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(cls)))
    names: set[str] = set()

    def walk(node: ast.AST) -> Iterator[ast.AST]:
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, ast.ClassDef):  # nested Message classes have their own self
                yield child
                yield from walk(child)

    for node in walk(tree.body[0]):
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            for leaf in target.elts if isinstance(target, ast.Tuple) else [target]:
                if (
                    isinstance(leaf, ast.Attribute)
                    and isinstance(leaf.value, ast.Name)
                    and leaf.value.id == "self"
                ):
                    names.add(leaf.attr)
    return names


def _shadowed(cls: type, base: type, bare: object) -> set[str]:
    """Names ``cls`` assigns on ``self`` that the Textual base already uses."""
    return _assigned_names(cls) & (set(dir(base)) | set(vars(bare)))


def test_app_attributes_do_not_shadow_textual_internals():
    """Textual keeps private state under plain names (``_running``, ``_context``...); clobbering
    one breaks the app in ways that only show up at runtime."""
    assert _shadowed(KalashApp, App, App()) <= {"theme", "sub_title"}


PALETTE = Palette(
    accent="#f5a524",
    ok="#4ade80",
    error="#ff6b6b",
    warn="#e3b341",
    info="#2dd4bf",
    fg="#e6edf3",
    muted="#8b949e",
    added_fg="#7ee787",
    added_bg="#12261a",
    removed_fg="#ffa198",
    removed_bg="#2d1618",
    hunk="#79c0ff",
    plan="#2dd4bf",
)


@pytest.mark.parametrize(
    ("cls", "base", "make_base"),
    [
        (PromptArea, TextArea, lambda: TextArea()),
        (StatusBar, Static, lambda: Static("")),
        (ActivityBar, Static, lambda: Static("")),
        (ToolBlock, Vertical, lambda: Vertical()),
        (AssistantMessage, Vertical, lambda: Vertical()),
        (ReasoningRow, Static, lambda: Static("")),
        (UserMessage, Static, lambda: Static("")),
        (NoticeRow, Static, lambda: Static("")),
        (ErrorCard, Static, lambda: Static("")),
        (WelcomeBanner, Static, lambda: Static("")),
        (Suggestions, OptionList, lambda: OptionList()),
        (Transcript, VerticalScroll, lambda: VerticalScroll()),
    ],
)
def test_widget_attributes_do_not_shadow_textual_internals(cls, base, make_base):  # type: ignore[no-untyped-def]
    intended = (  # public setters
        {"display", "highlighted", "border_subtitle"} if cls is Suggestions else set()
    )
    assert _shadowed(cls, base, make_base()) <= intended


async def test_legacy_theme_names_in_old_settings_files_still_apply(work: Path):
    """The previous UI stored "light"/"dark"; those settings must not silently flip to dark."""
    app, _ = make_app(work, [], tui={"theme": "light", "dark": False})
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        assert app.theme == "kalash-light"
    unknown, _ = make_app(work, [], tui={"theme": "no-such-theme"})
    async with unknown.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        assert unknown.theme == "kalash-dark"


# ------------------------------------------------------------------------------ /provider


async def test_provider_command_stores_a_key_and_starts_the_configured_model(
    work: Path, monkeypatch: pytest.MonkeyPatch
):
    from kalash.credentials import stored_providers
    from kalash.tui.screens import KeyScreen

    settings = load_settings(work, {"model": "openai/gpt-5.6-sol"})[0]
    app = KalashApp(cwd=work, settings=settings)
    toasts: list[str] = []
    monkeypatch.setattr(
        app, "toast", lambda message, severity="information": toasts.append(message)
    )
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        assert app.runtime is None, "no key yet, so no runtime"
        await send(pilot, app, "/provider")
        await screen_is(pilot, app, PickerScreen)
        options = app.screen.query_one(OptionList)
        ids = [str(options.get_option_at_index(i).id) for i in range(options.option_count)]
        assert {"anthropic", "openai", "google", "sarvam", "ollama"} <= set(ids)
        await pilot.press("o", "p", "e", "n", "a", "i")
        await pilot.pause(0.1)
        await pilot.press("enter")
        await screen_is(pilot, app, KeyScreen)
        await pilot.press(*"sk-test-key")
        await pilot.press("enter")
        await until(pilot, lambda: app.runtime is not None)
        assert "openai" in stored_providers()
        assert str(app.runtime.ref) == "openai/gpt-5.6-sol"
        assert "Saved the key for OpenAI" in toasts
        assert len(list(app.query(WelcomeBanner))) == 1, "the banner is replaced, not stacked"


async def test_login_still_works_as_an_alias_and_unknown_providers_are_reported(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "/login nowhere")
        await pilot.pause(0.2)
        assert any("Unknown provider 'nowhere'" in n for n in notes(app))
        await send(pilot, app, "/provider ollama")
        await pilot.pause(0.2)
        assert any("needs no API key" in n for n in notes(app))


async def test_slash_popup_offers_provider_and_finds_it_through_the_old_name(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("slash", "l", "o", "g")
        await pilot.pause(0.1)
        values = [
            str(app._suggest.get_option_at_index(i).id) for i in range(app._suggest.option_count)
        ]
        assert values[0] == "/provider"


# ------------------------------------------------------------- tool header and the meter


async def test_bash_header_leads_with_the_models_description_and_dims_the_command(work: Path):
    command = "node -e \"console.log(require('fs').readFileSync('a.txt','utf8'))\""
    app, _ = make_app(
        work,
        [call("bash", {"description": "Print a.txt", "command": command}), say("done")],
    )
    (work / "a.txt").write_text("hello\n")
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "show it")
        await settle(pilot, app)
        head = plain(app.query_one(ToolBlock)._head)
        assert head.startswith("✓ Run  Print a.txt")
        assert "$ node -e" in head


async def test_context_meter_is_right_after_resuming_a_session(work: Path):
    app, _ = make_app(work, [say("an answer " * 50)])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "hello " * 50)
        await settle(pilot, app)
        session_id = app.runtime.session.id
    resumed, _ = make_app(work, [])
    resumed.session_ref = session_id
    async with resumed.run_test(size=SIZE) as pilot:
        await pilot.pause(0.4)
        assert resumed._status.data.used > 0 and resumed._status.data.window > 0
        assert "%" in plain(resumed._status)


async def test_a_repeating_failure_ends_in_an_error_card_that_says_what_to_do(work: Path):
    loop = call("bash", {"description": "Run the tests", "command": "echo 'Error: boom'; exit 1"})
    app, _ = make_app(work, [loop] * 6)
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "fix the tests")
        await settle(pilot, app, timeout=30)
        card = app.query_one(ErrorCard)
        assert "the same failure kept repeating" in card.plain
        assert "/undo" in card.plain and "6 times" in card.plain
        blocks = list(app.query(ToolBlock))
        assert len(blocks) == 6 and all(b.data.state == "error" for b in blocks)
        assert "Stop retrying" in blocks[4].data.output, "the 5th failure carried the warning"
        assert not app._turn_active


async def test_retired_token_limit_does_not_interrupt_work(work: Path):
    app, _ = make_app(
        work,
        [call("bash", {"description": "x", "command": "echo hi"}) for _ in range(4)]
        + [say("finished")],
        limits={"max_session_tokens": 150},
    )
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "work")
        await settle(pilot, app)
        assert not list(app.query(ErrorCard))
        assert len(list(app.query(ToolBlock))) == 4
        assert not app._turn_active


# ------------------------------------------------------------------- the full slash menu


def popup_values(app: KalashApp) -> list[str]:
    options = [app._suggest.get_option_at_index(i) for i in range(app._suggest.option_count)]
    return [str(o.id) for o in options if not o.disabled]


def popup_headers(app: KalashApp) -> list[str]:
    options = [app._suggest.get_option_at_index(i) for i in range(app._suggest.option_count)]
    return [o.prompt.plain.strip() for o in options if o.disabled]  # type: ignore[union-attr]


async def test_typing_just_a_slash_lists_every_command_and_mode_in_sections(work: Path):
    from kalash.tui.slash import builtin_commands

    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("slash")
        await pilot.pause(0.2)
        assert app._suggest.display
        values = popup_values(app)
        assert len(values) >= len(builtin_commands()), "nothing is capped"
        for mode in ("ask", "auto", "plan", "read-only", "full", "permissions"):
            assert f"/{mode}" in values
        assert values[:5] == ["/ask", "/auto", "/plan", "/read-only", "/full"]
        assert "/quit" in values and "/model" in values and "/provider" in values
        assert popup_headers(app)[0] == "MODES"
        assert {"MODES", "SESSION", "MODEL & KEYS", "INFO & TOOLS"} <= set(popup_headers(app))
        assert "commands" in (app._suggest.border_subtitle or "")


async def test_the_menu_scrolls_to_commands_far_down_the_list(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("slash")
        await pilot.pause(0.2)
        values = popup_values(app)
        for _ in range(len(values) - 1):
            await pilot.press("down")
        await pilot.pause(0.1)
        assert app._suggest.selected_value() == values[-1] == "/quit"
        assert app._suggest.scroll_y > 0, "the list scrolled to keep the selection in view"
        await pilot.press("up")
        assert app._suggest.selected_value() == values[-2]


async def test_arrow_keys_skip_the_section_headers(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("slash")
        await pilot.pause(0.2)
        seen = []
        for _ in range(8):
            seen.append(app._suggest.selected_value())
            await pilot.press("down")
        assert None not in seen and seen[:6] == [
            "/ask",
            "/auto",
            "/plan",
            "/read-only",
            "/full",
            "/permissions",
        ]
        assert seen[6] == "/new", "the SESSION header between them was skipped"


async def test_the_current_mode_is_marked_in_the_menu(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("shift+tab")  # ask -> auto
        await pilot.pause(0.1)
        await pilot.press("slash")
        await pilot.pause(0.2)
        rows = {
            str(app._suggest.get_option_at_index(i).id): app._suggest.get_option_at_index(i)
            for i in range(app._suggest.option_count)
        }
        assert "current" in rows["/auto"].prompt.plain  # type: ignore[union-attr]
        assert "current" not in rows["/ask"].prompt.plain  # type: ignore[union-attr]


async def test_each_mode_has_a_slash_command_that_switches_to_it(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        for command, expected in (
            ("/auto", "auto"),
            ("/plan", "plan"),
            ("/read-only", "read-only"),
            ("/readonly", "read-only"),
            ("/ask", "ask"),
        ):
            await send(pilot, app, command)
            await pilot.pause(0.2)
            assert app.runtime.agent.mode == expected, command
            assert app._status.data.mode_id == expected


async def test_full_access_needs_confirmation_and_declining_changes_nothing(work: Path):
    from kalash.tui.screens import ConfirmScreen

    app, _ = make_app(work, [])
    hook, armed_at_dispatch = key_on_dialog_mount(ConfirmScreen)
    async with app.run_test(size=SIZE, message_hook=hook) as pilot:
        await send(pilot, app, "/full")
        await screen_is(pilot, app, ConfirmScreen)
        assert armed_at_dispatch == [False], "stray typing reached the actual unarmed dialog"
        assert isinstance(app.screen, ConfirmScreen) and app.runtime.agent.mode == "ask"
        await pilot.press("n")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, ConfirmScreen) and app.runtime.agent.mode == "ask"
        await send(pilot, app, "/permissions yolo")  # the alias asks too
        await screen_is(pilot, app, ConfirmScreen)
        await pilot.pause(0.6)
        await pilot.press("y")
        await pilot.pause(0.3)
        assert app.runtime.agent.mode == "full" and app._status.data.mode_id == "full"
        await send(pilot, app, "/full")  # already there: no second question
        await pilot.pause(0.3)
        assert not isinstance(app.screen, ConfirmScreen)


async def test_escape_cancels_the_full_access_question(work: Path):
    from kalash.tui.screens import ConfirmScreen

    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "/full")
        await screen_is(pilot, app, ConfirmScreen)
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert app.runtime.agent.mode == "ask"


async def test_skills_and_custom_commands_are_listed_and_runnable(work: Path):
    (work / ".kalash" / "skills" / "deploy").mkdir(parents=True)
    (work / ".kalash" / "skills" / "deploy" / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: Ship the build to staging\n---\nStep 1.\n"
    )
    (work / ".kalash" / "commands").mkdir(parents=True)
    (work / ".kalash" / "commands" / "review.md").write_text(
        "---\ndescription: review a file\n---\nReview $ARGUMENTS.\n"
    )
    app, adapter = make_app(work, [say("deploying"), say("reviewed")])
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("slash")
        await pilot.pause(0.2)
        assert {"/review", "/deploy"} <= set(popup_values(app))
        assert {"CUSTOM COMMANDS", "SKILLS"} <= set(popup_headers(app))
        await pilot.press("escape")
        await send(pilot, app, "/deploy the hotfix")
        await settle(pilot, app)
        assert [w.plain for w in app.query(UserMessage)] == ["/deploy the hotfix"]
    sent = adapter.requests[0].messages[-1]
    assert "Use the `deploy` skill for this task: the hotfix" in sent.text


async def test_tab_completes_only_the_command_word_of_a_multi_line_draft(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        app._prompt.focus()
        app._prompt.set_text("/mo\nsecond line of my draft")
        app._prompt.move_cursor((0, 3))
        await pilot.pause(0.1)
        assert app._suggest.display
        await pilot.press("tab")
        await pilot.pause(0.1)
        assert app._prompt.text == "/model \nsecond line of my draft"


async def test_enter_on_a_popup_completes_instead_of_running_when_there_is_more_text(
    work: Path,
):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        app._prompt.focus()
        app._prompt.set_text("/mo\nand my notes")
        app._prompt.move_cursor((0, 3))
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app._prompt.text == "/model \nand my notes"
        assert not isinstance(app.screen, PickerScreen), "the draft was not swallowed"


async def test_a_message_that_cannot_be_sent_stays_in_the_prompt(work: Path):
    app = KalashApp(cwd=work, settings=load_settings(work)[0])
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await send(pilot, app, "my long careful question")
        await pilot.pause(0.2)
        assert app._prompt.text == "my long careful question"
        assert any("No model is configured" in n for n in notes(app))


async def test_resumed_transcript_shows_only_what_the_user_typed(work: Path):
    (work / "AGENTS.md").write_text("Instruction text that must not show up as a user message.")
    app, _ = make_app(work, [say("an answer")])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "my question")
        await settle(pilot, app)
        session_id = app.runtime.session.id
    resumed, _ = make_app(work, [])
    resumed.session_ref = session_id
    async with resumed.run_test(size=SIZE) as pilot:
        await pilot.pause(0.4)
        assert [w.plain for w in resumed.query(UserMessage)] == ["my question"]


# ------------------------------------------------------------------------------ effort


def sarvam_info():  # type: ignore[no-untyped-def]
    from kalash.llm.catalog import CATALOG

    return CATALOG["sarvam"]["sarvam-105b"]


async def test_effort_on_a_model_without_a_reasoning_control_says_so(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "/effort")
        await pilot.pause(0.2)
        assert any("no reasoning control" in n for n in notes(app))
        await pilot.press("ctrl+t")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, PickerScreen)


async def test_effort_picker_lists_only_what_the_model_accepts_and_applies_the_choice(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        app.runtime.agent.info = sarvam_info()
        await send(pilot, app, "/effort")
        await screen_is(pilot, app, PickerScreen)
        options = app.screen.query_one(OptionList)
        ids = [str(options.get_option_at_index(i).id) for i in range(options.option_count)]
        assert ids == ["default", "auto", "low", "high", "off"]
        shown = options.get_option_at_index(0).prompt.plain  # type: ignore[union-attr]
        assert "current" in shown, "the model default is the current setting"
        await pilot.press("down", "down", "enter")  # -> low
        await pilot.pause(0.3)
        assert app.runtime.agent.effort_setting == "low"
        assert app._status.data.effort == "low" and "low" in plain(app._status)


async def test_effort_command_validates_substitutes_and_refuses(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        app.runtime.agent.info = sarvam_info()
        await send(pilot, app, "/effort off")
        await pilot.pause(0.2)
        assert app.runtime.agent.effort_setting == "off"
        from kalash.llm.catalog import CATALOG

        app.runtime.agent.info = CATALOG["groq"]["openai/gpt-oss-20b"]
        app.runtime.agent.set_effort("high")
        await send(pilot, app, "/effort off")
        await pilot.pause(0.2)
        assert any("cannot be turned off" in n for n in notes(app))
        assert app.runtime.agent.effort_setting == "high"
        app.runtime.agent.info = sarvam_info()
        await send(pilot, app, "/effort medium")  # not offered: nearest is used, and said
        await pilot.pause(0.2)
        assert app.runtime.agent.effort_setting == "low"
        await send(pilot, app, "/effort auto")
        await pilot.pause(0.2)
        assert app.runtime.agent.effort_setting == "auto" and "auto" in plain(app._status)
        await send(pilot, app, "/effort default")
        await pilot.pause(0.2)
        assert app.runtime.agent.effort_setting is None


async def test_ctrl_t_cycles_through_the_levels_the_model_has(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        app.runtime.agent.info = sarvam_info()
        seen = []
        for _ in range(4):
            await pilot.press("ctrl+t")
            await pilot.pause(0.1)
            seen.append(app.runtime.agent.effort_setting)
        assert seen == ["low", "high", "off", None]


async def test_the_status_bar_follows_auto_effort_per_step(work: Path):
    app, adapter = make_app(
        work,
        [
            call("bash", {"description": "ok", "command": "true"}),
            say("done"),
        ],
    )
    async with app.run_test(size=SIZE) as pilot:
        app.runtime.agent.info = sarvam_info()
        await send(pilot, app, "/effort auto")
        await pilot.pause(0.2)
        await send(pilot, app, "go")
        await settle(pilot, app)
        assert [r.reasoning_effort for r in adapter.requests] == ["high", "low"]
        assert "auto:low" in plain(app._status)


# ------------------------------------------------------------------- transcript polish


def files(work: Path, *names: str) -> None:
    for name in names:
        (work / name).write_text(f"{name}\n")


async def test_file_headers_use_paths_relative_to_the_working_directory(work: Path):
    (work / "src").mkdir()
    files(work, "src/app.js")
    app, _ = make_app(work, [call("read", {"file_path": str(work / "src" / "app.js")}), say("ok")])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "look")
        await settle(pilot, app)
        head = plain(app.query_one(ToolBlock)._head)
        assert "src/app.js" in head and str(work) not in head


async def test_a_run_of_reads_folds_into_one_explored_row_that_unfolds_on_click(work: Path):
    files(work, "a.txt", "b.txt", "c.txt")
    app, _ = make_app(
        work,
        [
            calls(*(("read", {"file_path": f"{n}.txt"}) for n in "abc")),
            say("read them all"),
        ],
    )
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "read three files")
        await settle(pilot, app)
        group = app.query_one(ExploreGroup)
        assert len(group.members) == 3 and not any(b.display for b in group.members)
        assert "Explored" in group.plain and "read 3 files" in group.plain
        assert "a.txt, b.txt, c.txt" in group.plain
        group.on_click()
        await pilot.pause(0.1)
        assert all(b.display for b in group.members) and "▾" in group.plain
        group.on_click()
        assert not any(b.display for b in group.members)


async def test_ctrl_o_unfolds_every_explored_row(work: Path):
    files(work, "a.txt", "b.txt")
    app, _ = make_app(
        work, [calls(("read", {"file_path": "a.txt"}), ("grep", {"pattern": "a"})), say("done")]
    )
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "look around")
        await settle(pilot, app)
        group = app.query_one(ExploreGroup)
        assert "1 search" in group.plain
        await pilot.press("ctrl+o")
        await pilot.pause(0.1)
        assert group.open and all(b.display for b in group.members)
        await pilot.press("ctrl+o")
        await pilot.pause(0.1)
        assert not group.open


async def test_a_single_read_an_edit_or_a_failure_is_never_folded(work: Path):
    files(work, "a.txt", "b.txt")
    app, _ = make_app(
        work,
        [
            call("read", {"file_path": "a.txt"}),
            call("edit", {"file_path": "a.txt", "old_string": "a.txt", "new_string": "A"}),
            call("read", {"file_path": "b.txt"}),
            call("read", {"file_path": "missing.txt"}),
            call("read", {"file_path": "b.txt"}),
            say("done"),
        ],
    )
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "mixed")
        await settle(pilot, app)
        assert not list(app.query(ExploreGroup)), "nothing had two neighbours that finished fine"
        assert all(b.display for b in app.query(ToolBlock))


async def test_a_failed_read_ends_a_run_and_stays_visible(work: Path):
    files(work, "a.txt", "b.txt", "c.txt")
    app, _ = make_app(
        work,
        [
            call("read", {"file_path": "a.txt"}),
            call("read", {"file_path": "b.txt"}),
            call("read", {"file_path": "missing.txt"}),
            call("read", {"file_path": "c.txt"}),
            say("done"),
        ],
    )
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "go")
        await settle(pilot, app)
        groups = list(app.query(ExploreGroup))
        assert len(groups) == 1 and len(groups[0].members) == 2
        failed = [b for b in app.query(ToolBlock) if b.data.state == "error"]
        assert len(failed) == 1 and failed[0].display and failed[0].group is None


async def test_resuming_folds_old_reads_too(work: Path):
    files(work, "a.txt", "b.txt")
    app, _ = make_app(
        work, [calls(("read", {"file_path": "a.txt"}), ("read", {"file_path": "b.txt"})), say("ok")]
    )
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "read both")
        await settle(pilot, app)
        session_id = app.runtime.session.id
    resumed, _ = make_app(work, [])
    resumed.session_ref = session_id
    async with resumed.run_test(size=SIZE) as pilot:
        await pilot.pause(0.5)
        groups = list(resumed.query(ExploreGroup))
        assert len(groups) == 1 and len(groups[0].members) == 2


async def test_a_finished_turn_ends_with_a_one_line_summary(work: Path):
    files(work, "a.txt")
    app, _ = make_app(
        work,
        [
            call("read", {"file_path": "a.txt"}),
            call("edit", {"file_path": "a.txt", "old_string": "a.txt", "new_string": "alpha"}),
            say("done"),
        ],
    )
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "change it")
        await settle(pilot, app)
        footer = app.query_one(TurnFooter).plain
        assert footer.startswith("✓ ") and "2 tool calls" in footer
        assert "edited 1 file +1 −1" in footer and " in · " in footer
        assert list(app._transcript.children)[-1] is app.query_one(TurnFooter)


async def test_a_trivial_answer_gets_no_footer(work: Path):
    app, _ = make_app(work, [say("hello")])
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "hi")
        await settle(pilot, app)
        assert not list(app.query(TurnFooter))


async def test_failed_turns_show_the_error_card_not_a_footer(work: Path):
    app, _ = make_app(work, [fail(ErrorCode.SERVER, "provider down")] * 3)
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "hi")
        await settle(pilot, app, timeout=30)
        assert list(app.query(ErrorCard)) and not list(app.query(TurnFooter))


async def test_reasoning_rows_show_how_the_thought_began(work: Path):
    app, _ = make_app(
        work,
        [Reply(reasoning="Let me check the failing test first.\nThen I will fix it.", text="ok")],
    )
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "think")
        await settle(pilot, app)
        row = app.query_one(ReasoningRow)
        assert "Thought · Let me check the failing test first." in plain(row)
        assert "(12 words)" in plain(row)


async def test_whitespace_only_rewrites_are_flagged_in_the_header(work: Path):
    (work / "q.js").write_text("const a = {\n  id: 0,\n  id: 1,\n};\n")
    app, _ = make_app(
        work,
        [
            call("read", {"file_path": "q.js"}),
            call(
                "write",
                {"file_path": "q.js", "content": "const a = {\n  id:   0,\n  id:   1,\n};\n"},
            ),
            say("rewrote"),
        ],
    )
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, app, "tidy")
        await settle(pilot, app)
        write = [b for b in app.query(ToolBlock) if b.data.name == "write"][0]
        assert "whitespace only" in plain(write._head)
        assert "differ only in whitespace" in app.runtime.session.messages()[-2].text


async def test_scrolled_away_users_are_told_that_newer_output_exists(work: Path):
    app, _ = make_app(work, [])
    async with app.run_test(size=SIZE) as pilot:
        app._stick = False
        app._on_event(Notice("something new"))
        await pilot.pause(0.1)
        assert app._activity.behind and "new output" in plain(app._activity)
        await pilot.press("ctrl+end")
        await pilot.pause(0.1)
        assert not app._activity.behind and "new output" not in plain(app._activity)


async def test_mcp_command_describes_each_server_and_what_failed(work: Path):
    app, _ = make_app(
        work,
        [],
        mcp_servers={
            "wiki": {"url": "https://mcp.example.test/mcp", "trust": True},
            "down": {"url": "https://down.example.test/mcp"},
        },
    )
    async with app.run_test(size=SIZE) as pilot:
        client = HttpMCPClient("wiki", McpServer(url="https://mcp.example.test/mcp", trust=True))
        client.era, client.version = "legacy", "2025-06-18"
        tool = MCPTool(client, {"name": "ask_question", "description": "d"}, trusted=True)
        app.runtime.mcp = MCPManager([client], [tool], ["MCP server 'down' unavailable: refused"])
        await send(pilot, app, "/mcp")
        await pilot.pause(0.2)
        text = "\n".join(notes(app))
        assert (
            "✓ wiki (remote, legacy protocol 2025-06-18, trusted): 1 tools - ask_question" in text
        )
        assert "✗ down: not connected" in text and "unavailable: refused" in text


# ------------------------------------------------------------------------- MCP trust


def app_with_untrusted_mcp(work: Path, script: list[Reply]):  # type: ignore[no-untyped-def]
    return make_app(work, script, mcp_servers={"time": {"url": "https://mcp.example.test/mcp"}})


async def test_mcp_trust_command_lets_a_server_run_without_asking_for_the_session(work: Path):
    app, _ = app_with_untrusted_mcp(work, [])
    async with app.run_test(size=SIZE) as pilot:
        client = HttpMCPClient("time", app.settings.mcp_servers["time"])
        tool = MCPTool(client, {"name": "now", "description": "d"}, trusted=False)
        app.runtime.mcp = MCPManager([client], [tool], [])
        assert tool.needs_approval
        await send(pilot, app, "/mcp trust time")
        await pilot.pause(0.2)
        assert not tool.needs_approval and app.settings.mcp_servers["time"].trust
        assert any(
            "tools now run without asking" in n and "kalash mcp trust time" in n for n in notes(app)
        )
        await send(pilot, app, "/mcp untrust time")
        await pilot.pause(0.2)
        assert tool.needs_approval
        await send(pilot, app, "/mcp trust nothing")
        await pilot.pause(0.2)
        assert any("No connected MCP server named 'nothing'" in n for n in notes(app))


async def test_a_denied_mcp_call_in_auto_mode_tells_the_user_how_to_allow_it(work: Path):
    app, _ = app_with_untrusted_mcp(work, [call("mcp__time__now", {}), say("could not")])
    async with app.run_test(size=SIZE) as pilot:
        client = HttpMCPClient("time", app.settings.mcp_servers["time"])
        tool = MCPTool(client, {"name": "now", "description": "d"}, trusted=False)
        app.runtime.mcp = MCPManager([client], [tool], [])
        app.runtime.agent.registry.add(tool)
        await send(pilot, app, "/auto")
        await pilot.pause(0.2)
        await send(pilot, app, "what time is it")
        await settle(pilot, app)
        denial = next(n for n in notes(app) if n.startswith("Denied: mcp__time__now"))
        assert "/mcp trust time" in denial and "Do not retry" not in denial
        tool_result = [m for m in app.runtime.session.messages() if m.role == "tool"][0]
        assert "`/mcp trust time`" in tool_result.text, "the model can relay the remedy"


def test_approval_dialogs_name_mcp_actions_readably():
    from kalash.tui.describe import mcp_server_of, pretty_action

    assert pretty_action('mcp__time__get_current_time {"timezone": "UTC"}') == (
        'MCP time · get_current_time  {"timezone": "UTC"}'
    )
    assert pretty_action("write src/a.py") == "write src/a.py"
    assert mcp_server_of("mcp__time__get_current_time: Permission denied: x") == "time"
    assert mcp_server_of("write: nope") is None
