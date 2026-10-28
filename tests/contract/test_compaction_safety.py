"""Unusable summaries keep their billed usage and leave the conversation intact."""

import pytest

from kalash.llm.errors import ErrorCode
from kalash.llm.types import Usage
from kalash.session.log import SessionLog
from tests.contract.test_compaction import BIG, WINDOW, big_reply
from tests.harness.scripted import Reply, fail, say


@pytest.mark.parametrize(
    "summary",
    [
        Reply(calls=[("read", {"file_path": "x"})], usage=Usage(600, 80)),
        say("too big " * 10_000, usage=Usage(600, 80)),
    ],
    ids=["no-text", "too-large"],
)
async def test_rejected_compaction_still_records_usage_and_cost(make_harness, summary):
    h = await make_harness(
        [big_reply(), summary],
        window=WINDOW,
        pricing={"scripted/model": {"input_per_mtok": 1, "output_per_mtok": 1}},
    )
    await h.turn(BIG)
    result = await h.turn("continue")

    assert result.reason == "error"
    assert result.usage.total == 680
    assert result.cost == pytest.approx(0.00068)
    assert h.session.usage_total().total == 9100 + 680
    assert h.session.cost_total() == pytest.approx(0.00978)
    assert not any(e.type == "compaction" for e in h.session.events)
    assert BIG in [m.text for m in h.session.messages()]
    path = h.session.path
    await h.runtime.aclose()
    reopened = SessionLog.open(path)
    try:
        assert reopened.usage_total().total == 9780
        assert reopened.cost_total() == pytest.approx(0.00978)
    finally:
        reopened.close()


@pytest.mark.parametrize("recovery", ["pressure", "overflow"])
@pytest.mark.parametrize("budget", ["tokens", "cost"])
async def test_compaction_continues_despite_retired_budget_settings(make_harness, recovery, budget):
    first = big_reply() if recovery == "pressure" else big_reply(input_tokens=100)
    script = [first]
    if recovery == "overflow":
        script.append(fail(ErrorCode.CONTEXT_OVERFLOW))
    script.extend([say("checkpoint", usage=Usage(500, 100)), say("continued")])
    # Enough for the first response, but not for the completed compaction as well.
    total_limit = first.usage.total + 500
    limits = (
        {"max_session_tokens": total_limit}
        if budget == "tokens"
        else {"max_session_cost_usd": total_limit / 1_000_000}
    )
    h = await make_harness(
        script,
        window=WINDOW,
        limits=limits,
        pricing={"scripted/model": {"input_per_mtok": 1, "output_per_mtok": 1}},
    )
    await h.turn(BIG)
    result = await h.turn("continue")

    assert result.reason == "completed" and result.text == "continued"
    assert result.error_code is None
    assert len(h.adapter.requests) == (3 if recovery == "pressure" else 4)
    assert result.usage.total > 600
    assert h.session.usage_total().total > total_limit + 100


async def test_manual_compaction_ignores_retired_session_limits(make_harness):
    h = await make_harness(
        [big_reply(), say("checkpoint")],
        window=WINDOW,
        limits={"max_session_tokens": 9000},
    )
    await h.turn(BIG)
    await h.agent.compact_now()
    assert len(h.adapter.requests) == 2
    assert any(e.type == "compaction" for e in h.session.events)


@pytest.mark.parametrize("finish", ["max_tokens", "content_filter", "refusal", "tool_calls"])
async def test_incomplete_summary_never_replaces_history(make_harness, finish):
    reply = say("unfinished checkpoint", finish=finish, usage=Usage(600, 80))
    if finish == "tool_calls":
        reply.calls = [("write", {"file_path": "unexpected.txt", "content": "no"})]
    replies = [reply] * 12 if finish == "max_tokens" else [reply]
    h = await make_harness([big_reply(), *replies], window=WINDOW)
    await h.turn(BIG)
    result = await h.turn("continue")

    assert result.reason == "error" and "summaris" in result.error
    assert not any(e.type == "compaction" for e in h.session.events)
    assert BIG in [m.text for m in h.session.messages()]
    assert len(h.adapter.requests) >= 2
    assert result.usage.total == 680 * (len(h.adapter.requests) - 1)
