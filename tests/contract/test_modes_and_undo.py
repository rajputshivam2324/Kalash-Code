"""Runtime permission modes, plan mode, undo checkpoints, diffs, approval previews, user shell."""

from __future__ import annotations

from pathlib import Path

from kalash.agent.events import ToolDone
from kalash.modes import MODES, initial_mode, next_mode, resolve_mode
from tests.harness.scripted import call, say

# ----------------------------------------------------------------------------- modes


def test_modes_are_consistent_and_cycle_never_reaches_full_access():
    assert initial_mode("workspace-write", "ask") == "ask"
    assert initial_mode("workspace-write", "never") == "auto"
    assert initial_mode("read-only", "ask") == "read-only"
    assert initial_mode("danger-full-access", "ask") == "full"
    seen, mode = [], "ask"
    for _ in range(6):
        mode = next_mode(mode)
        seen.append(mode)
    assert set(seen) == {"ask", "auto", "plan"} and "full" not in seen
    assert resolve_mode("YOLO") is MODES["full"] and resolve_mode("nonsense") is None
    assert MODES["plan"].sandbox == "read-only" and MODES["plan"].plan


async def test_default_mode_follows_settings(make_harness):
    ask = await make_harness([])
    auto = await make_harness([], permissions={"approval": "never"})
    assert (ask.agent.mode, auto.agent.mode) == ("ask", "auto")


async def test_switching_to_plan_blocks_writes_and_tells_the_model_on_the_next_prompt(
    make_harness, work: Path
):
    h = await make_harness(
        [
            call("write", {"file_path": "x.txt", "content": "no"}),
            say("I cannot write in plan mode."),
        ]
    )
    h.agent.set_mode("plan")
    assert h.session.events[-1].type == "mode" and h.session.events[-1].data["mode"] == "plan"
    result = await h.turn("create x.txt")
    assert not (work / "x.txt").exists() and "read-only" in result.denials[0]
    first = h.adapter.requests[0].messages[0]
    assert "Permission mode: PLAN" in first.text and "from ASK to PLAN" in first.text
    assert first.visible_text.strip() == "create x.txt", "the user's own words stay separate"
    # delivered once, not repeated
    h.adapter.script.append(say("again"))
    await h.turn("second")
    last = h.adapter.requests[-1].messages[-1]
    assert last.text.strip() == "second", "an unchanged snapshot is not sent again"


async def test_mode_change_mid_turn_does_not_inject_a_message_into_a_tool_exchange(make_harness):
    h = await make_harness([call("todo_write", {"todos": []}), say("ok")])
    h.agent.tool_ctx.extras["set_mode"]("auto", announce=False)
    await h.turn("go")
    roles = [m.role for m in h.session.messages()]
    assert roles == ["user", "assistant", "tool", "assistant"]


async def test_plan_approval_switches_mode_and_allows_the_work_in_the_same_turn(
    make_harness, work: Path
):
    seen: list[str] = []

    async def decide(plan: str):
        seen.append(plan)
        return "approve", ""

    h = await make_harness(
        [
            call("exit_plan_mode", {"plan": "1. create done.txt"}),
            call("write", {"file_path": "done.txt", "content": "ok"}),
            say("implemented"),
        ]
    )
    h.agent.plan_decider = decide
    h.agent.set_mode("plan")
    result = await h.turn("plan then do it")
    assert seen == ["1. create done.txt"] and h.agent.mode == "ask"
    assert (work / "done.txt").read_text() == "ok" and not result.denials
    approval = [m for m in h.session.messages() if m.role == "tool"][0]
    assert "approved" in approval.text and not approval.is_error


async def test_plan_rejection_keeps_plan_mode_and_returns_the_feedback(make_harness):
    async def decide(plan: str):
        return "keep", "also cover the tests"

    h = await make_harness([call("exit_plan_mode", {"plan": "p"}), say("revising")])
    h.agent.plan_decider = decide
    h.agent.set_mode("plan")
    await h.turn("go")
    assert h.agent.mode == "plan"
    result = [m for m in h.session.messages() if m.role == "tool"][0]
    assert result.is_error and "also cover the tests" in result.text


