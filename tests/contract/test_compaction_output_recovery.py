"""A completed reasoning-only truncation is recoverable without losing history or usage."""

from kalash.llm.types import Usage, user_text
from tests.contract.test_compaction import BIG, WINDOW, big_reply
from tests.harness.scripted import Reply, say


async def test_reasoning_only_summary_gets_more_output_room_and_repartitions(make_harness):
    def inspect(request):
        if "compaction engine" in request.messages[-1].text:
            assert request.max_tokens > WINDOW // 8

    h = await make_harness(
        [
            big_reply(),
            Reply(reasoning="planning a summary", finish="max_tokens", usage=Usage(400, 1250)),
            *[
                say("complete checkpoint", usage=Usage(500, 100), on_start=inspect)
                for _ in range(10)
            ],
        ],
        window=WINDOW,
    )
    await h.turn(BIG)
    result = await h.turn("continue")

    assert result.reason == "completed" and result.text == "complete checkpoint"
    requests = [e for e in h.session.events if e.type == "compaction_request"]
    assert len(requests) >= 2 and requests[1].data["max_tokens"] > requests[0].data["max_tokens"]
    assert result.usage.output_tokens == 1250 + 100 * len(requests)
    assert "complete checkpoint" in h.session.messages()[0].text
    assert "planning a summary" not in "".join(m.text for m in h.session.messages())


async def test_grown_summary_output_resizes_when_the_rolling_prefix_takes_space(make_harness):
    h = await make_harness(
        [
            *[Reply(reasoning="thinking", finish="max_tokens") for _ in range(3)],
            *[say("rolling checkpoint " * 40, usage=Usage(1000, 200)) for _ in range(20)],
        ],
        window=8192,
    )
    for _ in range(6):
        h.session.append_message(user_text("dense history " * 700))

    result = await h.turn("continue")

    assert result.reason == "completed", result.error
    summaries = [e for e in h.session.events if e.type == "compaction_request"]
    # The fourth response completed after output grew. Its rolling prefix takes space
    # in the fifth request, which must leave room for both history and that checkpoint.
    assert len(summaries) >= 5
    assert summaries[4].data["max_tokens"] < summaries[3].data["max_tokens"]
    assert any(e.type == "compaction" for e in h.session.events)


async def test_proactive_pressure_does_not_abort_when_only_a_checkpoint_is_old(make_harness, work):
    from tests.harness.scripted import call

    (work / "note.txt").write_text("small new evidence")
    h = await make_harness(
        [
            call("read", {"file_path": "note.txt"}, usage=Usage(6600, 50)),
            say("finished"),
        ],
        window=8192,
    )
    h.session.append("compaction", summary="checkpoint fact " * 450, keep_from=1000)

    result = await h.turn("continue")

    assert result.reason == "completed", result.error
    assert result.text == "finished"
    h.adapter.assert_exhausted()


async def test_reasoning_truncation_can_disable_summary_thinking_without_changing_user_effort(
    make_harness,
):
    from kalash.llm.catalog import ModelInfo

    def inspect(request):
        if "compaction engine" in request.messages[-1].text:
            assert request.reasoning_effort == "off"

    h = await make_harness(
        [
            big_reply(),
            Reply(reasoning="thinking", finish="max_tokens"),
            *[say("checkpoint", on_start=inspect) for _ in range(12)],
        ],
        window=WINDOW,
    )
    h.agent.info = ModelInfo(
        "model",
        WINDOW,
        5000,
        thinking="effort",
        efforts=("off", "low", "high"),
        default_effort="low",
    )
    await h.turn(BIG)

    result = await h.turn("continue")

    assert result.reason == "completed", result.error
    assert h.adapter.requests[-1].reasoning_effort == "low"
    assert h.agent.effort_setting is None
