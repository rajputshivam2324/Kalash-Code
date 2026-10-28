"""Anthropic Messages adapter (also the DeepSeek dialect) against raw wire payloads."""

from __future__ import annotations

import json

import pytest

from kalash.llm.errors import ErrorCode, LLMError
from kalash.llm.types import (
    Message,
    ReasoningBlock,
    ReasoningDelta,
    Request,
    TextBlock,
    TextDelta,
    ToolCallBlock,
    ToolSpec,
    tool_result,
)
from tests.harness.wire import (
    Recorder,
    anthropic_adapter,
    collect,
    final,
    json_error,
    respond,
    simple_request,
    sse_bytes,
)


def stream(*events: dict) -> bytes:
    return sse_bytes(list(events), named=True)


START = {
    "type": "message_start",
    "message": {
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 50,
            "cache_creation_input_tokens": 5,
            "output_tokens": 1,
        }
    },
}


def text_block(index: int, text: str) -> list[dict]:
    return [
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        },
        {"type": "content_block_stop", "index": index},
    ]


def stop(reason: str, output: int = 7) -> dict:
    return {
        "type": "message_delta",
        "delta": {"stop_reason": reason},
        "usage": {"output_tokens": output},
    }


async def test_text_and_usage_includes_cache_tokens_in_total_input():
    rec = Recorder(
        respond(stream(START, *text_block(0, "Hello"), stop("end_turn"), {"type": "message_stop"}))
    )
    events = await collect(anthropic_adapter(rec), simple_request())
    done = events[-1]
    assert done.message.text == "Hello" and done.finish == "stop"
    assert done.usage.input_tokens == 65  # 10 fresh + 50 cache read + 5 cache write
    assert done.usage.cache_read_tokens == 50 and done.usage.cache_write_tokens == 5
    assert done.usage.output_tokens == 7
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hello"]


async def test_tool_use_arguments_assembled_from_partial_json():
    rec = Recorder(
        respond(
            stream(
                START,
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"file_'},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": 'path": "a.py"}'},
                },
                {"type": "content_block_stop", "index": 0},
                stop("tool_use"),
            )
        )
    )
    done = await final(anthropic_adapter(rec), simple_request())
    call = done.message.tool_calls[0]
    assert (call.id, call.name, json.loads(call.arguments)) == (
        "toolu_1",
        "read",
        {"file_path": "a.py"},
    )
    assert done.finish == "tool_calls"


async def test_tool_use_without_arguments_defaults_to_empty_object():
    rec = Recorder(
        respond(
            stream(
                START,
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": "t", "name": "glob", "input": {}},
                },
                {"type": "content_block_stop", "index": 0},
                stop("tool_use"),
            )
        )
    )
    done = await final(anthropic_adapter(rec), simple_request())
    assert done.message.tool_calls[0].arguments == "{}"


async def test_thinking_signature_and_redacted_thinking_are_preserved():
    rec = Recorder(
        respond(
            stream(
                START,
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "hmm"},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "signature_delta", "signature": "SIG"},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "redacted_thinking", "data": "OPAQUE"},
                },
                {"type": "content_block_stop", "index": 1},
                *text_block(2, "answer"),
                stop("end_turn"),
            )
        )
    )
    events = await collect(anthropic_adapter(rec), simple_request())
    assert any(isinstance(e, ReasoningDelta) for e in events)
    blocks = events[-1].message.content
    assert isinstance(blocks[0], ReasoningBlock) and blocks[0].signature == "SIG"
    assert isinstance(blocks[1], ReasoningBlock) and blocks[1].redacted == "OPAQUE"


async def test_straggler_delta_after_block_stop_is_ignored():
    rec = Recorder(
        respond(
            stream(
                START,
                *text_block(0, "ok"),
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "LATE"},
                },
                {
                    "type": "content_block_delta",
                    "index": 9,
                    "delta": {"type": "text_delta", "text": "UNKNOWN"},
                },
                stop("end_turn"),
            )
        )
    )
    assert (await final(anthropic_adapter(rec), simple_request())).message.text == "ok"


async def test_duplicate_block_start_is_malformed():
    rec = Recorder(
        respond(stream(START, *text_block(0, "a"), *text_block(0, "b"), stop("end_turn")))
    )
    with pytest.raises(LLMError) as info:
        await collect(anthropic_adapter(rec), simple_request())
    assert info.value.code == ErrorCode.MALFORMED_RESPONSE