async def test_exit_plan_mode_outside_plan_mode_and_without_ui_are_clear_errors(make_harness):
    h = await make_harness([call("exit_plan_mode", {"plan": "p"}), say("fine")])
    await h.turn("go")
    assert "Not in plan mode" in [m for m in h.session.messages() if m.role == "tool"][0].text
    h2 = await make_harness([call("exit_plan_mode", {"plan": "p"}), say("fine")])
    h2.agent.set_mode("plan")
    h2.agent.plan_decider = None
    await h2.turn("go")
    text = [m for m in h2.session.messages() if m.role == "tool"][0].text
    assert "stay in plan mode" in text or "interactive" in text


# ----------------------------------------------------------------------------- undo


async def test_undo_removes_a_created_file_and_tells_the_model(make_harness, work: Path):
    h = await make_harness(
        [call("write", {"file_path": "new.txt", "content": "hi"}), say("done"), say("noted")]
    )
    await h.turn("create new.txt")
    change = [e for e in h.session.events if e.type == "file_change"][0]
    assert change.data["existed"] is False and change.data["path"].endswith("new.txt")
    message = h.agent.undo_last_turn()
    assert "removed" in message and not (work / "new.txt").exists()
    await h.turn("what happened?")
    assert "undid your file changes" in h.adapter.requests[-1].messages[-1].text
    assert h.agent.undo_last_turn().startswith("Nothing to undo")


