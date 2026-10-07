"""Offline reproductions of disappearing output and misordered streaming turns."""

# These regression tests exercise the internal event-to-widget boundary.
# ruff: noqa: SLF001

import pytest

from kalash.core.events import Event, EventBus, EventType
from kalash.tui.app import KalashApp
from kalash.tui.messages import AssistantMessage, SystemMessage, ToolCallLine
from tests.render import plain


def test_tool_progress_does_not_erase_output():
    line = ToolCallLine("$ pytest")
    line.append_output("collected 12 tests\n")
    line.tick(2)
    assert "collected 12 tests" in plain(line.renderable)


@pytest.mark.parametrize("chunks", [["one\n", "tw", "o\n"], ["one", "\ntwo", "\n"]])
def test_tool_output_preserves_lines_across_arbitrary_chunks(chunks):
    line = ToolCallLine("$ check")
    for chunk in chunks:
        line.append_output(chunk)
    assert line.plain_output.splitlines() == ["one", "two"]


async def test_last_chunk_is_painted_even_when_provider_pauses():
    app = KalashApp()
    async with app.run_test() as pilot:
        message = AssistantMessage()
        await app._post(message)
        message.append("first ")
        message.append("last")
        await pilot.pause(0.12)
        assert "first last" in plain(message.renderable)


async def test_assistant_tool_assistant_stays_in_chronological_order():
    app = KalashApp()
    bus = EventBus()
    async with app.run_test() as pilot:
        app._subscribe_tool_events(bus)
        initial = AssistantMessage()
        app._current_assistant = initial
        await app._post(initial)
        emit = app._make_sink(initial)
        emit("I will check the tests.")
        await bus.emit(
            Event(
                type=EventType.TOOL_START,
                data={
                    "tool_name": "shell",
                    "tool_use_id": "t1",
                    "arguments": {"command": "pytest"},
                },
            )
        )
        await bus.emit(
            Event(
                type=EventType.TOOL_COMPLETE,
                data={
                    "tool_name": "shell",
                    "tool_use_id": "t1",
                    "ok": True,
                },
            )
        )
        await bus.emit(Event(type=EventType.TURN_START, data={"iteration": 2}))
        emit("The tests passed.")
        await pilot.pause(0.12)
        entries = list(app._transcript().children)
        assert [type(entry) for entry in entries] == [
            AssistantMessage,
            ToolCallLine,
            AssistantMessage,
        ]
        assert entries[0].plain_text == "I will check the tests."
        assert entries[2].plain_text == "The tests passed."


async def test_new_entries_do_not_pull_reader_away_from_history():
    app = KalashApp()
    async with app.run_test(size=(70, 20)) as pilot:
        await app._post(SystemMessage("\n".join(f"line {i}" for i in range(100))))
        await pilot.pause()
        app.action_scroll_to_home()
        await pilot.pause()
        await app._post(ToolCallLine("Read next.py"))
        await pilot.pause()
        assert app._transcript().scroll_y == 0


def test_stream_error_keeps_partial_response():
    message = AssistantMessage()
    message.append("Already received text.")
    message.set_error("connection lost")
    rendered = plain(message.renderable)
    assert "Already received text." in rendered
    assert "connection lost" in rendered


def test_tool_failure_shows_actionable_error():
    from kalash.tui.formatting import summarize_tool_result

    summary = summarize_tool_result(
        "edit",
        {
            "error": "File changed since the last read.",
            "remediation": "Read the file again before editing.",
        },
    )
    assert "File changed" in summary
    assert "Read the file again" in summary


@pytest.mark.parametrize("size", [(100, 30), (52, 18)])
async def test_long_stream_follows_tail_and_keeps_input_visible(size):
    from textual.widgets import Input

    app = KalashApp()
    async with app.run_test(size=size) as pilot:
        message = AssistantMessage()
        await app._post(message)
        for index in range(80):
            message.append(f"line {index}\n")
        await pilot.pause(0.15)
        transcript = app._transcript()
        assert transcript.scroll_y == transcript.max_scroll_y
        assert app.query_one(Input).region.bottom <= size[1]
        app.action_scroll_to_home()
        await pilot.pause()
        message.append("latest line\n")
        await pilot.pause(0.15)
        assert transcript.scroll_y == 0
        app.action_scroll_to_end()
        await pilot.pause()
        message.append("newest line\n")
        message.finish()
        await pilot.pause()
        assert transcript.scroll_y == transcript.max_scroll_y
        assert "newest line" in plain(message.renderable)


