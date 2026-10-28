"""Retry policy, error parsing, provider resolution, credentials."""

from __future__ import annotations

import json
import stat

import pytest

from kalash.credentials import (
    CredentialError,
    get_credential,
    get_selected_model,
    save_credential,
    set_selected_model,
    stored_providers,
)
from kalash.llm.errors import ErrorCode, LLMError, parse_retry_after
from kalash.llm.providers import (
    ProviderError,
    all_specs,
    api_key_for,
    build_adapter,
    parse_model,
)
from kalash.llm.retry import RetryPolicy, stream_with_retry
from kalash.llm.types import Done, Message, Retrying, TextBlock, Usage
from kalash.paths import kalash_home
from tests.harness.scripted import Reply, ScriptedAdapter, fail, say
from tests.harness.wire import simple_request

# ----------------------------------------------------------------------------- retry


def err(code: ErrorCode, **kw) -> LLMError:
    return LLMError("x", code, **kw)


def test_policy_backoff_doubles_and_caps():
    policy = RetryPolicy(initial_delay=1.0, max_delay=5.0, jitter=0.0)
    delays = [policy.delay(n, err(ErrorCode.SERVER)) for n in range(1, 6)]
    assert delays == [1.0, 2.0, 4.0, 5.0, 5.0]
    assert policy.delay(6, err(ErrorCode.SERVER)) is None  # max_retries=5


def test_policy_honours_retry_after_but_gives_up_when_it_is_too_long():
    policy = RetryPolicy(max_retry_after=30)
    assert policy.delay(1, err(ErrorCode.RATE_LIMIT, retry_after=7)) == 7
    assert policy.delay(1, err(ErrorCode.RATE_LIMIT, retry_after=3600)) is None


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.AUTH,
        ErrorCode.QUOTA,
        ErrorCode.INVALID_REQUEST,
        ErrorCode.CONTEXT_OVERFLOW,
        ErrorCode.MALFORMED_RESPONSE,
    ],
)
def test_non_transient_codes_are_never_retried(code):
    assert RetryPolicy().delay(1, err(code)) is None


async def collect_retry(adapter, policy=None):
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    events = [
        e
        async for e in stream_with_retry(
            adapter, simple_request(), policy or RetryPolicy(jitter=0.0), sleep
        )
    ]
    return events, sleeps


async def test_retry_before_output_then_success():
    adapter = ScriptedAdapter([fail(ErrorCode.SERVER), fail(ErrorCode.RATE_LIMIT), say("ok")])
    events, sleeps = await collect_retry(adapter)
    assert [type(e).__name__ for e in events if isinstance(e, Retrying)] == ["Retrying"] * 2
    assert isinstance(events[-1], Done) and sleeps == [0.5, 1.0]


async def test_retry_exhaustion_raises_the_last_error():
    adapter = ScriptedAdapter([fail(ErrorCode.SERVER, "down")] * 3)
    with pytest.raises(LLMError, match="down"):
        await collect_retry(adapter, RetryPolicy(max_retries=2, jitter=0.0))
    assert len(adapter.requests) == 3


async def test_auth_failure_is_raised_immediately():
    adapter = ScriptedAdapter([fail(ErrorCode.AUTH)])
    with pytest.raises(LLMError):
        await collect_retry(adapter)
    assert len(adapter.requests) == 1


async def test_mid_stream_transport_failure_retries_and_marks_output_discarded():
    adapter = ScriptedAdapter([Reply(text="part one part two", fail_after_events=1), say("whole")])
    events, _ = await collect_retry(adapter)
    retry = next(e for e in events if isinstance(e, Retrying))
    assert retry.discarded is True
    assert events[-1].message.text == "whole"


async def test_mid_stream_malformed_response_is_not_retried():
    bad = Reply(text="abc def", fail_after_events=1)
    bad.fail_after_events = 1  # replaced below by a non-retryable error type
    adapter = ScriptedAdapter([bad])

    original = adapter.stream

    async def stream(request):
        agen = original(request)
        yield await agen.__anext__()
        raise LLMError("garbled", ErrorCode.MALFORMED_RESPONSE)

    adapter.stream = stream  # type: ignore[method-assign]
    with pytest.raises(LLMError, match="garbled"):
        await collect_retry(adapter)


# ----------------------------------------------------------------------------- errors


