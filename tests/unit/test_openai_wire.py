"""OpenAI Chat Completions adapter against raw wire payloads."""

from __future__ import annotations

import json

import pytest

from kalash.llm.errors import ErrorCode, LLMError
from kalash.llm.openai_chat import OpenAIDialect
from kalash.llm.types import (
    Message,
    ReasoningBlock,
    ReasoningDelta,
    TextBlock,
    TextDelta,
    ToolCallBlock,
    ToolCallStarted,
    ToolSpec,
    tool_result,
)
from tests.harness.wire import (
    Recorder,
    collect,
    final,
    json_error,
    openai_adapter,
    respond,
    simple_request,
    sse_bytes,
)


def chunk(delta: dict, finish: str | None = None, **extra) -> dict:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}


async def test_text_stream_with_usage_tail():
    rec = Recorder(
        respond(
            sse_bytes(
                [
                    chunk({"role": "assistant", "content": "Hel"}),
                    chunk({"content": "lo"}),
                    chunk({}, "stop"),
                    {
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 12,
                            "completion_tokens": 3,
                            "prompt_tokens_details": {"cached_tokens": 4},
                            "completion_tokens_details": {"reasoning_tokens": 1},
                        },
                    },
                    "[DONE]",
                ]
            )
        )
    )
    events = await collect(openai_adapter(rec), simple_request())
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hel", "lo"]
    done = events[-1]
    assert done.message.text == "Hello" and done.finish == "stop"
    assert (done.usage.input_tokens, done.usage.output_tokens) == (12, 3)
    assert done.usage.cache_read_tokens == 4 and done.usage.reasoning_tokens == 1
    assert done.message.origin == "openai/m"


async def test_tool_call_fragments_are_assembled_once():
    rec = Recorder(
        respond(
            sse_bytes(
                [
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_a",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": '{"file_'},
                                }
                            ]
                        }
                    ),
                    # Many servers repeat id/name on every fragment: they must not be concatenated.
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_a",
                                    "function": {"name": "read", "arguments": 'path": "x"}'},
                                }
                            ]
                        }
                    ),
                    chunk({}, "tool_calls"),
                ]
            )
        )
    )
    events = await collect(openai_adapter(rec), simple_request())
    started = [e for e in events if isinstance(e, ToolCallStarted)]
    assert len(started) == 1 and (started[0].id, started[0].name) == ("call_a", "read")
    call = events[-1].message.tool_calls[0]
    assert (call.id, call.name, json.loads(call.arguments)) == (
        "call_a",
        "read",
        {"file_path": "x"},
    )


async def test_parallel_calls_without_index_stay_separate():
    rec = Recorder(
        respond(
            sse_bytes(
                [
                    chunk(
                        {
                            "tool_calls": [
                                {"id": "c1", "function": {"name": "read", "arguments": '{"a":1}'}}
                            ]
                        }
                    ),
                    chunk(
                        {
                            "tool_calls": [
                                {"id": "c2", "function": {"name": "glob", "arguments": '{"b":2}'}}
                            ]
                        }
                    ),
                    chunk({}, "tool_calls"),
                ]
            )
        )
    )
    done = await final(openai_adapter(rec), simple_request())
    assert [(c.id, c.name, c.arguments) for c in done.message.tool_calls] == [
        ("c1", "read", '{"a":1}'),
        ("c2", "glob", '{"b":2}'),
    ]


async def test_same_index_reused_for_a_new_call_id_starts_a_new_call():
    rec = Recorder(
        respond(
            sse_bytes(
                [
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {"name": "a", "arguments": "{}"},
                                }
                            ]
                        }
                    ),
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c2",
                                    "function": {"name": "b", "arguments": "{}"},
                                }
                            ]
                        }
                    ),
                    chunk({}, "tool_calls"),
                ]
            )
        )
    )
    done = await final(openai_adapter(rec), simple_request())
    assert [c.id for c in done.message.tool_calls] == ["c1", "c2"]


async def test_reasoning_after_text_is_kept_separate():
    rec = Recorder(
        respond(
            sse_bytes(
                [
                    chunk({"content": "answer"}),
                    chunk({"reasoning_content": "late thought"}),
                    chunk({"content": " more"}),
                    chunk({}, "stop"),
                ]
            )
        )
    )
    events = await collect(openai_adapter(rec), simple_request())
    assert any(isinstance(e, ReasoningDelta) for e in events)
    assert events[-1].message.text == "answer more"