async def test_empty_error_survives_final_cleanup():
    app = KalashApp()
    async with app.run_test():
        message = AssistantMessage()
        app._current_assistant = message
        await app._post(message)
        message.set_error("provider unavailable")
        app._finish_assistant()
        assert message.display
        assert "provider unavailable" in plain(message.renderable)


@pytest.mark.parametrize(("key", "expected"), [("escape", "d"), ("a", "a")])
async def test_streaming_and_posting_continue_behind_approval_modal(key, expected):
    from kalash.permissions.prompt import ApprovalResponse, PromptContext
    from kalash.tui.approval import ApprovalScreen

    app = KalashApp()
    decisions = []
    async with app.run_test() as pilot:
        message = AssistantMessage()
        await app._post(message)
        transcript = app._transcript()
        await pilot.pause()
        app.busy = True
        modal = ApprovalScreen(
            PromptContext(
                action="write",
                tool_name="write",
                command="",
                paths=["sample.py"],
                capabilities=["fs.write"],
                risk_class="write",
                reversibility="reversible",
            )
        )
        await app.push_screen(modal, decisions.append)
        message.append("Output received while waiting for permission.")
        app._queue_follow()
        await pilot.pause(0.12)
        tool = ToolCallLine("Read other.py")
        await app._post(tool)
        tool.append_output("read complete\n")
        await pilot.pause(0.12)
        assert app.screen is modal
        assert decisions == []
        assert app._transcript() is transcript
        assert tool.parent is transcript
        app.busy = False
        original_mode = app.mode
        await pilot.press("tab", "up", "down")
        assert app.mode == original_mode
        assert decisions == []
        await pilot.press(key)
        await pilot.pause()
        assert decisions[0].response == ApprovalResponse(expected)
        assert "Output received" in plain(message.renderable)
        from textual.widgets import Input

        assert app.query_one(Input).placeholder == "Ask anything…"


async def test_pending_follow_is_safe_after_transcript_unmount():
    app = KalashApp()
    async with app.run_test() as pilot:
        transcript = app._transcript()
        await transcript.remove()
        app._queue_follow()
        await pilot.pause()
        assert not app._follow_pending


async def test_command_palette_owns_escape_while_response_streams():
    app = KalashApp()
    async with app.run_test() as pilot:
        message = AssistantMessage()
        await app._post(message)
        conversation_screen = app.screen
        app.busy = True
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert app.screen is not conversation_screen
        message.append("still streaming")
        await pilot.pause(0.12)
        app.busy = False
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is conversation_screen
        assert "still streaming" in plain(message.renderable)
        from textual.widgets import Input

        assert app.query_one(Input).placeholder == "Ask anything…"


async def test_late_tool_events_do_not_change_another_running_tool():
    app = KalashApp()
    bus = EventBus()
    async with app.run_test():
        app._subscribe_tool_events(bus)
        for call_id in ("finished", "running"):
            await bus.emit(
                Event(
                    type=EventType.TOOL_START,
                    data={
                        "tool_name": "read",
                        "tool_use_id": call_id,
                    },
                )
            )
        running = app._active_tools["running"]
        await bus.emit(
            Event(
                type=EventType.TOOL_COMPLETE,
                data={
                    "tool_name": "read",
                    "tool_use_id": "finished",
                    "ok": True,
                },
            )
        )
        await bus.emit(
            Event(
                type=EventType.TOOL_OUTPUT,
                data={
                    "tool_use_id": "finished",
                    "chunk": "late output from completed call",
                },
            )
        )
        assert running.plain_output == ""
        await bus.emit(
            Event(
                type=EventType.TOOL_DENIED,
                data={
                    "tool_use_id": "unknown",
                    "reason": "not allowed",
                },
            )
        )
        assert not running.is_done
