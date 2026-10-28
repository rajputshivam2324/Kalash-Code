"""Live Sarvam returned billed empty completions before retrying successfully."""

from pathlib import Path

from kalash.agent.build import create_runtime
from kalash.config import load_settings
from tests.harness.mock_server import text_response
from tests.harness.wire import Recorder, openai_adapter, respond, sse_bytes


async def test_empty_response_retry_keeps_the_failed_attempts_reported_usage(work: Path):
    empty = sse_bytes(
        [
            {
                "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 4307,
                    "completion_tokens": 104,
                    "prompt_tokens_details": {"cached_tokens": 1000},
                },
            },
        ]
    )
    recorder = Recorder(
        respond(empty), respond(text_response("recovered", prompt_tokens=10, completion_tokens=5))
    )
    runtime = await create_runtime(
        cwd=work, settings=load_settings(work)[0], adapter=openai_adapter(recorder), with_mcp=False
    )
    try:
        result = await runtime.agent.run_turn("continue")
        assert result.reason == "completed" and result.text == "recovered"
        assert result.usage.input_tokens == 4307 + 10
        assert result.usage.output_tokens == 104 + 5
        assert runtime.session.usage_total() == result.usage
        assert result.usage.cache_read_tokens == 1000
        usage = [e for e in runtime.session.events if e.type == "usage"]
        assert len(usage) == 1 and usage[0].data["usage"]["input_tokens"] == 4307
        assert recorder.body(0) == recorder.body(1)
    finally:
        await runtime.aclose()


async def test_failed_compaction_attempt_is_billed_before_retrying(make_harness):
    from kalash.llm.errors import ErrorCode, LLMError
    from kalash.llm.types import Usage
    from tests.contract.test_compaction import BIG, WINDOW, big_reply
    from tests.harness.scripted import Reply, say

    h = await make_harness(
        [
            big_reply(),
            Reply(error=LLMError("empty", ErrorCode.EMPTY_RESPONSE, usage=Usage(4307, 104))),
            say("checkpoint", usage=Usage(500, 100)),
            say("recovered"),
        ],
        window=WINDOW,
    )
    await h.turn(BIG)
    result = await h.turn("continue")

    assert result.reason == "completed" and result.text == "recovered"
    assert result.usage.input_tokens >= 4307 + 500
    assert result.usage.output_tokens == 104 + 100 + 20
    assert h.session.usage_total().total == 9100 + result.usage.total
    failed = [e for e in h.session.events if e.data.get("purpose") == "failed compaction attempt"]
    assert len(failed) == 1 and failed[0].data["usage"]["input_tokens"] == 4307
    h.adapter.assert_exhausted()
