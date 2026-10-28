"""Regression from Groq's live 429: a billing link does not mean exhausted quota."""

import json
from pathlib import Path

import httpx
import pytest

from kalash.llm.errors import ErrorCode, from_http

RATE_MESSAGE = (
    "Rate limit reached for model `openai/gpt-oss-20b` on tokens per minute (TPM): "
    "Limit 8000, Used 5351, Requested 3051. Please try again in 3.015s. "
    "Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing"
)


@pytest.mark.parametrize("code", ["rate_limit_exceeded", "rate_limit_error"])
def test_structured_rate_limit_wins_over_promotional_billing_text(code):
    body = json.dumps({"error": {"message": RATE_MESSAGE, "type": "tokens", "code": code}}).encode()
    error = from_http(429, body, httpx.Headers({"retry-after": "3.015"}), "groq")
    assert error.code == ErrorCode.RATE_LIMIT and error.retryable
    assert error.retry_after == 3.015


def test_unstructured_billing_link_does_not_turn_a_rate_limit_into_quota():
    error = from_http(429, RATE_MESSAGE.encode(), {}, "groq")
    assert error.code == ErrorCode.RATE_LIMIT and error.retryable


@pytest.mark.parametrize("code", ["insufficient_quota", "billing_hard_limit_reached"])
def test_actual_quota_codes_remain_non_retryable(code):
    body = json.dumps({"error": {"message": "Contact the provider", "code": code}}).encode()
    error = from_http(429, body, {}, "provider")
    assert error.code == ErrorCode.QUOTA and not error.retryable


async def test_rate_limit_after_write_retries_the_model_without_repeating_the_write(work: Path):
    from kalash.agent.build import create_runtime
    from kalash.config import load_settings
    from tests.harness.mock_server import text_response, tool_call_response
    from tests.harness.wire import Recorder, openai_adapter, respond

    recorder = Recorder(
        respond(
            tool_call_response(
                "write-once", "write", {"file_path": "out.txt", "content": "exact\n"}
            )
        ),
        httpx.Response(
            429,
            json={
                "error": {"message": RATE_MESSAGE, "type": "tokens", "code": "rate_limit_exceeded"}
            },
            headers={"retry-after": "0"},
        ),
        respond(tool_call_response("read-back", "read", {"file_path": "out.txt"})),
        respond(text_response("verified")),
    )
    runtime = await create_runtime(
        cwd=work,
        settings=load_settings(work)[0],
        adapter=openai_adapter(recorder),
        with_mcp=False,
    )
    try:
        result = await runtime.agent.run_turn("write out.txt, read it back and verify")
        assert result.reason == "completed" and result.text == "verified"
        assert (work / "out.txt").read_bytes() == b"exact\n"
        changes = [e for e in runtime.session.events if e.type == "file_change"]
        assert len(changes) == 1 and changes[0].data["call_id"] == "write-once"
        assert len([e for e in runtime.session.events if e.type == "tool_start"]) == 2
        assert recorder.body(1) == recorder.body(2), "only the current model request is retried"
        assert len(recorder.requests) == 4 and not recorder.responses
    finally:
        await runtime.aclose()