@pytest.mark.parametrize(
    ("reason", "finish"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "max_tokens"),
        ("model_context_window_exceeded", "max_tokens"),
        ("refusal", "refusal"),
    ],
)
async def test_stop_reasons(reason, finish):
    rec = Recorder(respond(stream(START, *text_block(0, "x"), stop(reason))))
    assert (await final(anthropic_adapter(rec), simple_request())).finish == finish


async def test_unclosed_tool_block_with_normal_stop_is_a_transport_error():
    rec = Recorder(
        respond(
            stream(
                START,
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": "t", "name": "read", "input": {}},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"a'},
                },
                stop("tool_use"),
            )
        )
    )
    with pytest.raises(LLMError) as info:
        await collect(anthropic_adapter(rec), simple_request())
    assert info.value.code == ErrorCode.TRANSPORT


async def test_stream_ending_without_stop_reason_is_a_transport_error():
    rec = Recorder(respond(stream(START, *text_block(0, "partial"))))
    with pytest.raises(LLMError) as info:
        await collect(anthropic_adapter(rec), simple_request())
    assert info.value.code == ErrorCode.TRANSPORT


async def test_overloaded_error_event_is_a_retryable_server_error():
    rec = Recorder(
        respond(
            stream(
                START,
                {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
            )
        )
    )
    with pytest.raises(LLMError) as info:
        await collect(anthropic_adapter(rec), simple_request())
    assert info.value.code == ErrorCode.SERVER and info.value.retryable


async def test_prompt_too_long_is_context_overflow():
    rec = Recorder(json_error(400, "prompt is too long: 250000 tokens > 200000 maximum"))
    with pytest.raises(LLMError) as info:
        await collect(anthropic_adapter(rec), simple_request())
    assert info.value.code == ErrorCode.CONTEXT_OVERFLOW


# ----------------------------------------------------------------------------- request side


async def test_anthropic_request_has_cache_breakpoints_and_headers():
    rec = Recorder(respond(stream(START, *text_block(0, "x"), stop("end_turn"))))
    request = simple_request(tools=[ToolSpec("read", "d", {"type": "object", "properties": {}})])
    await collect(anthropic_adapter(rec), request)
    body = rec.body()
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["max_tokens"] == 4096 and body["stream"] is True
    headers = rec.requests[0].headers
    assert headers["x-api-key"] == "k" and headers["anthropic-version"] == "2023-06-01"


async def test_older_models_use_budgeted_thinking_without_temperature():
    rec = Recorder(respond(stream(START, *text_block(0, "x"), stop("end_turn"))))
    request = simple_request(reasoning_effort="high", max_tokens=40_000, temperature=0.3)
    await collect(anthropic_adapter(rec, model="claude-sonnet-4-5"), request)
    body = rec.body()
    assert body["thinking"]["type"] == "enabled" and body["thinking"]["budget_tokens"] >= 1024
    assert body["thinking"]["budget_tokens"] < body["max_tokens"]
    assert "temperature" not in body and "output_config" not in body


async def test_current_models_use_adaptive_thinking_with_an_effort_never_a_budget():
    from kalash.llm.catalog import lookup

    rec = Recorder(
        respond(stream(START, *text_block(0, "x"), stop("end_turn"))),
        respond(stream(START, *text_block(0, "x"), stop("end_turn"))),
    )
    info = lookup("anthropic", "claude-opus-5-5")
    adapter = anthropic_adapter(rec, model="claude-opus-5-5", info=info)
    await collect(adapter, simple_request(reasoning_effort="xhigh", temperature=0.2))
    body = rec.body(0)
    assert body["output_config"] == {"effort": "xhigh"}
    assert "thinking" not in body, "budget_tokens/enabled is a 400 on Claude 4.7+"
    assert "temperature" not in body, "sampling parameters are rejected on current models"
    await collect(adapter, simple_request(reasoning_effort="off"))
    assert "thinking" not in rec.body(1), "Opus 5.5 cannot disable thinking; nothing is sent"


async def test_haiku_can_disable_thinking_and_effort_is_clamped_to_supported_values():
    from kalash.llm.catalog import lookup

    rec = Recorder(
        respond(stream(START, *text_block(0, "x"), stop("end_turn"))),
        respond(stream(START, *text_block(0, "x"), stop("end_turn"))),
    )
    adapter = anthropic_adapter(
        rec, model="claude-haiku-5-5", info=lookup("anthropic", "claude-haiku-5-5")
    )
    await collect(adapter, simple_request(reasoning_effort="off"))
    assert rec.body(0)["thinking"] == {"type": "disabled"}
    await collect(adapter, simple_request(reasoning_effort="minimal"))
    assert rec.body(1)["output_config"] == {"effort": "low"}


async def test_max_tokens_is_capped_to_the_model_limit_and_version_header_is_configurable():
    from kalash.llm.catalog import lookup

    rec = Recorder(respond(stream(START, *text_block(0, "x"), stop("end_turn"))))
    adapter = anthropic_adapter(
        rec,
        model="claude-opus-5-5",
        info=lookup("anthropic", "claude-opus-5-5"),
        version="2026-10-21",
    )
    await collect(adapter, simple_request(max_tokens=999_999))
    assert rec.body()["max_tokens"] == 128_000
    assert rec.requests[0].headers["anthropic-version"] == "2026-10-21"


def test_tool_results_merge_into_one_user_message_leading_the_content():
    c1, c2 = ToolCallBlock("a", "read", "{}"), ToolCallBlock("b", "read", "{}")
    messages = [
        Message("user", [TextBlock("go")]),
        Message("assistant", [c1, c2], origin="anthropic/m"),
        tool_result(c1, "one"),
        tool_result(c2, "two", is_error=True),
        Message("user", [TextBlock("also this")]),
    ]
    adapter = anthropic_adapter(Recorder())
    wire = adapter.serialize(messages)
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    kinds = [b["type"] for b in wire[2]["content"]]
    assert kinds == ["tool_result", "tool_result", "text"]
    assert wire[2]["content"][1]["is_error"] is True


def test_thinking_is_replayed_only_inside_the_current_tool_loop_for_the_same_model():
    call = ToolCallBlock("c", "read", "{}")
    signed = ReasoningBlock("t", signature="S")
    old_turn = Message("assistant", [signed, TextBlock("old")], origin="anthropic/m")
    in_loop = Message("assistant", [signed, call], origin="anthropic/m")
    foreign = Message("assistant", [signed, call], origin="deepseek/x")
    unsigned = Message("assistant", [ReasoningBlock("t"), call], origin="anthropic/m")
    redacted = Message(
        "assistant", [ReasoningBlock("", redacted="OPQ"), call], origin="anthropic/m"
    )
    adapter = anthropic_adapter(Recorder())

    def first_assistant_types(history):
        wire = adapter.serialize(history)
        return [b["type"] for b in [m for m in wire if m["role"] == "assistant"][-1]["content"]]

    base = [Message("user", [TextBlock("q1")]), old_turn, Message("user", [TextBlock("q2")])]
    wire = adapter.serialize([*base, in_loop, tool_result(call, "ok")])
    assistants = [m["content"] for m in wire if m["role"] == "assistant"]
    assert [b["type"] for b in assistants[0]] == ["text"], "previous turns' thinking is dropped"
    assert [b["type"] for b in assistants[1]] == ["thinking", "tool_use"]
    assert first_assistant_types([*base, foreign, tool_result(call, "ok")]) == ["tool_use"]
    assert first_assistant_types([*base, unsigned, tool_result(call, "ok")]) == ["tool_use"]
    assert first_assistant_types([*base, redacted, tool_result(call, "ok")]) == [
        "redacted_thinking",
        "tool_use",
    ]


async def test_deepseek_dialect_controls_thinking_and_skips_cache_control():
    rec = Recorder(
        respond(stream(START, *text_block(0, "x"), stop("end_turn"))),
        respond(stream(START, *text_block(0, "x"), stop("end_turn"))),
    )
    adapter = anthropic_adapter(rec, provider="deepseek", dialect="deepseek")
    await collect(adapter, simple_request(reasoning_effort="medium"))
    await collect(adapter, simple_request(reasoning_effort="off"))
    first, second = rec.body(0), rec.body(1)
    assert first["thinking"] == {"type": "enabled"} and first["output_config"]["effort"] == "high"
    assert second["thinking"] == {"type": "disabled"} and "output_config" not in second
    assert "cache_control" not in json.dumps(first)
    assert rec.requests[0].headers["authorization"] == "Bearer k"


def test_deepseek_replays_reasoning_as_thinking_blocks():
    msg = Message(
        "assistant", [ReasoningBlock("deep thoughts"), TextBlock("a")], origin="deepseek/v4"
    )
    wire = anthropic_adapter(
        Recorder(), provider="deepseek", model="v4", dialect="deepseek"
    ).serialize([Message("user", [TextBlock("q")]), msg])
    assert wire[1]["content"][0] == {"type": "thinking", "thinking": "deep thoughts"}


def test_request_dataclass_roundtrips_messages():
    request = Request(model="m", system="", messages=[Message("user", [TextBlock("x")])])
    assert Message.from_dict(request.messages[0].to_dict()) == request.messages[0]