async def test_finish_reason_length_maps_to_max_tokens():
    rec = Recorder(respond(sse_bytes([chunk({"content": "cut"}), chunk({}, "length")])))
    assert (await final(openai_adapter(rec), simple_request())).finish == "max_tokens"


async def test_stream_without_finish_reason_is_a_transport_error():
    rec = Recorder(respond(sse_bytes([chunk({"content": "partial"}), "[DONE]"])))
    with pytest.raises(LLMError) as info:
        await collect(openai_adapter(rec), simple_request())
    assert info.value.code == ErrorCode.TRANSPORT and info.value.retryable


async def test_empty_stop_is_an_empty_response_error():
    rec = Recorder(respond(sse_bytes([chunk({}, "stop")])))
    with pytest.raises(LLMError) as info:
        await collect(openai_adapter(rec), simple_request())
    assert info.value.code == ErrorCode.EMPTY_RESPONSE


async def test_reasoning_only_stop_is_an_empty_response_error():
    rec = Recorder(respond(sse_bytes([chunk({"reasoning_content": "hmm"}), chunk({}, "stop")])))
    with pytest.raises(LLMError) as info:
        await collect(openai_adapter(rec), simple_request())
    assert info.value.code == ErrorCode.EMPTY_RESPONSE


async def test_null_usage_counters_do_not_crash():
    rec = Recorder(
        respond(
            sse_bytes(
                [
                    chunk(
                        {"content": "ok"},
                        "stop",
                        usage={"prompt_tokens": None, "completion_tokens": None},
                    ),
                ]
            )
        )
    )
    done = await final(openai_adapter(rec), simple_request())
    assert done.usage.input_tokens is None and done.usage.output_tokens is None


async def test_duplicate_tool_call_ids_in_one_response_are_rejected():
    rec = Recorder(
        respond(
            sse_bytes(
                [
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "same",
                                    "function": {"name": "a", "arguments": "{}"},
                                }
                            ]
                        }
                    ),
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "same",
                                    "function": {"name": "b", "arguments": "{}"},
                                }
                            ]
                        }
                    ),
                    chunk({}, "tool_calls"),
                ]
            )
        )
    )
    with pytest.raises(LLMError, match="duplicate"):
        await collect(openai_adapter(rec), simple_request())


@pytest.mark.parametrize(
    ("status", "message", "code", "retryable"),
    [
        (401, "Incorrect API key provided", ErrorCode.AUTH, False),
        (403, "forbidden", ErrorCode.AUTH, False),
        (402, "payment required", ErrorCode.QUOTA, False),
        (429, "Rate limit reached", ErrorCode.RATE_LIMIT, True),
        (429, "You exceeded your current quota, insufficient_quota", ErrorCode.QUOTA, False),
        (500, "internal error", ErrorCode.SERVER, True),
        (503, "overloaded", ErrorCode.SERVER, True),
        (404, "model not found", ErrorCode.INVALID_REQUEST, False),
        (
            400,
            "invalid request: max_tokens 15000 is too large for tools",
            ErrorCode.INVALID_REQUEST,
            False,
        ),
        (
            400,
            "This model's maximum context length is 128000 tokens",
            ErrorCode.CONTEXT_OVERFLOW,
            False,
        ),
        (413, "Request too large", ErrorCode.CONTEXT_OVERFLOW, False),
    ],
)
async def test_http_errors_are_classified_by_status_not_by_digits_in_text(
    status, message, code, retryable
):
    rec = Recorder(json_error(status, message))
    with pytest.raises(LLMError) as info:
        await collect(openai_adapter(rec), simple_request())
    assert info.value.code == code and info.value.retryable is retryable
    assert info.value.status == status


async def test_retry_after_header_is_parsed_in_seconds_and_ms():
    for headers, expected in (
        ({"retry-after": "7"}, 7.0),
        ({"retry-after-ms": "1500"}, 1.5),
        ({"retry-after": "3600"}, 3600.0),
    ):
        rec = Recorder(json_error(429, "slow down", headers))
        with pytest.raises(LLMError) as info:
            await collect(openai_adapter(rec), simple_request())
        assert info.value.retry_after == expected


async def test_in_stream_error_object_is_raised_with_classification():
    rec = Recorder(
        respond(sse_bytes([{"error": {"message": "maximum context length exceeded", "code": 400}}]))
    )
    with pytest.raises(LLMError) as info:
        await collect(openai_adapter(rec), simple_request())
    assert info.value.code == ErrorCode.CONTEXT_OVERFLOW


