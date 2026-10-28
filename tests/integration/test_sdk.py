"""The public Python API."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kalash.agent.events import TextChunk, ToolDone, TurnEnd
from kalash.sdk import Kalash
from tests.harness.scripted import Reply, ScriptedAdapter, call, say


def sdk(work: Path, script: list[Reply], **kw) -> tuple[Kalash, ScriptedAdapter]:
    adapter = ScriptedAdapter(script)
    return Kalash(work, adapter=adapter, settings={"model": "scripted/model"}, **kw), adapter


async def test_run_returns_the_turn_result_and_exposes_session_state(work: Path):
    agent, adapter = sdk(work, [call("write", {"file_path": "a.txt", "content": "x"}), say("ok")])
    async with agent:
        result = await agent.run("make a.txt")
        assert result.reason == "completed" and result.text == "ok" and result.tool_calls == 1
        assert (work / "a.txt").read_text() == "x"
        assert agent.model == "scripted/model" and agent.usage.total > 0
        assert agent.session_id and agent.cost_usd is None
    adapter.assert_exhausted()


async def test_stream_yields_events_in_order_and_ends_with_turn_end(work: Path):
    (work / "f.txt").write_text("hello")
    agent, _ = sdk(work, [call("read", {"file_path": "f.txt"}), say("it says hello")])
    async with agent:
        events = [event async for event in agent.stream("read f.txt")]
    kinds = [type(e).__name__ for e in events]
    assert kinds[-1] == "TurnEnd" and "ToolDone" in kinds and "TextChunk" in kinds
    assert kinds.index("ToolDone") < kinds.index("TextChunk")
    assert "".join(e.text for e in events if isinstance(e, TextChunk)) == "it says hello"
    assert isinstance(events[-1], TurnEnd) and events[-1].result.reason == "completed"


async def test_breaking_out_of_a_stream_cancels_the_turn_and_keeps_the_session_valid(work: Path):
    agent, _ = sdk(work, [Reply(text="starting", hang=True), say("next turn works")])
    async with agent:
        async for event in agent.stream("long task"):
            if isinstance(event, TextChunk):
                break
        await asyncio.sleep(0.1)
        runtime = agent._runtime
        assert runtime.session.events[-1].type == "turn_end"
        assert runtime.session.events[-1].data["reason"] == "interrupted"
        assert (await agent.run("again")).text == "next turn works"


async def test_a_second_concurrent_turn_is_refused(work: Path):
    agent, _ = sdk(work, [Reply(text="busy", hang=True)])
    async with agent:
        first = asyncio.create_task(agent.run("one"))
        await asyncio.sleep(0.2)
        with pytest.raises(RuntimeError, match="already running"):
            await agent.run("two")
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first


async def test_usage_before_start_is_a_clear_error(work: Path):
    agent, _ = sdk(work, [])
    with pytest.raises(RuntimeError, match="not started"):
        _ = agent.usage
    with pytest.raises(RuntimeError, match="start"):
        await agent.run("x")


async def test_settings_override_and_approver_callback(work: Path):
    (work / ".kalash").mkdir()
    asked: list[str] = []

    async def approver(request):
        asked.append(request.summary)
        return "once"

    agent, _ = sdk(
        work,
        [call("write", {"file_path": ".kalash/x.txt", "content": "1"}), say("done")],
        approver=approver,
    )
    async with agent:
        result = await agent.run("write config")
    assert asked == ["write .kalash/x.txt"] and not result.denials
    assert (work / ".kalash" / "x.txt").exists()


async def test_without_an_approver_protected_actions_are_denied_and_reported(work: Path):
    (work / ".kalash").mkdir()
    agent, _ = sdk(
        work, [call("write", {"file_path": ".kalash/x.txt", "content": "1"}), say("could not")]
    )
    async with agent:
        events = [e async for e in agent.stream("write config")]
    done = [e for e in events if isinstance(e, ToolDone)][0]
    assert done.denied and not (work / ".kalash" / "x.txt").exists()


async def test_resume_continues_the_same_session(work: Path):
    first, _ = sdk(work, [say("one")])
    async with first:
        await first.run("remember this")
        sid = first.session_id
    second, adapter = sdk(work, [say("two")], resume=sid)
    async with second:
        await second.run("what did I say?")
        assert second.session_id == sid
    assert [m.role for m in adapter.requests[0].messages] == ["user", "assistant", "user"]


async def test_on_event_callback_sees_every_event(work: Path):
    seen: list[str] = []
    agent, _ = sdk(work, [say("hi")], on_event=lambda e: seen.append(type(e).__name__))
    async with agent:
        await agent.run("hello")
    assert seen[0] == "TurnStart" and seen[-1] == "TurnEnd"
