"""Native Gemini adapter against raw ``streamGenerateContent?alt=sse`` payloads.

Wire shapes are taken from ai.google.dev (thought-signatures, function-calling, API reference,
api-errors, thinking); the model is the only thing mocked, over ``httpx.MockTransport``.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import httpx
import pytest

from kalash.llm.catalog import ModelInfo
from kalash.llm.errors import ErrorCode, LLMError
from kalash.llm.gemini import (
    CONTINUE_PROMPT,
    DUMMY_THOUGHT_SIGNATURE,
    GeminiAdapter,
    sanitize_schema,
)
from kalash.llm.http import make_client
from kalash.llm.types import (
    Done,
    ImageBlock,
    Message,
    ReasoningBlock,
    ReasoningDelta,
    Request,
    TextBlock,
    TextDelta,
    ToolCallBlock,
    ToolCallStarted,
    ToolSpec,
    tool_result,
    user_text,
)
from tests.harness.wire import Recorder, collect, final, respond, sse_bytes

MODEL = "gemini-3.8-flash"
M = 1_048_576


def _info(**kw: Any) -> ModelInfo:
    return ModelInfo(**{"id": MODEL, "context": M, "max_output": 65_536, **kw})


LEVEL = _info(
    thinking="level", efforts=("low", "medium", "high"), can_disable_thinking=False, sampling=False
)
LEVEL_MINIMAL = _info(
    thinking="level",
    efforts=("minimal", "low", "medium", "high"),
    can_disable_thinking=False,
    sampling=False,
)
BUDGET_PRO = _info(thinking="budget", efforts=("low", "medium", "high"), can_disable_thinking=False)
BUDGET_FLASH = _info(
    thinking="budget", efforts=("none", "low", "medium", "high"), can_disable_thinking=True
)
PLAIN = _info(thinking="none")
SAMPLING = _info(thinking="none", sampling=True)


def adapter(
    rec: Recorder,
    *,
    info: ModelInfo | None = LEVEL,
    model: str = MODEL,
    provider: str = "google",
    api_key: str | None = "k",
) -> GeminiAdapter:
    return GeminiAdapter(
        provider=provider,
        model=model,
        base_url="https://g.test/v1beta",
        api_key=api_key,
        context_window=M,
        info=info,
        client=make_client(transport=httpx.MockTransport(rec)),
    )


def request(text: str = "hi", **kw: Any) -> Request:
    return Request(model=kw.pop("model", MODEL), system="sys", messages=[user_text(text)], **kw)


def chunk(*parts: dict[str, Any], finish: str | None = None, usage: Any = None, **extra: Any):
    candidate: dict[str, Any] = {"content": {"role": "model", "parts": list(parts)}, "index": 0}
    if finish:
        candidate["finishReason"] = finish
    data: dict[str, Any] = {"candidates": [candidate], **extra}
    if usage is not None:
        data["usageMetadata"] = usage
    return data


def ok(*chunks: dict[str, Any]) -> httpx.Response:
    return respond(sse_bytes(list(chunks)))


def text_reply(text: str = "ok", finish: str = "STOP", **kw: Any) -> httpx.Response:
    return ok(chunk({"text": text}, finish=finish, **kw))


def google_json(
    status: int,
    message: str,
    grpc: str,
    details: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    error = {"code": status, "message": message, "status": grpc, "details": details or []}
    return httpx.Response(status, json={"error": error}, headers=headers)


def quota_details(quota_id: str, retry_delay: str | None = None) -> list[dict[str, Any]]:
    metric = "generativelanguage.googleapis.com/generate_content_free_tier_requests"
    details: list[dict[str, Any]] = [
        {
            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [{"quotaMetric": metric, "quotaId": quota_id}],
        }
    ]
    if retry_delay:
        details.append(
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}
        )
    return details


async def fails(rec: Recorder, **kw: Any) -> LLMError:
    with pytest.raises(LLMError) as info:
        await collect(adapter(rec, **kw), request())
    return info.value


def assistant(*blocks: Any, origin: str | None = f"google/{MODEL}") -> Message:
    return Message("assistant", list(blocks), origin=origin)


def serialize(*messages: Message, **kw: Any) -> list[dict[str, Any]]:
    info = kw.pop("info", LEVEL)
    req = Request(model=MODEL, system="", messages=list(messages))
    return adapter(Recorder(), info=info, **kw).serialize(req)


FC_READ = {"functionCall": {"name": "read", "args": {"path": "a.py"}}}

# ----------------------------------------------------------------------------- streaming


async def test_text_stream_with_thought_summaries_and_usage():
    usage = {
        "promptTokenCount": 100,
        "cachedContentTokenCount": 40,
        "candidatesTokenCount": 20,
        "thoughtsTokenCount": 30,
        "totalTokenCount": 150,
    }
    rec = Recorder(
        ok(
            chunk({"text": "Considering", "thought": True}),
            chunk({"text": " the options.", "thought": True}),
            chunk({"text": "Hel"}),
            chunk({"text": "lo"}, finish="STOP", usage=usage),
        )
    )
    events = await collect(adapter(rec), request())
    done = events[-1]
    assert isinstance(done, Done) and done.finish == "stop"
    assert [e.text for e in events if isinstance(e, ReasoningDelta)] == [
        "Considering",
        " the options.",
    ]
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hel", "lo"]
    assert done.message.content == [ReasoningBlock("Considering the options."), TextBlock("Hello")]
    assert done.message.origin == f"google/{MODEL}"
    # input includes cached tokens; billed output = candidates + thoughts
    assert done.usage.input_tokens == 100 and done.usage.cache_read_tokens == 40
    assert done.usage.output_tokens == 50 and done.usage.reasoning_tokens == 30


async def test_usage_comes_from_the_last_cumulative_chunk_and_trailing_usage_only_chunk():
    rec = Recorder(
        ok(
            chunk({"text": "a"}, usage={"promptTokenCount": 10, "candidatesTokenCount": 1}),
            chunk(
                {"text": "b"},
                finish="STOP",
                usage={"promptTokenCount": 10, "candidatesTokenCount": 7, "thoughtsTokenCount": 3},
            ),
            {"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 9}},
        )
    )
    usage = (await final(adapter(rec), request())).usage
    assert (usage.input_tokens, usage.output_tokens, usage.reasoning_tokens) == (10, 9, 0)


async def test_missing_null_and_partial_usage_are_tolerated():
    for usage in (
        None,
        {},
        {"totalTokenCount": 9},
        {"promptTokenCount": "x", "thoughtsTokenCount": -1},
    ):
        rec = Recorder(ok(chunk({"text": "a"}, finish="STOP", usage=usage)))
        done = await final(adapter(rec), request())
        assert done.usage.input_tokens is None and done.usage.output_tokens is None
        assert done.usage.cache_read_tokens == 0 and done.usage.reasoning_tokens == 0
    rec = Recorder(ok({"usageMetadata": None, **chunk({"text": "a"}, finish="STOP")}))
    assert (await final(adapter(rec), request())).message.text == "a"


async def test_function_call_arrives_whole_with_signature_and_generated_id():
    call = {**FC_READ, "thoughtSignature": "SIG_A"}
    rec = Recorder(ok(chunk(call, finish="STOP")))
    events = await collect(adapter(rec), request())
    assert [e for e in events if isinstance(e, ToolCallStarted)] == [
        ToolCallStarted(0, "call_0_read", "read")
    ]
    done = events[-1]
    block = done.message.tool_calls[0]
    assert (block.id, block.name, json.loads(block.arguments)) == (
        "call_0_read",
        "read",
        {"path": "a.py"},
    )
    assert block.extra == {"thoughtSignature": "SIG_A"}
    assert done.finish == "tool_calls"  # STOP + a function call


async def test_api_supplied_call_id_is_used_and_kept_for_replay():
    part = {"functionCall": {"id": "fc-77", "name": "read", "args": {}}, "thoughtSignature": "S"}
    done = await final(adapter(Recorder(ok(chunk(part, finish="STOP")))), request())
    block = done.message.tool_calls[0]
    assert block.id == "fc-77" and block.extra == {"thoughtSignature": "S", "id": "fc-77"}


async def test_parallel_calls_signature_only_on_first_and_ids_are_unique():
    rec = Recorder(
        ok(
            chunk(
                {**FC_READ, "thoughtSignature": "SIG_A"},
                {"functionCall": {"name": "read", "args": {"path": "b.py"}}},
                finish="STOP",
            )
        )
    )
    done = await final(adapter(rec), request())
    first, second = done.message.tool_calls
    assert (first.id, second.id) == ("call_0_read", "call_1_read")
    assert first.extra == {"thoughtSignature": "SIG_A"} and second.extra is None
    assert json.loads(second.arguments) == {"path": "b.py"}


async def test_duplicate_api_call_ids_are_made_unique():
    dup = {"functionCall": {"id": "x", "name": "read", "args": {}}}
    done = await final(adapter(Recorder(ok(chunk(dup, dup, finish="STOP")))), request())
    ids = [c.id for c in done.message.tool_calls]
    assert ids[0] == "x" and len(set(ids)) == 2


async def test_call_without_args_is_empty_object_and_unicode_is_kept():
    parts = [
        {"functionCall": {"name": "glob"}},
        {"functionCall": {"name": "say", "args": {"t": "héllo ✓"}}},
    ]
    done = await final(adapter(Recorder(ok(chunk(*parts, finish="STOP")))), request())
    first, second = done.message.tool_calls
    assert first.arguments == "{}" and second.arguments == '{"t": "héllo ✓"}'


async def test_signature_on_a_late_empty_text_part_becomes_its_own_block():
    rec = Recorder(
        ok(
            chunk({"text": "Hello"}),
            chunk({"text": " world"}),
            chunk({"text": "", "thoughtSignature": "SIG_T"}, finish="STOP"),
        )
    )
    done = await final(adapter(rec), request())
    assert done.message.text == "Hello world"
    assert done.message.content == [
        TextBlock("Hello world"),
        TextBlock("", {"thoughtSignature": "SIG_T"}),
    ]


async def test_signed_text_part_is_never_merged_with_unsigned_text():
    rec = Recorder(
        ok(
            chunk({"text": "One. "}),
            chunk({"text": "Two. ", "thoughtSignature": "S2"}),
            chunk({"text": "Three."}, finish="STOP"),
        )
    )
    blocks = (await final(adapter(rec), request())).message.content
    assert blocks == [
        TextBlock("One. "),
        TextBlock("Two. ", {"thoughtSignature": "S2"}),
        TextBlock("Three."),
    ]


async def test_signature_on_a_thought_summary_is_kept_on_the_reasoning_block():
    rec = Recorder(
        ok(
            chunk({"text": "Thinking", "thought": True, "thoughtSignature": "SIG_R"}),
            chunk({"text": "answer"}, finish="STOP"),
        )
    )
    blocks = (await final(adapter(rec), request())).message.content
    assert blocks == [ReasoningBlock("Thinking", "SIG_R"), TextBlock("answer")]


async def test_only_candidate_zero_text_parts_count_and_other_part_kinds_are_ignored():
    extra_candidate = {"index": 1, "content": {"parts": [{"text": "OTHER"}]}}
    first = chunk(
        {"inlineData": {"mimeType": "image/png", "data": "AA"}},
        {"executableCode": {"language": "PYTHON", "code": "1"}},
        {"text": "kept"},
        finish="STOP",
    )
    first["candidates"].append(extra_candidate)
    done = await final(adapter(Recorder(ok(first)), info=LEVEL), request())
    assert done.message.text == "kept"


async def test_sse_comments_blank_data_and_done_sentinel_are_skipped():
    body = (
        b": keepalive\n\n"
        b"data:  \n\n" + sse_bytes([chunk({"text": "x"}, finish="STOP")]) + b"data: [DONE]\n\n"
    )
    done = await final(adapter(Recorder(respond(body))), request())
    assert done.message.text == "x"


# ----------------------------------------------------------------------------- finish reasons


@pytest.mark.parametrize(
    ("reason", "finish"),
    [
        ("STOP", "stop"),
        ("MAX_TOKENS", "max_tokens"),
        ("SAFETY", "content_filter"),
        ("RECITATION", "content_filter"),
        ("BLOCKLIST", "content_filter"),
        ("PROHIBITED_CONTENT", "content_filter"),
        ("SPII", "content_filter"),
    ],
)
async def test_finish_reason_mapping(reason, finish):
    done = await final(adapter(Recorder(text_reply("x", reason))), request())
    assert done.finish == finish


@pytest.mark.parametrize(
    "reason",
    [
        "MALFORMED_FUNCTION_CALL",
        "UNEXPECTED_TOOL_CALL",
        "TOO_MANY_TOOL_CALLS",
        "MALFORMED_RESPONSE",
        "OTHER",
        "SOMETHING_NEW",
    ],
)
async def test_bad_finish_reasons_raise_malformed_response_naming_the_reason(reason):
    error = await fails(Recorder(text_reply("x", reason)))
    assert error.code == ErrorCode.MALFORMED_RESPONSE and reason in error.message


async def test_missing_thought_signature_finish_is_an_invalid_request():
    rec = Recorder(ok(chunk({"text": ""}, finish="MISSING_THOUGHT_SIGNATURE")))
    error = await fails(rec)
    assert error.code == ErrorCode.INVALID_REQUEST and "thought signature" in error.message
    assert not error.retryable


async def test_continuation_finish_is_reported_as_unsupported():
    error = await fails(Recorder(text_reply("part", "CONTINUATION")))
    assert error.code == ErrorCode.MALFORMED_RESPONSE and "not supported" in error.message


async def test_finish_message_is_included_in_the_error():
    rec = Recorder(
        ok(
            {
                "candidates": [
                    {
                        "finishReason": "MALFORMED_FUNCTION_CALL",
                        "finishMessage": "Malformed function call: call:read{",
                    }
                ]
            }
        )
    )
    assert "Malformed function call: call:read{" in (await fails(rec)).message


async def test_max_tokens_with_only_thoughts_is_returned_not_an_error():
    rec = Recorder(ok(chunk({"text": "hmm", "thought": True}, finish="MAX_TOKENS")))
    done = await final(adapter(rec), request())
    assert done.finish == "max_tokens" and done.message.content == [ReasoningBlock("hmm")]


async def test_prompt_block_reason_is_content_filter_not_an_error():
    rec = Recorder(
        ok(
            {
                "promptFeedback": {"blockReason": "PROHIBITED_CONTENT"},
                "usageMetadata": {"promptTokenCount": 5},
            }
        )
    )
    done = await final(adapter(rec), request())
    assert done.finish == "content_filter" and done.message.content == []
    assert done.usage.input_tokens == 5


async def test_stream_without_a_finish_reason_is_a_retryable_transport_error():
    error = await fails(Recorder(ok(chunk({"text": "partial"}))))
    assert error.code == ErrorCode.TRANSPORT and error.retryable


async def test_unspecified_finish_reason_is_not_a_finish():
    rec = Recorder(
        ok(
            chunk({"text": "a"}, finish="FINISH_REASON_UNSPECIFIED"),
            chunk({"text": "b"}, finish="STOP"),
        )
    )
    assert (await final(adapter(rec), request())).message.text == "ab"


async def test_finish_reason_alone_in_a_contentless_chunk_still_ends_the_turn():
    rec = Recorder(ok(chunk({"text": "hi"}), {"candidates": [{"finishReason": "STOP"}]}))
    assert (await final(adapter(rec), request())).finish == "stop"


@pytest.mark.parametrize(
    "parts", [[], [{"text": ""}], [{"text": "thinking only", "thought": True}]]
)
async def test_stop_with_no_text_and_no_call_is_an_empty_response(parts):
    error = await fails(Recorder(ok(chunk(*parts, finish="STOP"))))
    assert error.code == ErrorCode.EMPTY_RESPONSE and error.retryable


async def test_call_without_a_name_is_malformed():
    rec = Recorder(ok(chunk({"functionCall": {"args": {}}}, finish="STOP")))
    assert (await fails(rec)).code == ErrorCode.MALFORMED_RESPONSE


async def test_unparseable_chunk_is_malformed():
    error = await fails(Recorder(respond(sse_bytes(["{not json"]))))
    assert error.code == ErrorCode.MALFORMED_RESPONSE


# ----------------------------------------------------------------------------- errors


async def test_error_object_inside_a_200_stream_is_classified():
    err = {"error": {"code": 503, "message": "The model is overloaded.", "status": "UNAVAILABLE"}}
    error = await fails(Recorder(ok(chunk({"text": "par"}), err)))
    assert error.code == ErrorCode.SERVER and error.retryable and error.status == 503


async def test_in_stream_per_minute_limit_carries_the_retry_delay():
    err = {
        "error": {
            "code": 429,
            "message": "You exceeded your current quota, please check your plan and billing.",
            "status": "RESOURCE_EXHAUSTED",
            "details": quota_details("GenerateRequestsPerMinutePerProjectPerModel", "2.5s"),
        }
    }
    error = await fails(Recorder(ok(err)))
    assert error.code == ErrorCode.RATE_LIMIT and error.retry_after == 2.5


async def test_per_minute_429_is_retryable_even_though_the_text_mentions_billing():
    message = "You exceeded your current quota, please check your plan and billing details."
    rec = Recorder(
        google_json(
            429,
            message,
            "RESOURCE_EXHAUSTED",
            quota_details("GenerateContentPaidTierInputTokensPerModelPerMinute-PaidTier2", "10s"),
        )
    )
    error = await fails(rec)
    assert error.code == ErrorCode.RATE_LIMIT and error.retryable
    assert error.retry_after == 10.0 and error.status == 429


async def test_daily_quota_429_is_not_retryable():
    rec = Recorder(
        google_json(
            429,
            "You exceeded your current quota, please check your plan and billing details.",
            "RESOURCE_EXHAUSTED",
            quota_details("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "33s"),
        )
    )
    error = await fails(rec)
    assert error.code == ErrorCode.QUOTA and not error.retryable


@pytest.mark.parametrize(
    "message",
    [
        "Your billing account has exceeded its monthly spending cap.",
        "Your Prepay credit balance is depleted.",
        "You have exhausted your capacity on this model.",
    ],
)
async def test_spend_cap_and_credit_exhaustion_are_quota_errors(message):
    error = await fails(Recorder(google_json(429, message, "RESOURCE_EXHAUSTED")))
    assert error.code == ErrorCode.QUOTA and not error.retryable


async def test_http_402_prepay_depleted_is_quota():
    error = await fails(Recorder(google_json(402, "Credit balance depleted", "RESOURCE_EXHAUSTED")))
    assert error.code == ErrorCode.QUOTA


async def test_plain_429_is_a_rate_limit_and_honours_retry_after_header():
    rec = Recorder(
        google_json(
            429, "Resource has been exhausted", "RESOURCE_EXHAUSTED", headers={"retry-after": "7"}
        )
    )
    error = await fails(rec)
    assert error.code == ErrorCode.RATE_LIMIT and error.retry_after == 7.0


async def test_retry_delay_is_read_from_the_message_when_there_are_no_details():
    rec = Recorder(google_json(429, "Quota exceeded. Please retry in 33.6s.", "RESOURCE_EXHAUSTED"))
    assert (await fails(rec)).retry_after == pytest.approx(33.6)


async def test_missing_thought_signature_400_keeps_its_text():
    text = "Function call `FC1` in the `1.` content block is missing a `thought_signature`."
    error = await fails(Recorder(google_json(400, text, "INVALID_ARGUMENT")))
    assert error.code == ErrorCode.INVALID_REQUEST and text in error.message
    assert not error.retryable


@pytest.mark.parametrize(
    "message",
    [
        "The input token count (1632254) exceeds the maximum number of tokens allowed (1048576).",
        "Request exceeds the maximum number of tokens allowed for this model.",
    ],
)
async def test_context_overflow_texts(message):
    error = await fails(Recorder(google_json(400, message, "INVALID_ARGUMENT")))
    assert error.code == ErrorCode.CONTEXT_OVERFLOW and not error.retryable


async def test_invalid_api_key_400_is_an_auth_error():
    details = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}]
    rec = Recorder(
        google_json(
            400, "API key not valid. Please pass a valid API key.", "INVALID_ARGUMENT", details
        )
    )
    error = await fails(rec)
    assert error.code == ErrorCode.AUTH and error.status == 400


@pytest.mark.parametrize(
    ("status", "grpc", "code"),
    [
        (403, "PERMISSION_DENIED", ErrorCode.AUTH),
        (404, "NOT_FOUND", ErrorCode.INVALID_REQUEST),
        (400, "FAILED_PRECONDITION", ErrorCode.INVALID_REQUEST),
        (500, "INTERNAL", ErrorCode.SERVER),
        (503, "UNAVAILABLE", ErrorCode.SERVER),
        (504, "DEADLINE_EXCEEDED", ErrorCode.TIMEOUT),
        (499, "CANCELLED", ErrorCode.TRANSPORT),
    ],
)
async def test_http_status_classification(status, grpc, code):
    error = await fails(Recorder(google_json(status, "boom", grpc)))
    assert error.code == code and error.status == status


async def test_non_json_error_body_is_classified_by_status():
    error = await fails(Recorder(httpx.Response(502, text="<html>Bad Gateway</html>")))
    assert error.code == ErrorCode.SERVER and "Bad Gateway" in error.message


async def test_connection_failures_map_to_transport_and_timeout():
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    assert (await fails(Recorder(refused))).code == ErrorCode.TRANSPORT
    assert (await fails(Recorder(slow))).code == ErrorCode.TIMEOUT


async def test_error_wrapped_in_a_json_array_is_unwrapped():
    body = [{"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}}]
    error = await fails(Recorder(httpx.Response(503, json=body)))
    assert error.code == ErrorCode.SERVER and error.message == "overloaded"


async def test_two_step_tool_loop_over_http_replays_the_signature_on_the_same_part():
    """Step 1 returns a signed call; step 2's request must carry it back (else Gemini 3/4 400s)."""
    seen: list[dict[str, Any]] = []

    def second_step(http_request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(http_request.content))
        return text_reply("done")

    first = ok(
        chunk(
            {"text": "plan", "thought": True},
            {
                "functionCall": {"id": "fc-1", "name": "read", "args": {"p": "a"}},
                "thoughtSignature": "SIG_1",
            },
            finish="STOP",
            usage={"promptTokenCount": 7, "candidatesTokenCount": 2},
        )
    )
    gemini = adapter(Recorder(first, second_step))
    step1 = await final(gemini, request("read a"))
    call = step1.message.tool_calls[0]
    history = [user_text("read a"), step1.message, tool_result(call, "AAA")]
    step2 = await final(gemini, Request(model=MODEL, system="sys", messages=history))
    assert step2.message.text == "done"
    assert seen[0]["contents"] == [
        {"role": "user", "parts": [{"text": "read a"}]},
        {
            "role": "model",
            "parts": [
                {
                    "functionCall": {"id": "fc-1", "name": "read", "args": {"p": "a"}},
                    "thoughtSignature": "SIG_1",
                }
            ],
        },
        {
            "role": "user",
            "parts": [
                {"functionResponse": {"id": "fc-1", "name": "read", "response": {"output": "AAA"}}}
            ],
        },
    ]


