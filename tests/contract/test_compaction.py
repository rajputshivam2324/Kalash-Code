"""Compaction: pressure trigger, provider-overflow recovery, pair integrity, failure reporting."""

from __future__ import annotations

from kalash.agent.context import NothingToCompact, choose_cut, message_tokens
from kalash.agent.events import Compacted, Notice
from kalash.llm.errors import ErrorCode
from kalash.llm.types import Message, TextBlock, ToolCallBlock, Usage, tool_result, user_text
from tests.harness.scripted import Reply, fail, say

BIG = "lorem ipsum dolor " * 600  # ~10.8k chars, roughly 3k tokens
WINDOW = 10_000


def big_reply(input_tokens: int = 9000) -> Reply:
    return say(BIG, usage=Usage(input_tokens=input_tokens, output_tokens=100))


async def test_pressure_triggers_a_summary_call_and_history_is_replaced(make_harness):
    h = await make_harness(
        [
            big_reply(),
            say("## Primary Request\n- SUMMARY-MARKER", usage=Usage(500, 100)),
            say("final answer"),
        ],
        window=WINDOW,
    )
    await h.turn(BIG)
    result = await h.turn("next question")
    assert result.reason == "completed" and result.text == "final answer"
    h.adapter.assert_exhausted()

    summary_request, real_request = h.adapter.requests[1], h.adapter.requests[2]
    assert summary_request.messages[-1].text.startswith("You are now acting as a compaction")
    assert not summary_request.tools and summary_request.system == real_request.system
    assert [m.role for m in real_request.messages] == ["user", "user"]
    assert "SUMMARY-MARKER" in real_request.messages[0].text
    assert real_request.messages[1].text == "next question"
    assert len(h.of(Compacted)) == 1 and h.of(Compacted)[0].tokens_after < WINDOW * 0.8
    compaction = [e for e in h.session.events if e.type == "compaction"]
    assert len(compaction) == 1 and compaction[0].data["replaced"] == 2
    # the full original conversation is still in the log
    assert sum(1 for e in h.session.events if e.type in ("user", "assistant")) == 4


async def test_compaction_usage_is_accounted_in_turn_and_session_totals(make_harness):
    h = await make_harness(
        [big_reply(), say("SUMMARY", usage=Usage(500, 100)), say("ok")], window=WINDOW
    )
    await h.turn(BIG)
    result = await h.turn("more")
    final_input = 100 + len(h.adapter.requests[2].messages)  # ScriptedAdapter default usage
    assert (result.usage.input_tokens, result.usage.output_tokens) == (500 + final_input, 100 + 20)
    total = h.session.usage_total()
    assert (total.input_tokens, total.output_tokens) == (9000 + 500 + final_input, 100 + 100 + 20)


async def test_provider_reported_overflow_compacts_and_retries_once(make_harness):
    h = await make_harness(
        [
            say(BIG, usage=Usage(input_tokens=100, output_tokens=10)),
            fail(ErrorCode.CONTEXT_OVERFLOW, "maximum context length exceeded"),
            say("SUMMARY-AFTER-OVERFLOW"),
            say("recovered"),
        ],
        window=WINDOW,
    )
    await h.turn(BIG)
    result = await h.turn("again")
    assert result.reason == "completed" and result.text == "recovered"
    assert any("overflow" in n.text for n in h.of(Notice))
    assert "SUMMARY-AFTER-OVERFLOW" in h.adapter.requests[-1].messages[0].text


async def test_second_overflow_after_compaction_is_reported_not_looped(make_harness):
    h = await make_harness(
        [
            say(BIG, usage=Usage(input_tokens=100, output_tokens=10)),
            fail(ErrorCode.CONTEXT_OVERFLOW),
            say("SUMMARY"),
            fail(ErrorCode.CONTEXT_OVERFLOW),
        ],
        window=WINDOW,
    )
    await h.turn(BIG)
    result = await h.turn("again")
    assert result.reason == "error" and "CONTEXT_OVERFLOW" in (result.error or "")


async def test_compaction_that_cannot_reduce_the_context_fails_clearly(make_harness):
    h = await make_harness(
        [say("tiny", usage=Usage(input_tokens=9500, output_tokens=10))], window=WINDOW
    )
    await h.turn("hi")
    result = await h.turn("again")
    assert result.reason == "error" and "cannot compact the context" in (result.error or "")
    assert h.session.events[-1].type == "turn_end"


async def test_summariser_returning_nothing_is_an_error_not_an_empty_checkpoint(make_harness):
    h = await make_harness(
        [big_reply(), Reply(calls=[("read", {"file_path": "x"})])], window=WINDOW
    )
    await h.turn(BIG)
    result = await h.turn("again")
    assert result.reason == "error" and "no text" in (result.error or "")
    assert not [e for e in h.session.events if e.type == "compaction"]


async def test_resumed_session_sees_the_checkpoint_not_the_old_messages(make_harness):
    from kalash.session.log import SessionLog

    h = await make_harness(
        [big_reply(), say("SUMMARY-X", usage=Usage(500, 50)), say("answer")], window=WINDOW
    )
    await h.turn(BIG)
    await h.turn("second")
    path = h.session.path
    await h.runtime.aclose()
    h2 = await make_harness([say("after resume")], window=WINDOW, session=SessionLog.open(path))
    await h2.turn("third")
    texts = [m.text for m in h2.adapter.requests[0].messages]
    assert "SUMMARY-X" in texts[0] and texts[-1] == "third" and BIG not in "".join(texts)


# ----------------------------------------------------------------------------- unit: cut choice


def exchange(size: int) -> list[Message]:
    call = ToolCallBlock("c", "read", "{}")
    return [Message("assistant", [call]), tool_result(call, "x" * size)]


def test_cut_never_separates_a_call_from_its_results():
    items = [
        (0, user_text("q")),
        *[(i + 1, m) for i, m in enumerate(exchange(40_000))],
        (3, user_text("next")),
    ]
    for retain in (10, 500, 5_000, 20_000):
        cut = choose_cut(items, retain)
        assert cut == len(items) or items[cut][1].role != "tool"


def test_cut_keeps_the_newest_messages_that_fit():
    items = [(i, Message("user", [TextBlock("w" * 350)])) for i in range(10)]
    per = message_tokens(items[0][1])
    cut = choose_cut(items, per * 3)
    assert len(items) - cut == 3


def test_cut_with_everything_fitting_keeps_everything():
    items = [(i, user_text("short")) for i in range(4)]
    assert choose_cut(items, 10_000) == 0


async def test_compacting_a_short_conversation_is_not_a_failure(make_harness):
    import pytest

    h = await make_harness([say("tiny")], window=WINDOW)
    await h.turn("hi")
    with pytest.raises(NothingToCompact, match="nothing to compact yet"):
        await h.agent.compact_now()
    assert not [e for e in h.session.events if e.type == "compaction"]
    assert len(h.adapter.requests) == 1, "no summary call was spent on it"