class FakeHeaders(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


def test_retry_after_forms():
    assert parse_retry_after(FakeHeaders({"retry-after": "12"})) == 12.0
    assert parse_retry_after(FakeHeaders({"retry-after-ms": "250"})) == 0.25
    assert parse_retry_after(FakeHeaders({"retry-after": "garbage"})) is None
    assert parse_retry_after(None) is None
    future = parse_retry_after(FakeHeaders({"retry-after": "Wed, 21 Oct 2099 07:28:00 GMT"}))
    assert future is not None and future > 1e6


# ----------------------------------------------------------------------------- providers


def test_parse_model_requires_provider_unless_unambiguous():
    specs = all_specs()
    assert str(parse_model("anthropic/claude-sonnet-5-5", specs)) == "anthropic/claude-sonnet-5-5"
    assert str(parse_model("openrouter/anthropic/claude-3.5", specs)) == (
        "openrouter/anthropic/claude-3.5"
    )
    assert str(parse_model("gemini-2.5-pro", specs)) == "google/gemini-2.5-pro"
    with pytest.raises(ProviderError, match="cannot resolve"):
        parse_model("totally-unknown-model", specs)


def test_bare_model_offered_by_two_providers_is_ambiguous():
    specs = all_specs({"mine": {"base_url": "http://x/v1", "models": ["gpt-5.6-sol"]}})
    with pytest.raises(ProviderError, match="offered by"):
        parse_model("gpt-5.6-sol", specs)


def test_missing_key_error_names_env_var_and_login_command():
    with pytest.raises(ProviderError) as info:
        build_adapter(parse_model("anthropic/claude-sonnet-5-5", all_specs()), all_specs())
    assert "ANTHROPIC_API_KEY" in str(info.value) and "kalash provider anthropic" in str(info.value)


def test_env_key_beats_stored_key_and_ollama_needs_none(monkeypatch):
    specs = all_specs()
    save_credential("openai", "stored-key")
    assert api_key_for(specs["openai"]) == "stored-key"
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    assert api_key_for(specs["openai"]) == "env-key"
    adapter = build_adapter(parse_model("ollama/llama3.1:70b", specs), specs)
    assert adapter.model == "llama3.1:70b"  # tag preserved


def test_sarvam_v2_models_use_the_v2_endpoint_and_context_windows(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "k")
    specs = all_specs()
    v1 = build_adapter(parse_model("sarvam/sarvam-105b", specs), specs)
    v2 = build_adapter(parse_model("sarvam/glm5.3", specs), specs)
    assert v1.base_url.endswith("/v1") and v2.base_url.endswith("/v2")
    assert (v1.context_window, v2.context_window) == (128_000, 1_048_576)
    assert v1.dialect.extra_headers["api-subscription-key"] == "k"


def test_deepseek_uses_the_messages_endpoint(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    specs = all_specs()
    adapter = build_adapter(parse_model("deepseek/deepseek-v4-pro", specs), specs)
    assert adapter.base_url == "https://api.deepseek.com/anthropic"
    assert adapter.dialect == "deepseek" and adapter.context_window == 1_000_000


# ----------------------------------------------------------------------------- credentials


def test_credentials_roundtrip_are_encrypted_and_private():
    save_credential("openai", "sk-secret-value")
    raw = (kalash_home() / "auth.json").read_text()
    assert "sk-secret-value" not in raw and "enc2:" in raw
    assert get_credential("openai") == "sk-secret-value"
    assert stat.S_IMODE((kalash_home() / "auth.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((kalash_home() / "auth.key").stat().st_mode) == 0o600
    assert stored_providers() == ["openai"]
    save_credential("openai", "")
    assert get_credential("openai") is None


def test_tampered_ciphertext_is_detected_not_silently_replaced():
    save_credential("openai", "sk-1")
    path = kalash_home() / "auth.json"
    data = json.loads(path.read_text())
    data["openai"] = data["openai"][:-4] + "AAAA"
    path.write_text(json.dumps(data))
    with pytest.raises(CredentialError, match="integrity"):
        get_credential("openai")


def test_one_corrupt_entry_does_not_block_saving_or_reading_others():
    save_credential("openai", "sk-1")
    path = kalash_home() / "auth.json"
    data = json.loads(path.read_text())
    data["openai"] = data["openai"][:-4] + "AAAA"
    path.write_text(json.dumps(data))
    save_credential("groq", "gsk-2")
    assert get_credential("groq") == "gsk-2"
    assert "openai" in json.loads(path.read_text())  # kept untouched, not dropped


def test_group_readable_key_file_is_refused():
    save_credential("openai", "sk-1")
    (kalash_home() / "auth.key").chmod(0o644)
    with pytest.raises(CredentialError, match="0600"):
        get_credential("openai")


def test_selected_model_roundtrip():
    assert get_selected_model() is None
    set_selected_model("sarvam", "sarvam-105b")
    assert get_selected_model() == "sarvam/sarvam-105b"


def test_message_roundtrip_through_json_preserves_blocks():
    from kalash.llm.types import ImageBlock, ReasoningBlock, ToolCallBlock

    message = Message(
        "assistant",
        [
            ReasoningBlock("r", "sig"),
            TextBlock("t"),
            ToolCallBlock("c", "n", "{}", {"extra_content": {"a": 1}}),
            ImageBlock("image/png", "AAAA"),
        ],
        origin="p/m",
    )
    assert Message.from_dict(json.loads(json.dumps(message.to_dict()))) == message
    assert Usage.from_dict(Usage(1, 2, 3, 4, 5).to_dict()) == Usage(1, 2, 3, 4, 5)


async def test_an_empty_completion_is_retried_once_not_five_times():
    adapter = ScriptedAdapter([fail(ErrorCode.EMPTY_RESPONSE, "empty")] * 4)
    with pytest.raises(LLMError, match="empty"):
        await collect_retry(adapter)
    assert len(adapter.requests) == 2, "one retry, then the caller decides"
