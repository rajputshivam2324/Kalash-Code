"""Loop contracts: the real agent, tools, permissions and session log over a scripted model."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kalash.agent.events import ToolDone, ToolRunning
from kalash.llm.errors import ErrorCode
from kalash.llm.types import Message, Usage
from kalash.session.log import SessionLog
from tests.harness.scripted import Reply, call, calls, fail, say


def results_by_call(messages: list[Message]) -> dict[str, Message]:
    return {m.tool_call_id: m for m in messages if m.role == "tool" and m.tool_call_id}


def assert_paired(messages: list[Message]) -> None:
    """Every tool call has exactly one result, and no result lacks a call."""
    called = [c.id for m in messages if m.role == "assistant" for c in m.tool_calls]
    answered = [m.tool_call_id for m in messages if m.role == "tool"]
    assert sorted(called) == sorted(i for i in answered if i), (called, answered)
    assert len(set(called)) == len(called)


async def test_plain_answer_completes(make_harness):
    h = await make_harness([say("All done.")])
    result = await h.turn("hello")
    assert result.reason == "completed"
    assert result.text == "All done."
    assert [m.role for m in h.session.messages()] == ["user", "assistant"]
    h.adapter.assert_exhausted()


async def test_write_then_read_back_through_real_tools(make_harness, work: Path):
    h = await make_harness(
        [
            call("write", {"file_path": "hello.txt", "content": "hi\n"}),
            call("read", {"file_path": "hello.txt"}),
            say("Created and verified hello.txt."),
        ]
    )
    result = await h.turn("make hello.txt")
    assert result.reason == "completed"
    assert (work / "hello.txt").read_text() == "hi\n"
    read_result = [m for m in h.session.messages() if m.role == "tool"][-1]
    assert "1: hi" in read_result.text
    assert_paired(h.session.messages())
    assert result.tool_calls == 2


async def test_sequential_edits_need_no_reread(make_harness, work: Path):
    (work / "a.py").write_text("x = 1\ny = 2\n")
    h = await make_harness(
        [
            call("read", {"file_path": "a.py"}),
            call("edit", {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 10"}),
            call("edit", {"file_path": "a.py", "old_string": "y = 2", "new_string": "y = 20"}),
            say("ok"),
        ]
    )
    result = await h.turn("edit twice")
    assert result.reason == "completed"
    assert (work / "a.py").read_text() == "x = 10\ny = 20\n"
    assert not any(m.is_error for m in h.session.messages() if m.role == "tool")


async def test_edit_unread_file_is_refused_and_file_untouched(make_harness, work: Path):
    (work / "a.py").write_text("x = 1\n")
    h = await make_harness(
        [
            call("edit", {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}),
            say("could not edit"),
        ]
    )
    await h.turn("edit it")
    assert (work / "a.py").read_text() == "x = 1\n"
    refusal = [m for m in h.session.messages() if m.role == "tool"][0]
    assert refusal.is_error and "Read it first" in refusal.text


async def test_external_change_after_read_blocks_overwrite(make_harness, work: Path):
    (work / "a.txt").write_text("one\n")

    def clobber(_request) -> None:
        (work / "a.txt").write_text("changed elsewhere\n")

    h = await make_harness(
        [
            call("read", {"file_path": "a.txt"}),
            Reply(calls=[("write", {"file_path": "a.txt", "content": "mine\n"})], on_start=clobber),
            say("stopped"),
        ]
    )
    await h.turn("overwrite")
    assert (work / "a.txt").read_text() == "changed elsewhere\n"
    last = [m for m in h.session.messages() if m.role == "tool"][-1]
    assert last.is_error and "changed since you last read" in last.text


async def test_parallel_calls_return_results_in_call_order(make_harness, work: Path):
    for name in "abc":
        (work / f"{name}.txt").write_text(name)
    h = await make_harness(
        [
            calls(*[("read", {"file_path": f"{n}.txt"}) for n in "abc"]),
            say("read all"),
        ]
    )
    await h.turn("read three")
    messages = h.session.messages()
    assistant = [m for m in messages if m.role == "assistant"][0]
    order = [m.tool_call_id for m in messages if m.role == "tool"]
    assert order == [c.id for c in assistant.tool_calls]
    assert_paired(messages)


async def test_unknown_tool_and_bad_arguments_get_actionable_results(make_harness):
    h = await make_harness(
        [
            calls(
                ("nonexistent", {}), ("read", {"file_path": "x", "bogus": 1}), ("read", "{not json")
            ),
            say("gave up"),
        ]
    )
    await h.turn("go")
    texts = [m.text for m in h.session.messages() if m.role == "tool"]
    assert "Unknown tool 'nonexistent'" in texts[0] and "read" in texts[0]
    assert "unknown parameter 'bogus'" in texts[1] and "Valid parameters" in texts[1]
    assert "not valid JSON" in texts[2]
    assert_paired(h.session.messages())


async def test_truncated_tool_call_is_not_executed(make_harness, work: Path):
    h = await make_harness(
        [
            Reply(
                calls=[("write", {"file_path": "big.txt", "content": "partial"})],
                finish="max_tokens",
            ),
            say("retrying smaller"),
        ]
    )
    result = await h.turn("write big file")
    assert result.reason == "completed"
    assert not (work / "big.txt").exists()
    first = [m for m in h.session.messages() if m.role == "tool"][0]
    assert first.is_error and "NOT run" in first.text


async def test_text_only_max_tokens_is_reported_not_completed(make_harness):
    h = await make_harness([Reply(text="cut off mid-sen", finish="max_tokens")])
    result = await h.turn("write an essay")
    assert result.reason == "max_tokens"
    assert "output limit" in (result.error or "")


async def test_denied_write_in_read_only_mode_reports_denial(make_harness, work: Path):
    h = await make_harness(
        [call("write", {"file_path": "x.txt", "content": "no"}), say("Written!")],
        permissions={"sandbox": "read-only"},
    )
    result = await h.turn("write x")
    assert not (work / "x.txt").exists()
    assert result.reason == "completed"
    assert len(result.denials) == 1 and "read-only" in result.denials[0]
    done = h.of(ToolDone)[0]
    assert done.denied and done.is_error


async def test_write_outside_workspace_is_denied(make_harness, tmp_path: Path):
    target = tmp_path / "elsewhere.txt"
    h = await make_harness(
        [
            call("write", {"file_path": str(target), "content": "x"}),
            say("done"),
        ]
    )
    result = await h.turn("write outside")
    assert not target.exists()
    assert "outside the workspace" in result.denials[0]


async def test_ask_without_approver_denies_with_explanation(make_harness, work: Path):
    h = await make_harness(
        [call("write", {"file_path": ".git/hooks/pre-commit", "content": "evil"}), say("x")],
    )
    (work / ".git" / "hooks").mkdir(parents=True)
    result = await h.turn("install hook")
    assert not (work / ".git" / "hooks" / "pre-commit").exists()
    assert "no one is available" in result.denials[0]


async def test_approver_can_allow_protected_write(make_harness, work: Path):
    (work / ".kalash").mkdir()
    asked = []

    async def approve(request):
        asked.append(request)
        return "once"

    h = await make_harness(
        [call("write", {"file_path": ".kalash/note.txt", "content": "ok"}), say("done")],
        approver=approve,
    )
    result = await h.turn("write config")
    assert (work / ".kalash" / "note.txt").read_text() == "ok"
    assert len(asked) == 1 and not result.denials


async def test_approval_never_denies_asks_without_prompting(make_harness, work: Path):
    asked = []

    async def approve(request):
        asked.append(request)
        return "once"

    (work / ".kalash").mkdir()
    h = await make_harness(
        [call("write", {"file_path": ".kalash/x", "content": "1"}), say("done")],
        approver=approve,
        permissions={"approval": "never"},
    )
    result = await h.turn("go")
    assert asked == [] and "approval prompts are disabled" in result.denials[0]


async def test_deny_rule_blocks_before_anything_else(make_harness, work: Path):
    h = await make_harness(
        [call("write", {"file_path": "secrets/key.txt", "content": "k"}), say("x")],
        permissions={"deny": ["write secrets/*"]},
    )
    result = await h.turn("write secret")
    assert not (work / "secrets").exists()
    assert "deny rule" in result.denials[0]


async def test_credentials_cannot_be_read(make_harness):
    from kalash.paths import kalash_home

    auth = kalash_home() / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text("{}")
    h = await make_harness([call("read", {"file_path": str(auth)}), say("x")])
    result = await h.turn("read creds")
    assert "credentials" in result.denials[0]


async def test_retry_before_output_is_transparent(make_harness):
    h = await make_harness([fail(ErrorCode.RATE_LIMIT, retry_after=0.0), say("recovered")])
    result = await h.turn("hi")
    assert result.reason == "completed" and result.text == "recovered"
    assert len(h.adapter.requests) == 2


async def test_non_retryable_error_ends_turn_with_error_and_logs_attempt(make_harness):
    h = await make_harness([fail(ErrorCode.AUTH, "bad key")])
    result = await h.turn("hi")
    assert result.reason == "error" and "bad key" in (result.error or "")
    assert len(h.adapter.requests) == 1
    assert any(e.type == "attempt_failed" for e in h.session.events)
    assert h.session.events[-1].type == "turn_end"


async def test_mid_stream_drop_is_retried_and_partial_output_not_committed(make_harness):
    h = await make_harness(
        [
            Reply(text="partial answer that gets cut", fail_after_events=1),
            say("clean answer"),
        ]
    )
    result = await h.turn("hi")
    assert result.reason == "completed" and result.text == "clean answer"
    assert [m.text for m in h.session.messages() if m.role == "assistant"] == ["clean answer"]


async def test_cancellation_closes_running_tool_with_unknown_outcome(make_harness, work: Path):
    h = await make_harness(
        [call("bash", {"description": "wait", "command": "sleep 30"}), say("never")]
    )
    task = asyncio.create_task(h.turn("sleep"))
    for _ in range(200):  # ToolRunning is emitted before the process exists: wait for the job
        await asyncio.sleep(0.05)
        if h.of(ToolRunning) and h.runtime.jobs.all():
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    messages = h.session.messages()
    assert_paired(messages)
    closing = [m for m in messages if m.role == "tool"][-1]
    assert closing.is_error and "outcome unknown" in closing.text
    assert h.session.events[-1].type == "turn_end"
    assert h.session.events[-1].data["reason"] == "interrupted"
    assert not h.runtime.jobs.all()[0].running


async def test_next_request_after_cancel_is_valid_history(make_harness):
    h = await make_harness([Reply(text="thinking", hang=True), say("second answer")])
    task = asyncio.create_task(h.turn("first"))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = await h.turn("second")
    assert result.text == "second answer"
    roles = [m.role for m in h.adapter.requests[-1].messages]
    assert roles == ["user", "user"]  # the cancelled turn committed no assistant message


async def test_retired_step_limit_does_not_stop_the_turn(make_harness):
    h = await make_harness(
        [call("read", {"file_path": "nope"}) for _ in range(3)] + [say("finished")],
        limits={"max_steps_per_turn": 3},
    )
    result = await h.turn("loop")
    assert result.reason == "completed" and result.text == "finished"
    assert result.steps == 4


async def test_repeated_identical_failure_triggers_reminder(make_harness):
    h = await make_harness([call("read", {"file_path": "missing"}) for _ in range(3)] + [say("x")])
    await h.turn("go")
    last_result = [m for m in h.session.messages() if m.role == "tool"][-1]
    assert "3rd time" in last_result.text and "not the cause" in last_result.text


async def test_resume_repairs_interrupted_session(make_harness, work: Path):
    h = await make_harness([call("bash", {"description": "x", "command": "echo hi"})])
    # simulate a crash right after the model asked for a tool, before any result
    path = h.session.path
    h.session.append("turn_start", turn=1)
    h.session.append_message(Message("user", []))
    await h.runtime.aclose()
    reopened = SessionLog.open(path)
    fixes = reopened.repair()
    assert any("interrupted turn" in f for f in fixes)
    reopened.close()


async def test_session_log_is_the_only_history_after_reopen(make_harness, work: Path):
    h = await make_harness([say("first"), say("second")])
    await h.turn("one")
    path = h.session.path
    await h.runtime.aclose()
    session = SessionLog.open(path)
    h2 = await make_harness([say("third")], session=session)
    await h2.turn("two")
    roles = [m.role for m in h2.adapter.requests[0].messages]
    assert roles == ["user", "assistant", "user"]


async def test_usage_and_cost_accounting(make_harness):
    h = await make_harness(
        [
            Reply(text="a", usage=Usage(input_tokens=1000, output_tokens=100)),
            Reply(text="b", usage=Usage(input_tokens=2000, output_tokens=50)),
        ],
        pricing={"scripted/model": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}},
    )
    r1 = await h.turn("one")
    r2 = await h.turn("two")
    assert r1.cost == pytest.approx((1000 * 1.0 + 100 * 2.0) / 1e6)
    total = h.session.usage_total()
    assert total.input_tokens == 3000 and total.output_tokens == 150
    assert h.session.cost_total() == pytest.approx(r1.cost + r2.cost)


async def test_unpriced_model_runs_without_cost_cap_warnings(make_harness):
    from kalash.agent.events import Notice

    h = await make_harness([say("still works")], limits={"max_session_cost_usd": 1.0})
    warnings = [n.text for n in h.of(Notice) if n.level == "warning"]
    assert warnings == []
    assert (await h.turn("hi")).reason == "completed"


async def test_cost_is_reported_without_stopping_the_next_turn(make_harness):
    h = await make_harness(
        [Reply(text="a", usage=Usage(input_tokens=1000, output_tokens=0)), say("continued")],
        limits={"max_session_cost_usd": 0.0005},
        pricing={"scripted/model": {"input_per_mtok": 1.0, "output_per_mtok": 1.0}},
    )
    await h.turn("first")
    result = await h.turn("second")
    assert result.reason == "completed" and result.text == "continued"
    assert len(h.adapter.requests) == 2 and h.session.cost_total() > 0.0005


async def test_tool_event_sequence_is_ordered(make_harness, work: Path):
    (work / "f.txt").write_text("x")
    h = await make_harness([call("read", {"file_path": "f.txt"}), say("done")])
    await h.turn("read")
    kinds = [type(e).__name__ for e in h.events]
    assert kinds.index("ToolRunning") < kinds.index("ToolDone") < kinds.index("TurnEnd")