async def test_undo_restores_edited_content_byte_for_byte(make_harness, work: Path):
    (work / "a.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    h = await make_harness(
        [
            call("read", {"file_path": "a.py"}),
            call("edit", {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 10"}),
            call("edit", {"file_path": "a.py", "old_string": "y = 2", "new_string": "y = 20"}),
            say("done"),
        ]
    )
    await h.turn("edit twice")
    assert (work / "a.py").read_bytes() == b"x = 10\r\ny = 20\r\n"
    h.agent.undo_last_turn()
    assert (work / "a.py").read_bytes() == b"x = 1\r\ny = 2\r\n"


async def test_undo_never_overwrites_the_users_later_edits(make_harness, work: Path):
    h = await make_harness([call("write", {"file_path": "n.txt", "content": "agent"}), say("ok")])
    await h.turn("write")
    (work / "n.txt").write_text("user changed it afterwards")
    message = h.agent.undo_last_turn()
    assert "changed since the agent edited it" in message
    assert (work / "n.txt").read_text() == "user changed it afterwards"


async def test_undo_skips_the_whole_file_when_user_content_matches_an_intermediate_edit(
    make_harness, work: Path
):
    path = work / "a.txt"
    path.write_text("original")
    h = await make_harness(
        [
            call("read", {"file_path": "a.txt"}),
            call("edit", {"file_path": "a.txt", "old_string": "original", "new_string": "middle"}),
            call("edit", {"file_path": "a.txt", "old_string": "middle", "new_string": "last"}),
            call("write", {"file_path": "b.txt", "content": "independent change"}),
            say("edited twice"),
        ]
    )
    await h.turn("edit it twice")
    path.write_text("middle")  # user's later edit happens to match the earlier agent version

    report = h.agent.undo_last_turn()

    assert "changed since the agent edited it" in report
    assert path.read_text() == "middle", "skipping the last edit must also skip earlier edits"
    assert not (work / "b.txt").exists(), "unrelated files can still be undone"


async def test_undo_reverts_only_the_latest_turn_then_the_one_before(make_harness, work: Path):
    h = await make_harness(
        [
            call("write", {"file_path": "a.txt", "content": "1"}),
            say("one"),
            call("write", {"file_path": "b.txt", "content": "2"}),
            say("two"),
        ]
    )
    await h.turn("make a")
    await h.turn("make b")
    h.agent.undo_last_turn()
    assert (work / "a.txt").exists() and not (work / "b.txt").exists()
    h.agent.undo_last_turn()
    assert not (work / "a.txt").exists()


async def test_files_changed_by_bash_are_not_tracked(make_harness, work: Path):
    h = await make_harness(
        [call("bash", {"description": "touch", "command": "echo hi > viashell.txt"}), say("ok")]
    )
    await h.turn("run it")
    assert (work / "viashell.txt").exists()
    assert h.agent.undo_last_turn().startswith("Nothing to undo")
    assert (work / "viashell.txt").exists()


async def test_tool_done_carries_a_diff_for_edits_and_new_files(make_harness, work: Path):
    (work / "a.py").write_text("one\ntwo\nthree\n")
    h = await make_harness(
        [
            call("read", {"file_path": "a.py"}),
            call("edit", {"file_path": "a.py", "old_string": "two", "new_string": "TWO"}),
            call("write", {"file_path": "b.py", "content": "x\ny\n"}),
            say("done"),
        ]
    )
    await h.turn("go")
    done = [e for e in h.of(ToolDone) if e.diff]
    edit, write = done
    assert (edit.diff["added"], edit.diff["removed"]) == (1, 1) and "-two" in edit.diff["text"]
    assert write.diff["new_file"] is True and write.diff["added"] == 2
    model_text = [m.text for m in h.session.messages() if m.role == "tool"]
    assert not any("@@" in t for t in model_text), "diffs are for the UI, not for the model"


async def test_failed_edits_leave_no_checkpoint_events(make_harness, work: Path):
    (work / "a.txt").write_text("keep")
    h = await make_harness(
        [
            call("read", {"file_path": "a.txt"}),
            call("edit", {"file_path": "a.txt", "old_string": "zzz", "new_string": "y"}),
            say("failed"),
        ]
    )
    await h.turn("go")
    assert not [e for e in h.session.events if e.type == "file_change"]


# ----------------------------------------------------------------------------- approval + shell


async def test_approval_requests_carry_a_diff_preview(make_harness, work: Path):
    (work / ".kalash").mkdir()
    (work / ".kalash" / "settings.local.json").write_text('{"a": 1}\n')
    requests = []

    async def approve(request):
        requests.append(request)
        return "deny"

    h = await make_harness(
        [
            call("read", {"file_path": ".kalash/settings.local.json"}),
            call(
                "edit",
                {
                    "file_path": ".kalash/settings.local.json",
                    "old_string": '"a": 1',
                    "new_string": '"a": 2',
                },
            ),
            say("declined"),
        ],
        approver=approve,
    )
    await h.turn("edit config")
    assert len(requests) == 1
    assert '-{"a": 1}'.replace("{", "").replace("}", "") in requests[0].detail.replace(
        "{", ""
    ).replace("}", "")
    assert "settings.local.json" in requests[0].detail


async def test_user_shell_command_is_sandboxed_logged_and_visible_to_the_model(
    make_harness, work: Path
):
    h = await make_harness([say("saw it")])
    outcome = await h.agent.run_shell("echo from-the-user")
    assert "from-the-user" in outcome.text and not outcome.is_error
    texts = [m.text for m in h.session.messages()]
    assert texts[0].startswith("[The user ran a shell command]") and "from-the-user" in texts[0]
    assert any(isinstance(e, ToolDone) and e.name == "bash" for e in h.events)
    await h.turn("what did I run?")
    assert "from-the-user" in h.adapter.requests[0].messages[0].text


async def test_user_shell_respects_the_current_sandbox_mode(make_harness, work: Path):
    h = await make_harness([], permissions={"sandbox": "read-only"})
    outcome = await h.agent.run_shell("echo x > f.txt")
    assert outcome.is_error and not (work / "f.txt").exists()