# ----------------------------------------------------------------------------- request wire


async def test_endpoint_headers_and_body_shape():
    rec = Recorder(text_reply())
    tool = ToolSpec(
        "read", "Read a file", {"type": "object", "properties": {"p": {"type": "string"}}}
    )
    await collect(adapter(rec), request(tools=[tool], max_tokens=1000))
    http_request = rec.requests[0]
    assert (
        str(http_request.url)
        == f"https://g.test/v1beta/models/{MODEL}:streamGenerateContent?alt=sse"
    )
    assert http_request.method == "POST"
    assert http_request.headers["x-goog-api-key"] == "k"
    assert "authorization" not in http_request.headers
    assert http_request.headers["content-type"].startswith("application/json")
    body = rec.body()
    assert set(body) == {"contents", "systemInstruction", "tools", "generationConfig"}
    assert body["systemInstruction"] == {"parts": [{"text": "sys"}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
    assert body["generationConfig"] == {"maxOutputTokens": 1000}
    assert "toolConfig" not in body and "model" not in body and "stream" not in body


async def test_no_api_key_means_no_auth_header_and_no_system_means_no_instruction():
    rec = Recorder(text_reply())
    req = Request(model=MODEL, system="", messages=[user_text("hi")])
    await collect(adapter(rec, api_key=None), req)
    assert "x-goog-api-key" not in rec.requests[0].headers
    assert set(rec.body()) == {"contents"}


async def test_models_prefix_is_not_doubled_in_the_url():
    rec = Recorder(text_reply())
    await collect(adapter(rec), request(model=f"models/{MODEL}"))
    assert rec.requests[0].url.path == f"/v1beta/models/{MODEL}:streamGenerateContent"


async def test_default_client_uses_a_long_idle_timeout_and_aclose_closes_it():
    gemini = GeminiAdapter(
        provider="google", model=MODEL, base_url="https://x.test", api_key=None, context_window=M
    )
    assert gemini._client.timeout.read >= 1200  # Gemini 4 asks for >= 20 minutes per call
    await gemini.aclose()
    assert gemini._client.is_closed


@pytest.mark.parametrize(
    ("info", "effort", "expected"),
    [
        (LEVEL, "high", {"thinkingLevel": "HIGH", "includeThoughts": True}),
        (LEVEL, "low", {"thinkingLevel": "LOW", "includeThoughts": True}),
        (LEVEL, "medium", {"thinkingLevel": "MEDIUM", "includeThoughts": True}),
        (LEVEL, None, None),
        (LEVEL, "minimal", {"thinkingLevel": "LOW", "includeThoughts": True}),  # unsupported
        (LEVEL, "none", {"thinkingLevel": "LOW", "includeThoughts": True}),  # cannot disable
        (LEVEL, "max", {"thinkingLevel": "HIGH", "includeThoughts": True}),
        (LEVEL, "turbo", None),
        (LEVEL_MINIMAL, "minimal", {"thinkingLevel": "MINIMAL", "includeThoughts": True}),
        (BUDGET_FLASH, "low", {"thinkingBudget": 1024, "includeThoughts": True}),
        (BUDGET_FLASH, "medium", {"thinkingBudget": 8192, "includeThoughts": True}),
        (BUDGET_FLASH, "high", {"thinkingBudget": 24576, "includeThoughts": True}),
        (BUDGET_FLASH, "none", {"thinkingBudget": 0, "includeThoughts": True}),
        (BUDGET_PRO, "none", {"thinkingBudget": 1024, "includeThoughts": True}),  # never 0
        (BUDGET_PRO, "high", {"thinkingBudget": 24576, "includeThoughts": True}),
        (BUDGET_PRO, None, None),
        (PLAIN, "high", None),
        (None, "high", None),
    ],
)
async def test_thinking_config_matrix(info, effort, expected):
    rec = Recorder(text_reply())
    await collect(adapter(rec, info=info), request(reasoning_effort=effort))
    config = rec.body().get("generationConfig", {}).get("thinkingConfig")
    assert config == expected
    if config:
        assert not {"thinkingLevel", "thinkingBudget"} <= set(config)  # never both


async def test_sampling_parameters_are_omitted_when_the_model_ignores_them():
    rec = Recorder(text_reply(), text_reply(), text_reply())
    await collect(adapter(rec, info=LEVEL), request(temperature=0.3, max_tokens=50))
    await collect(adapter(rec, info=SAMPLING), request(temperature=0.3))
    await collect(adapter(rec, info=SAMPLING), request())
    for sent in (rec.body(0), rec.body(1), rec.body(2)):
        assert not {"topP", "topK"} & set(sent.get("generationConfig", {}))
    assert "temperature" not in rec.body(0)["generationConfig"]
    assert rec.body(1)["generationConfig"]["temperature"] == 0.3
    assert "generationConfig" not in rec.body(2)


async def test_max_output_tokens_never_exceeds_the_model_limit():
    rec = Recorder(text_reply(), text_reply())
    await collect(adapter(rec, info=LEVEL), request(max_tokens=500_000))
    await collect(adapter(rec, info=None), request(max_tokens=500_000))
    assert rec.body(0)["generationConfig"]["maxOutputTokens"] == 65_536  # no long decode
    assert rec.body(1)["generationConfig"]["maxOutputTokens"] == 500_000


async def test_tool_declarations_are_sanitised_and_empty_parameters_omitted():
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "title": "ReadArgs",
        "additionalProperties": False,
        "properties": {"path": {"type": "string", "default": "x"}},
        "required": ["path"],
    }
    tools = [
        ToolSpec("read", "Read a file", schema),
        ToolSpec("noargs", "", {"type": "object", "properties": {}}),
    ]
    rec = Recorder(text_reply())
    await collect(adapter(rec), request(tools=tools))
    (tool,) = rec.body()["tools"]
    read, noargs = tool["functionDeclarations"]
    assert read == {
        "name": "read",
        "description": "Read a file",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }
    assert noargs == {"name": "noargs", "description": "noargs"}  # no empty OBJECT, no empty text


# ----------------------------------------------------------------------------- serialization


def test_user_text_and_image_become_user_parts():
    wire = serialize(
        Message("user", [TextBlock("look"), ImageBlock("image/png", "QUJD"), TextBlock("")])
    )
    assert wire == [
        {
            "role": "user",
            "parts": [{"text": "look"}, {"inlineData": {"mimeType": "image/png", "data": "QUJD"}}],
        }
    ]


def test_tool_results_success_error_and_empty():
    c1, c2, c3 = (ToolCallBlock(f"c{i}", "read", "{}") for i in (1, 2, 3))
    wire = serialize(
        user_text("go"),
        assistant(c1, c2, c3, origin="x/y"),
        tool_result(c1, "contents"),
        tool_result(c2, "denied", is_error=True),
        tool_result(c3, ""),
        info=None,
    )
    assert wire[2] == {
        "role": "user",
        "parts": [
            {"functionResponse": {"name": "read", "response": {"output": "contents"}}},
            {"functionResponse": {"name": "read", "response": {"error": "denied"}}},
            {"functionResponse": {"name": "read", "response": {"output": "(no output)"}}},
        ],
    }


async def test_signature_round_trip_lands_on_the_same_function_call_part():
    call = {**FC_READ, "thoughtSignature": "SIG_A"}
    rec = Recorder(ok(chunk({"text": "plan", "thought": True}, call, finish="STOP")))
    gemini = adapter(rec)
    done = await final(gemini, request())
    block = done.message.tool_calls[0]
    wire = gemini.serialize(
        Request(
            model=MODEL,
            system="",
            messages=[user_text("hi"), done.message, tool_result(block, "file body")],
        )
    )
    assert wire == [
        {"role": "user", "parts": [{"text": "hi"}]},
        # thought summary dropped; signature on the very part it arrived on
        {
            "role": "model",
            "parts": [
                {
                    "functionCall": {"name": "read", "args": {"path": "a.py"}},
                    "thoughtSignature": "SIG_A",
                }
            ],
        },
        {
            "role": "user",
            "parts": [{"functionResponse": {"name": "read", "response": {"output": "file body"}}}],
        },
    ]


def test_signature_is_replayed_only_to_the_route_that_produced_it():
    call = ToolCallBlock("call_0_read", "read", '{"path": "a.py"}', {"thoughtSignature": "REAL"})
    text = TextBlock("answer", {"thoughtSignature": "TEXT_SIG"})
    for origin, expected_call_sig, expected_text_sig in (
        (f"google/{MODEL}", "REAL", "TEXT_SIG"),
        ("google/gemini-3.6-flash", DUMMY_THOUGHT_SIGNATURE, None),
        ("openai/gpt-x", DUMMY_THOUGHT_SIGNATURE, None),
        (None, DUMMY_THOUGHT_SIGNATURE, None),
    ):
        wire = serialize(
            user_text("go"),
            assistant(text, call, origin=origin),
            tool_result(call, "r"),
        )
        parts = wire[1]["parts"]
        assert parts[0].get("thoughtSignature") == expected_text_sig
        assert parts[1]["thoughtSignature"] == expected_call_sig


def test_dummy_signature_only_for_gemini_3_and_4_models():
    call = ToolCallBlock("c", "read", "{}")
    messages = [user_text("go"), assistant(call, origin="openai/gpt-x"), tool_result(call, "r")]
    level = serialize(*messages, info=LEVEL)
    budget = serialize(*messages, info=BUDGET_FLASH)
    unknown = serialize(*messages, info=None)
    assert level[1]["parts"][0]["thoughtSignature"] == "skip_thought_signature_validator"
    assert "thoughtSignature" not in budget[1]["parts"][0]
    assert "thoughtSignature" not in unknown[1]["parts"][0]


def test_own_call_that_lost_its_signature_gets_the_dummy_on_a_level_model():
    call = ToolCallBlock("c", "read", "{}")
    wire = serialize(user_text("go"), assistant(call), tool_result(call, "r"))
    assert wire[1]["parts"][0]["thoughtSignature"] == DUMMY_THOUGHT_SIGNATURE


def test_parallel_calls_layout_is_fc1_sig_fc2_fc3_then_fr1_fr2_fr3_in_call_order():
    c1 = ToolCallBlock("a", "read", '{"path": "1"}', {"thoughtSignature": "SIG"})
    c2 = ToolCallBlock("b", "read", '{"path": "2"}')
    c3 = ToolCallBlock("c", "grep", '{"pattern": "x"}')
    wire = serialize(
        user_text("go"),
        assistant(c1, c2, c3),
        tool_result(c3, "third"),  # results finished out of order
        tool_result(c1, "first"),
        tool_result(c2, "second"),
    )
    assert wire == [
        {"role": "user", "parts": [{"text": "go"}]},
        {
            "role": "model",
            "parts": [
                {
                    "functionCall": {"name": "read", "args": {"path": "1"}},
                    "thoughtSignature": "SIG",
                },
                {"functionCall": {"name": "read", "args": {"path": "2"}}},
                {"functionCall": {"name": "grep", "args": {"pattern": "x"}}},
            ],
        },
        {  # all results in ONE user content, in the order the calls were made
            "role": "user",
            "parts": [
                {"functionResponse": {"name": "read", "response": {"output": "first"}}},
                {"functionResponse": {"name": "read", "response": {"output": "second"}}},
                {"functionResponse": {"name": "grep", "response": {"output": "third"}}},
            ],
        },
    ]


def test_sequential_calls_each_keep_their_own_signature():
    c1 = ToolCallBlock("c1", "check", "{}", {"thoughtSignature": "SIG_A"})
    c2 = ToolCallBlock("c2", "book", "{}", {"thoughtSignature": "SIG_B"})
    wire = serialize(
        user_text("go"),
        assistant(c1),
        tool_result(c1, "delayed"),
        assistant(c2),
        tool_result(c2, "booked"),
    )
    assert [c["role"] for c in wire] == ["user", "model", "user", "model", "user"]
    assert wire[1]["parts"][0]["thoughtSignature"] == "SIG_A"
    assert wire[3]["parts"][0]["thoughtSignature"] == "SIG_B"
    assert wire[2]["parts"][0]["functionResponse"]["name"] == "check"
    assert wire[4]["parts"][0]["functionResponse"]["name"] == "book"


def test_api_call_ids_are_echoed_on_call_and_response_for_the_producing_route_only():
    call = ToolCallBlock("fc-1", "read", "{}", {"thoughtSignature": "S", "id": "fc-1"})
    own = serialize(user_text("go"), assistant(call), tool_result(call, "r"))
    assert own[1]["parts"][0]["functionCall"]["id"] == "fc-1"
    assert own[2]["parts"][0]["functionResponse"]["id"] == "fc-1"
    foreign = serialize(user_text("go"), assistant(call, origin="openai/x"), tool_result(call, "r"))
    assert "id" not in foreign[1]["parts"][0]["functionCall"]
    assert "id" not in foreign[2]["parts"][0]["functionResponse"]


def test_generated_call_ids_are_not_sent_to_the_api():
    call = ToolCallBlock("call_0_read", "read", "{}", {"thoughtSignature": "S"})
    wire = serialize(user_text("go"), assistant(call), tool_result(call, "r"))
    assert "id" not in wire[1]["parts"][0]["functionCall"]
    assert "id" not in wire[2]["parts"][0]["functionResponse"]


def test_invalid_or_non_object_arguments_replay_as_empty_args():
    calls = [
        ToolCallBlock("a", "t", "{not json", {"thoughtSignature": "S"}),
        ToolCallBlock("b", "t", "[1, 2]"),
        ToolCallBlock("c", "t", ""),
    ]
    wire = serialize(user_text("go"), assistant(*calls))
    assert [p["functionCall"]["args"] for p in wire[1]["parts"]] == [{}, {}, {}]


def test_thought_summary_is_dropped_but_its_signature_moves_to_the_next_part():
    thought = ReasoningBlock("secret plan", signature="SIG_R")
    wire = serialize(user_text("q"), assistant(thought, TextBlock("answer")), user_text("next"))
    assert wire[1] == {"role": "model", "parts": [{"text": "answer", "thoughtSignature": "SIG_R"}]}
    assert "secret plan" not in json.dumps(wire)


def test_thought_signature_does_not_overwrite_a_signed_part_and_unsigned_thoughts_vanish():
    signed = ToolCallBlock("c", "read", "{}", {"thoughtSignature": "SIG_C"})
    wire = serialize(
        user_text("q"),
        assistant(ReasoningBlock("t", signature="SIG_R"), signed),
        tool_result(signed, "r"),
        assistant(ReasoningBlock("only text, no signature"), TextBlock("done")),
        user_text("more"),
    )
    assert wire[1]["parts"] == [
        {"text": "", "thoughtSignature": "SIG_R"},  # shape the API itself emits
        {"functionCall": {"name": "read", "args": {}}, "thoughtSignature": "SIG_C"},
    ]
    assert wire[3]["parts"] == [{"text": "done"}]


def test_thought_signature_from_another_route_is_not_replayed():
    wire = serialize(
        user_text("q"),
        assistant(ReasoningBlock("t", signature="FOREIGN"), TextBlock("a"), origin="anthropic/m"),
        user_text("q2"),
    )
    assert wire[1]["parts"] == [{"text": "a"}]


def test_compat_route_signatures_are_recovered_from_existing_sessions():
    extra = {"extra_content": {"google": {"thought_signature": "OLD"}}}
    call = ToolCallBlock("function-call-1", "read", "{}", extra)
    wire = serialize(user_text("go"), assistant(call), tool_result(call, "r"))
    assert wire[1]["parts"][0]["thoughtSignature"] == "OLD"


def test_consecutive_same_role_contents_are_merged_with_results_leading():
    call = ToolCallBlock("c", "read", "{}", {"thoughtSignature": "S"})
    wire = serialize(
        user_text("go"),
        assistant(call),
        user_text("also, steer left"),
        tool_result(call, "r"),
        user_text("and be brief"),
    )
    assert [c["role"] for c in wire] == ["user", "model", "user"]
    kinds = [next(iter(p)) for p in wire[2]["parts"]]
    assert kinds == ["functionResponse", "text", "text"]


def test_empty_assistant_messages_are_skipped():
    wire = serialize(user_text("a"), assistant(TextBlock("")), assistant(), user_text("b"))
    assert wire == [{"role": "user", "parts": [{"text": "a"}, {"text": "b"}]}]


def test_contents_never_end_with_a_model_turn():
    wire = serialize(user_text("q"), assistant(TextBlock("a half-finished answer")))
    assert [c["role"] for c in wire] == ["user", "model", "user"]
    assert wire[-1] == {"role": "user", "parts": [{"text": CONTINUE_PROMPT}]}
    # a request that already ends with the user is left alone
    assert serialize(user_text("q"))[-1]["role"] == "user"


async def test_body_contents_end_with_user_even_after_an_interrupted_turn():
    rec = Recorder(text_reply())
    req = Request(
        model=MODEL, system="", messages=[user_text("q"), assistant(TextBlock("partial"))]
    )
    await collect(adapter(rec), req)
    assert rec.body()["contents"][-1]["role"] == "user"


# ----------------------------------------------------------------------------- sanitize_schema


def test_sanitize_drops_unsupported_keywords_and_keeps_the_subset():
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "x",
        "type": "object",
        "title": "T",
        "description": "d",
        "additionalProperties": False,
        "properties": {
            "n": {
                "type": "integer",
                "default": 3,
                "examples": [1],
                "minimum": 0,
                "exclusiveMaximum": 10,
                "multipleOf": 2,
            },
            "s": {"type": "string", "pattern": "^a", "minLength": 1, "uniqueItems": True},
        },
        "required": ["n"],
    }
    assert sanitize_schema(schema) == {
        "type": "object",
        "description": "d",
        "properties": {
            "n": {"type": "integer", "minimum": 0},
            "s": {"type": "string", "pattern": "^a", "minLength": 1},
        },
        "required": ["n"],
    }