async def test_unparseable_chunk_is_malformed_response():
    rec = Recorder(respond(b"data: {not json\n\n"))
    with pytest.raises(LLMError) as info:
        await collect(openai_adapter(rec), simple_request())
    assert info.value.code == ErrorCode.MALFORMED_RESPONSE


# ----------------------------------------------------------------------------- request side


async def test_request_body_uses_the_configured_token_parameter_and_tools():
    rec = Recorder(respond(sse_bytes([chunk({"content": "x"}, "stop")])))
    adapter = openai_adapter(rec, dialect=OpenAIDialect(token_param="max_completion_tokens"))
    request = simple_request(
        max_tokens=777,
        reasoning_effort="high",
        tools=[ToolSpec("read", "Read a file", {"type": "object", "properties": {}})],
    )
    await collect(adapter, request)
    body = rec.body()
    assert body["max_completion_tokens"] == 777 and "max_tokens" not in body
    assert body["reasoning_effort"] == "high" and body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["tools"][0]["function"]["name"] == "read"
    assert rec.requests[0].headers["authorization"] == "Bearer k"


async def test_no_max_tokens_is_sent_when_unset():
    rec = Recorder(respond(sse_bytes([chunk({"content": "x"}, "stop")])))
    await collect(openai_adapter(rec), simple_request())
    assert "max_tokens" not in rec.body() and "reasoning_effort" not in rec.body()


def test_history_serialization_pairs_tool_results_and_skips_empty_assistants():
    call = ToolCallBlock("c1", "read", '{"file_path": "a"}')
    messages = [
        Message("user", [TextBlock("go")]),
        Message("assistant", [ReasoningBlock("only thinking")]),  # nothing to send
        Message("assistant", [TextBlock("looking"), call], origin="openai/m"),
        tool_result(call, "file contents"),
        Message("assistant", [ToolCallBlock("c2", "read", "not json at all")]),
        tool_result(ToolCallBlock("c2", "read", ""), "Invalid arguments"),
    ]
    adapter = openai_adapter(Recorder())
    wire = adapter.serialize(simple_request().__class__("m", "sys", messages))
    roles = [m["role"] for m in wire]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "tool"]
    assert "ThinkingBlock" not in json.dumps(wire) and "ReasoningBlock" not in json.dumps(wire)
    assert wire[2]["tool_calls"][0]["function"]["arguments"] == '{"file_path": "a"}'
    assert wire[3] == {"role": "tool", "tool_call_id": "c1", "content": "file contents"}
    assert wire[4]["tool_calls"][0]["function"]["arguments"] == "{}"  # invalid replayed as {}


def test_reasoning_replay_only_for_the_producing_provider():
    dialect = OpenAIDialect(reasoning_replay_field="reasoning_content")
    adapter = openai_adapter(Recorder(), provider="sarvam", dialect=dialect)
    own = Message("assistant", [ReasoningBlock("my thoughts"), TextBlock("a")], origin="sarvam/m")
    foreign = Message(
        "assistant", [ReasoningBlock("other thoughts"), TextBlock("b")], origin="openai/o3"
    )
    wire = adapter.serialize(simple_request().__class__("m", "", [own, foreign]))
    assert wire[0]["reasoning_content"] == "my thoughts"
    assert "reasoning_content" not in wire[1]


def test_tool_result_list_content_and_empty_output_are_strings():
    call = ToolCallBlock("c1", "bash", "{}")
    adapter = openai_adapter(Recorder())
    wire = adapter.serialize(
        simple_request().__class__("m", "", [Message("assistant", [call]), tool_result(call, "")])
    )
    assert wire[1]["content"] == "(no output)"


async def test_gemini_style_extra_content_is_captured_and_replayed_to_the_same_provider():
    signature = {"google": {"thought_signature": "SIG123"}}
    rec = Recorder(
        respond(
            sse_bytes(
                [
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "extra_content": signature,
                                    "function": {"name": "read", "arguments": "{}"},
                                }
                            ]
                        }
                    ),
                    chunk({}, "tool_calls"),
                ]
            )
        )
    )
    adapter = openai_adapter(rec, provider="google")
    done = await final(adapter, simple_request())
    call = done.message.tool_calls[0]
    assert call.extra == {"extra_content": signature}
    history = [Message("user", [TextBlock("go")]), done.message, tool_result(call, "ok")]
    wire = adapter.serialize(simple_request().__class__("m", "", history))
    assert wire[1]["tool_calls"][0]["extra_content"] == signature
    foreign = openai_adapter(Recorder(), provider="openai").serialize(
        simple_request().__class__("m", "", history)
    )
    assert "extra_content" not in foreign[1]["tool_calls"][0]