def test_sanitize_inlines_refs_and_stops_at_recursion():
    schema = {
        "type": "object",
        "properties": {
            "opts": {"$ref": "#/$defs/Opts", "description": "options"},
            "node": {"$ref": "#/$defs/Node"},
        },
        "$defs": {
            "Opts": {"type": "object", "properties": {"v": {"type": "boolean"}}},
            "Node": {"type": "object", "properties": {"next": {"$ref": "#/$defs/Node"}}},
        },
    }
    out = sanitize_schema(schema)
    assert out["properties"]["opts"] == {
        "type": "object",
        "description": "options",
        "properties": {"v": {"type": "boolean"}},
    }
    assert out["properties"]["node"]["properties"]["next"] == {"type": "object"}  # cycle cut
    assert "$defs" not in out and "$ref" not in json.dumps(out)


def test_sanitize_unresolvable_ref_becomes_an_opaque_object():
    out = sanitize_schema({"type": "object", "properties": {"x": {"$ref": "https://e.test/s"}}})
    assert out["properties"]["x"] == {"type": "object"}


def test_sanitize_nullable_via_anyof_and_type_lists():
    schema = {
        "type": "object",
        "properties": {
            "a": {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "maybe"},
            "b": {"type": ["integer", "null"]},
            "c": {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]},
            "d": {"oneOf": [{"type": "string"}, {"type": "number"}]},
            "e": {"type": ["string", "integer"]},
        },
    }
    props = sanitize_schema(schema)["properties"]
    assert props["a"] == {"type": "string", "nullable": True, "description": "maybe"}
    assert props["b"] == {"type": "integer", "nullable": True}
    assert props["c"] == {"anyOf": [{"type": "string"}, {"type": "integer"}], "nullable": True}
    assert props["d"] == {"anyOf": [{"type": "string"}, {"type": "number"}]}
    assert props["e"] == {"anyOf": [{"type": "string"}, {"type": "integer"}]}


def test_sanitize_const_and_enum_forms():
    schema = {
        "type": "object",
        "properties": {
            "mode": {"const": "fast"},
            "color": {"type": "string", "enum": ["red", "red", "blue"]},
            "level": {"type": "integer", "enum": [1, 2, 3], "description": "How much."},
            "flag": {"const": True},
            "opt": {"enum": ["a", None]},
        },
    }
    props = sanitize_schema(schema)["properties"]
    assert props["mode"] == {"type": "string", "enum": ["fast"]}
    assert props["color"] == {"type": "string", "enum": ["red", "blue"]}
    # Gemini enums are strings only: keep the type, say what is allowed
    assert props["level"] == {
        "type": "integer",
        "description": "How much. Allowed values: 1, 2, 3.",
    }
    assert props["flag"] == {"type": "boolean", "description": "Allowed values: true."}
    assert props["opt"] == {"type": "string", "enum": ["a"], "nullable": True}


def test_sanitize_nested_items_enum_required_and_array_defaults():
    schema = {
        "type": "object",
        "properties": {
            "edits": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "op": {"type": "string", "enum": ["add", "del"]},
                        "line": {"type": "integer", "format": "int64"},
                        "when": {"type": "string", "format": "date-time"},
                        "mail": {"type": "string", "format": "email"},
                    },
                    "required": ["op", "ghost"],  # "ghost" does not exist -> would be a 400
                },
            },
            "tags": {"type": "array"},
            "pair": {"type": "array", "items": [{"type": "string"}, {"type": "integer"}]},
            "free": {"description": "any value"},
            "blob": {"type": "object", "additionalProperties": {"type": "string"}},
        },
    }
    props = sanitize_schema(schema)["properties"]
    item = props["edits"]["items"]
    assert props["edits"]["minItems"] == 1
    assert item["required"] == ["op"]
    assert item["properties"]["op"] == {"type": "string", "enum": ["add", "del"]}
    assert item["properties"]["line"] == {"type": "integer", "format": "int64"}
    assert item["properties"]["when"] == {"type": "string", "format": "date-time"}
    assert item["properties"]["mail"] == {"type": "string"}  # unsupported format dropped
    assert props["tags"] == {"type": "array", "items": {"type": "string"}}
    assert props["pair"]["items"] == {"anyOf": [{"type": "string"}, {"type": "integer"}]}
    assert props["free"] == {"type": "string", "description": "any value"}
    assert props["blob"] == {"type": "object"}  # no empty `properties`


def test_sanitize_all_of_merges_properties_and_required():
    schema = {
        "allOf": [
            {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
            {"properties": {"b": {"type": "integer"}}, "required": ["b", "a"]},
        ],
        "description": "both",
    }
    assert sanitize_schema(schema) == {
        "type": "object",
        "description": "both",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    }


def test_sanitize_is_pure_idempotent_and_json_serialisable():
    schema = {
        "$schema": "x",
        "type": "object",
        "properties": {
            "a": {"anyOf": [{"$ref": "#/$defs/S"}, {"type": "null"}]},
            "b": {"type": "array", "items": {"$ref": "#/$defs/S"}},
            "c": {"enum": [1, 2]},
            "d": {"type": ["string", "null"], "enum": ["x", None]},
        },
        "required": ["a", "zzz"],
        "$defs": {"S": {"type": "string", "title": "S", "default": "q"}},
    }
    before = copy.deepcopy(schema)
    once = sanitize_schema(schema)
    assert schema == before  # input untouched
    assert sanitize_schema(once) == once
    json.dumps(once)
    assert sanitize_schema(None) == {"type": "string"} and sanitize_schema({}) == {"type": "string"}
