"""OpenAI Responses adapter against raw wire payloads written from the documented event shapes."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from kalash.llm.catalog import ModelInfo
from kalash.llm.errors import ErrorCode, LLMError
from kalash.llm.http import make_client
from kalash.llm.openai_responses import OpenAIResponsesAdapter
from kalash.llm.retry import RetryPolicy, stream_with_retry
from kalash.llm.types import (
    Done,
    ImageBlock,
    Message,
    ReasoningBlock,
    ReasoningDelta,
    Request,
    Retrying,
    StreamEvent,
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
    respond,
    simple_request,
    sse_bytes,
)

ENCRYPTED = "gAAAAABo-opaque-reasoning-ciphertext=="
USAGE = {
    "input_tokens": 12,
    "input_tokens_details": {"cached_tokens": 4, "cache_write_tokens": 2},
    "output_tokens": 3,
    "output_tokens_details": {"reasoning_tokens": 1},
    "total_tokens": 15,
}


def adapter_for(
    recorder: Recorder,
    *,
    provider: str = "openai",
    model: str = "m",
    info: ModelInfo | None = None,
    extra_headers: dict[str, str] | None = None,
    api_key: str | None = "k",
) -> OpenAIResponsesAdapter:
    client = make_client(transport=httpx.MockTransport(recorder))
    return OpenAIResponsesAdapter(
        provider=provider,
        model=model,
        base_url="https://x.test/v1",
        api_key=api_key,
        context_window=100_000,
        info=info,
        extra_headers=extra_headers,
        client=client,
    )


def bare() -> OpenAIResponsesAdapter:
    return adapter_for(Recorder())


def request_of(messages: list[Message], **kw: Any) -> Request:
    return Request(model="m", system="", messages=messages, **kw)


def stream_of(*events: dict[str, Any] | str) -> httpx.Response:
    return respond(sse_bytes(list(events), named=True))


# -- payload builders (shapes from the Responses streaming-events reference) ------------


def message_item(text: str = "Hello", item_id: str = "msg_1", **extra: Any) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": [], "logprobs": []}],
        **extra,
    }


def call_item(
    call_id: str = "call_1",
    name: str = "read",
    arguments: str = '{"file_path":"a.py"}',
    item_id: str = "fc_1",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "status": "completed",
        **extra,
    }


def reasoning_item(
    item_id: str = "rs_1", summary: tuple[str, ...] = ("Plan it",), **extra: Any
) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": text} for text in summary],
        "encrypted_content": ENCRYPTED,
        "status": "completed",
        **extra,
    }


def created() -> dict[str, Any]:
    return {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}}


def added(index: int, item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "response.output_item.added", "output_index": index, "item": item}


def done_item(index: int, item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "response.output_item.done", "output_index": index, "item": item}


def text_delta(index: int, delta: str, item_id: str = "msg_1") -> dict[str, Any]:
    return {
        "type": "response.output_text.delta",
        "item_id": item_id,
        "output_index": index,
        "content_index": 0,
        "delta": delta,
    }


def args_delta(index: int, delta: str, item_id: str = "fc_1") -> dict[str, Any]:
    return {
        "type": "response.function_call_arguments.delta",
        "item_id": item_id,
        "output_index": index,
        "delta": delta,
    }


def completed(output: list[dict[str, Any]], usage: Any = USAGE, **response: Any) -> dict[str, Any]:
    body = {"id": "resp_1", "status": "completed", "output": output, "usage": usage, **response}
    return {"type": "response.completed", "response": body}


def incomplete(
    output: list[dict[str, Any]], reason: str | None = "max_output_tokens", usage: Any = USAGE
) -> dict[str, Any]:
    details = {"reason": reason} if reason else None
    body = {
        "id": "resp_1",
        "status": "incomplete",
        "incomplete_details": details,
        "output": output,
        "usage": usage,
    }
    return {"type": "response.incomplete", "response": body}


def failed(code: str | None, message: str = "it broke") -> dict[str, Any]:
    error = {"code": code, "message": message}
    body = {"id": "resp_1", "status": "failed", "error": error, "output": []}
    return {"type": "response.failed", "response": body}


def message_events(text: str = "Hello", index: int = 0) -> list[dict[str, Any]]:
    item = message_item(text)
    empty = {**item, "status": "in_progress", "content": []}
    return [added(index, empty), text_delta(index, text), done_item(index, item)]


def text_events(text: str = "Hello") -> list[dict[str, Any]]:
    return [created(), *message_events(text), completed([message_item(text)])]


def call_events(index: int, item: dict[str, Any], *fragments: str) -> list[dict[str, Any]]:
    empty = {**item, "arguments": "", "status": "in_progress"}
    item_id = item["id"]
    return [
        added(index, empty),
        *[args_delta(index, piece, item_id) for piece in fragments],
        {
            "type": "response.function_call_arguments.done",
            "item_id": item_id,
            "output_index": index,
            "arguments": item["arguments"],
        },
        done_item(index, item),
    ]


async def run(events: list[dict[str, Any]], **kw: Any) -> Done:
    return await final(adapter_for(Recorder(stream_of(*events)), **kw), simple_request())


async def drain(events: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [event async for event in events]


async def collect_into(seen: list[StreamEvent], adapter: Any, request: Request) -> None:
    async for event in adapter.stream(request):
        seen.append(event)


def code_of(error: pytest.ExceptionInfo[LLMError]) -> ErrorCode:
    return error.value.code


# -- streaming: text, usage, replay -----------------------------------------------------


async def test_text_stream_with_usage_and_replay_items():
    events = await collect(
        adapter_for(Recorder(stream_of(*text_events("Hello")))), simple_request()
    )
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hello"]
    done = events[-1]
    assert isinstance(done, Done)
    assert done.message.text == "Hello" and done.finish == "stop"
    assert done.message.origin == "openai/m"
    assert done.message.replay == {"kind": "openai-responses", "items": [message_item("Hello")]}
    assert (done.usage.input_tokens, done.usage.output_tokens) == (12, 3)
    assert done.usage.cache_read_tokens == 4 and done.usage.cache_write_tokens == 2
    assert done.usage.reasoning_tokens == 1


async def test_request_is_posted_to_the_responses_endpoint_as_json():
    rec = Recorder(stream_of(*text_events()))
    await collect(adapter_for(rec), simple_request("hello there"))
    sent = rec.requests[0]
    assert (sent.method, sent.url.path) == ("POST", "/v1/responses")
    assert rec.body()["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "hello there"}]}
    ]


async def test_function_call_with_arguments_split_across_deltas_starts_once():
    item = call_item(arguments='{"file_path": "src/app.py"}')
    events = await collect(
        adapter_for(
            Recorder(
                stream_of(
                    created(),
                    *call_events(0, item, '{"file_', 'path": "src/', 'app.py"}'),
                    completed([item]),
                )
            )
        ),
        simple_request(),
    )
    started = [e for e in events if isinstance(e, ToolCallStarted)]
    assert started == [ToolCallStarted(0, "call_1", "read")]
    done = events[-1]
    assert isinstance(done, Done) and done.finish == "tool_calls"
    call = done.message.tool_calls[0]
    assert (call.id, call.name, call.arguments) == ("call_1", "read", '{"file_path": "src/app.py"}')
    assert call.extra == {"item_id": "fc_1"}


async def test_two_parallel_calls_interleaved_keep_their_own_arguments():
    first = call_item("call_a", "read", '{"a":1}', "fc_a")
    second = call_item("call_b", "glob", '{"b":2}', "fc_b")
    empty = [{**first, "arguments": ""}, {**second, "arguments": ""}]
    events = await collect(
        adapter_for(
            Recorder(
                stream_of(
                    created(),
                    added(0, empty[0]),
                    added(1, empty[1]),
                    args_delta(0, '{"a"', "fc_a"),
                    args_delta(1, '{"b"', "fc_b"),
                    args_delta(1, ":2}", "fc_b"),
                    args_delta(0, ":1}", "fc_a"),
                    done_item(1, second),
                    done_item(0, first),
                    completed([first, second]),
                )
            )
        ),
        simple_request(),
    )
    assert [(e.index, e.id, e.name) for e in events if isinstance(e, ToolCallStarted)] == [
        (0, "call_a", "read"),
        (1, "call_b", "glob"),
    ]
    done = events[-1]
    assert isinstance(done, Done)
    assert [(c.id, c.name, c.arguments) for c in done.message.tool_calls] == [
        ("call_a", "read", '{"a":1}'),
        ("call_b", "glob", '{"b":2}'),
    ]
    assert [i["call_id"] for i in done.message.replay["items"]] == ["call_a", "call_b"]


async def test_events_without_output_index_are_routed_by_item_id():
    item = call_item(arguments='{"x":1}')
    start = {"type": "response.output_item.added", "item": {**item, "arguments": ""}}
    chunk = {
        "type": "response.function_call_arguments.delta",
        "item_id": "fc_1",
        "delta": '{"x":1}',
    }
    finish = {"type": "response.output_item.done", "item": {**item, "arguments": ""}}
    done = await run([start, chunk, finish, completed([])])
    assert done.message.tool_calls[0].arguments == '{"x":1}'


async def test_call_announced_when_only_the_done_event_arrives():
    item = call_item()
    events = await collect(
        adapter_for(Recorder(stream_of(done_item(0, item), completed([item])))), simple_request()
    )
    assert [e for e in events if isinstance(e, ToolCallStarted)] == [
        ToolCallStarted(0, "call_1", "read")
    ]


# -- reasoning, replay and origins ------------------------------------------------------


async def test_reasoning_summary_streams_and_becomes_a_block_with_its_ciphertext():
    reasoning = reasoning_item(summary=("**Plan**", "**Check**"))
    still_open = {**reasoning, "summary": [], "encrypted_content": None, "status": "in_progress"}
    summary_delta = "response.reasoning_summary_text.delta"
    part_added = "response.reasoning_summary_part.added"
    events = await collect(
        adapter_for(
            Recorder(
                stream_of(
                    created(),
                    added(0, still_open),
                    {"type": part_added, "output_index": 0, "summary_index": 0},
                    {"type": summary_delta, "output_index": 0, "summary_index": 0, "delta": "**Pl"},
                    {"type": summary_delta, "output_index": 0, "summary_index": 0, "delta": "an**"},
                    {"type": part_added, "output_index": 0, "summary_index": 1},
                    {
                        "type": summary_delta,
                        "output_index": 0,
                        "summary_index": 1,
                        "delta": "**Check**",
                    },
                    done_item(0, reasoning),
                    *message_events("Done", 1),
                    completed([]),
                )
            )
        ),
        simple_request(),
    )
    shown = "".join(e.text for e in events if isinstance(e, ReasoningDelta))
    assert shown == "**Plan**\n\n**Check**"
    done = events[-1]
    assert isinstance(done, Done)
    block = done.message.content[0]
    assert isinstance(block, ReasoningBlock)
    assert block.text == "**Plan**\n\n**Check**"
    assert block.signature == ENCRYPTED and block.extra == {"id": "rs_1"}


async def test_raw_reasoning_text_events_are_shown_and_used_when_there_is_no_summary():
    reasoning = reasoning_item(summary=())
    reasoning["content"] = [{"type": "reasoning_text", "text": "raw chain"}]
    raw_delta = {"type": "response.reasoning_text.delta", "output_index": 0, "delta": "raw chain"}
    events = await collect(
        adapter_for(
            Recorder(
                stream_of(
                    raw_delta, done_item(0, reasoning), *message_events("Hello", 1), completed([])
                )
            )
        ),
        simple_request(),
    )
    assert [e.text for e in events if isinstance(e, ReasoningDelta)] == ["raw chain"]
    block = events[-1].message.content[0]
    assert isinstance(block, ReasoningBlock) and block.text == "raw chain"


async def test_reasoning_item_round_trips_byte_for_byte_to_its_own_route():
    reasoning = reasoning_item()
    call = call_item(arguments='{"file_path":  "a.py"}')  # odd spacing must survive untouched
    out = [reasoning, message_item("Looking", phase="commentary"), call]
    done = await run(
        [created(), *[done_item(i, item) for i, item in enumerate(out)], completed(out)]
    )
    block = next(b for b in done.message.content if isinstance(b, ToolCallBlock))
    history = [Message("user", [TextBlock("go")]), done.message, tool_result(block, "contents")]
    wire = bare().serialize(request_of(history))
    assert json.dumps(wire[1:4], sort_keys=False) == json.dumps(out, sort_keys=False)
    assert wire[4] == {"type": "function_call_output", "call_id": "call_1", "output": "contents"}


async def test_other_model_or_provider_rebuilds_the_turn_without_reasoning_or_ids():
    out = [reasoning_item(), message_item("Looking"), call_item()]
    done = await run([done_item(i, item) for i, item in enumerate(out)] + [completed(out)])
    call = done.message.tool_calls[0]
    history = [Message("user", [TextBlock("go")]), done.message, tool_result(call, "ok")]
    for other in (adapter_for(Recorder(), model="other"), adapter_for(Recorder(), provider="xai")):
        wire = other.serialize(request_of(history))
        assert wire[1] == {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Looking"}],
        }
        assert wire[2] == {
            "type": "function_call",
            "call_id": "call_1",
            "name": "read",
            "arguments": '{"file_path":"a.py"}',
        }
        assert "reasoning" not in json.dumps(wire) and "gAAAA" not in json.dumps(wire)


async def test_message_phase_is_preserved_in_replay():
    commentary = message_item("Checking the file first.", "msg_c", phase="commentary")
    call = call_item()
    out = [commentary, call]
    done = await run([done_item(0, commentary), *call_events(1, call), completed(out)])
    assert done.message.replay["items"][0]["phase"] == "commentary"
    block = done.message.tool_calls[0]
    history = [Message("user", [TextBlock("go")]), done.message, tool_result(block, "ok")]
    assert bare().serialize(request_of(history))[1]["phase"] == "commentary"


async def test_done_item_wins_over_the_added_copy_that_lacks_ciphertext():
    open_item = {**reasoning_item(), "encrypted_content": None, "status": "in_progress"}
    finished = reasoning_item()
    done = await run(
        [added(0, open_item), done_item(0, finished), *message_events("Hello", 1), completed([])]
    )
    assert done.message.replay["items"][0]["encrypted_content"] == ENCRYPTED


async def test_terminal_response_supplies_ciphertext_a_done_item_lacks():
    bare_done = {**reasoning_item(), "encrypted_content": None}
    msg = message_item()
    done = await run(
        [done_item(0, bare_done), done_item(1, msg), completed([reasoning_item(), msg])]
    )
    assert done.message.replay["items"][0]["encrypted_content"] == ENCRYPTED
    assert done.message.content[0].signature == ENCRYPTED  # type: ignore[union-attr]


async def test_terminal_output_alone_is_enough_when_item_events_are_missing():
    out = [reasoning_item(), message_item("Hi"), call_item(arguments="{}")]
    done = await run([created(), completed(out)])
    assert done.message.text == "Hi" and done.finish == "tool_calls"
    assert done.message.replay["items"] == out


async def test_call_arguments_fall_back_to_the_done_event_then_the_deltas():
    blank_a = call_item("call_a", arguments="", item_id="fc_a")
    blank_b = call_item("call_b", arguments="", item_id="fc_b")
    arguments_done = {
        "type": "response.function_call_arguments.done",
        "item_id": "fc_a",
        "output_index": 0,
        "arguments": '{"from":"done"}',
    }
    done = await run(
        [
            added(0, blank_a),
            added(1, blank_b),
            args_delta(1, '{"from":', "fc_b"),
            args_delta(1, '"deltas"}', "fc_b"),
            arguments_done,
            done_item(0, blank_a),
            done_item(1, blank_b),
            completed([]),
        ]
    )
    assert [c.arguments for c in done.message.tool_calls] == [
        '{"from":"done"}',
        '{"from":"deltas"}',
    ]
    assert done.message.replay["items"][0]["arguments"] == '{"from":"done"}'


async def test_refusal_is_shown_as_text_and_ends_as_a_refusal():
    refusal = {"type": "refusal", "refusal": "I can't help with that."}
    item = message_item()
    item["content"] = [refusal]
    delta = {
        "type": "response.refusal.delta",
        "output_index": 0,
        "content_index": 0,
        "delta": "I can't",
    }
    events = await collect(
        adapter_for(Recorder(stream_of(delta, done_item(0, item), completed([item])))),
        simple_request(),
    )
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["I can't"]
    done = events[-1]
    assert isinstance(done, Done)
    assert done.finish == "refusal" and done.message.text == "I can't help with that."


async def test_multiple_text_parts_become_separate_blocks_in_output_order():
    item = message_item()
    item["content"] = [
        {"type": "output_text", "text": "one", "annotations": []},
        {"type": "output_text", "text": "two", "annotations": []},
    ]
    done = await run([done_item(0, item), completed([item])])
    assert [b.text for b in done.message.content if isinstance(b, TextBlock)] == ["one", "two"]


# -- incomplete and failed responses ----------------------------------------------------


async def test_incomplete_max_output_tokens_finishes_as_max_tokens_and_keeps_the_text():
    partial = message_item("The answer is", status="incomplete")
    done = await run([done_item(0, partial), incomplete([partial])])
    assert done.finish == "max_tokens" and done.message.text == "The answer is"
    assert done.usage.output_tokens == 3


async def test_a_truncated_call_is_kept_so_the_loop_can_tell_the_model_it_was_cut_off():
    cut = call_item(arguments='{"file_path": "a.py", "content": "abc', status="incomplete")
    events = await collect(
        adapter_for(
            Recorder(
                stream_of(
                    *call_events(0, cut, '{"file_path": "a.py", "content": "abc'), incomplete([cut])
                )
            )
        ),
        simple_request(),
    )
    done = events[-1]
    assert isinstance(done, Done) and done.finish == "max_tokens"
    assert done.message.tool_calls[0].arguments == '{"file_path": "a.py", "content": "abc'
    # The next request still pairs it with a result and does not replay the broken JSON.
    history = [Message("user", [TextBlock("go")]), done.message]
    history.append(tool_result(done.message.tool_calls[0], "cut off"))
    wire = bare().serialize(request_of(history))
    assert wire[1]["arguments"] == "{}" and wire[2]["type"] == "function_call_output"


async def test_a_call_cut_off_before_it_was_named_is_dropped_on_incomplete():
    nameless = {"id": "fc_9", "type": "function_call", "status": "in_progress"}
    msg = message_item("text first", status="incomplete")
    done = await run([done_item(0, msg), done_item(1, nameless), incomplete([msg, nameless])])
    assert done.finish == "max_tokens" and done.message.tool_calls == []
    assert [i["type"] for i in done.message.replay["items"]] == ["message"]


async def test_reasoning_only_incomplete_response_is_max_tokens_and_replays_nothing():
    reasoning = reasoning_item()
    done = await run([done_item(0, reasoning), incomplete([reasoning])])
    assert done.finish == "max_tokens" and done.message.tool_calls == []
    # A reasoning item must be followed by another item, so the API would reject it alone.
    history = [
        Message("user", [TextBlock("go")]),
        done.message,
        Message("user", [TextBlock("more")]),
    ]
    assert [i.get("role") for i in bare().serialize(request_of(history))] == ["user", "user"]


@pytest.mark.parametrize(
    ("reason", "finish"),
    [
        ("max_output_tokens", "max_tokens"),
        ("content_filter", "content_filter"),
        ("max_messages", "max_tokens"),
        ("steered", "max_tokens"),
        (None, "max_tokens"),
    ],
)
async def test_incomplete_reasons_map_to_finish_reasons(reason, finish):
    done = await run([incomplete([message_item("partial")], reason)])
    assert done.finish == finish


async def test_incomplete_items_are_rebuilt_from_deltas_when_their_done_events_are_missing():
    open_msg = {**message_item(), "status": "in_progress", "content": []}
    open_call = call_item(arguments="", status="in_progress", item_id="fc_2")
    done = await run(
        [
            added(0, open_msg),
            text_delta(0, "Part"),
            text_delta(0, "ial"),
            added(1, open_call),
            args_delta(1, '{"file_path": "a', "fc_2"),
            incomplete([]),
        ]
    )
    assert done.finish == "max_tokens" and done.message.text == "Partial"
    assert done.message.tool_calls[0].arguments == '{"file_path": "a'
    assert [i["status"] for i in done.message.replay["items"]] == ["incomplete", "incomplete"]


@pytest.mark.parametrize(
    ("code", "expected", "retryable"),
    [
        ("server_error", ErrorCode.SERVER, True),
        ("server_is_overloaded", ErrorCode.SERVER, True),
        ("slow_down", ErrorCode.RATE_LIMIT, True),
        ("rate_limit_exceeded", ErrorCode.RATE_LIMIT, True),
        ("insufficient_quota", ErrorCode.QUOTA, False),
        ("credit_balance_exhausted", ErrorCode.QUOTA, False),
        ("organization_spend_limit_exceeded", ErrorCode.QUOTA, False),
        ("project_spend_limit_exceeded", ErrorCode.QUOTA, False),
        ("organization_usage_limit_exceeded", ErrorCode.QUOTA, False),
        ("context_length_exceeded", ErrorCode.CONTEXT_OVERFLOW, False),
        ("invalid_prompt", ErrorCode.INVALID_REQUEST, False),
        ("invalid_image_url", ErrorCode.INVALID_REQUEST, False),
        ("image_too_large", ErrorCode.INVALID_REQUEST, False),
        ("bio_policy", ErrorCode.INVALID_REQUEST, False),
        ("misalignment_policy_violation", ErrorCode.INVALID_REQUEST, False),
        ("invalid_api_key", ErrorCode.AUTH, False),
        ("a_code_from_the_future", ErrorCode.SERVER, True),
        (None, ErrorCode.SERVER, True),
    ],
)
async def test_failed_response_codes_map_to_typed_errors(code, expected, retryable):
    with pytest.raises(LLMError) as info:
        await run([created(), failed(code, "the API said so")])
    assert code_of(info) == expected and info.value.retryable is retryable
    assert "the API said so" in info.value.message


async def test_failed_response_with_an_overflow_message_and_unknown_code_is_an_overflow():
    with pytest.raises(LLMError) as info:
        await run([failed("mystery", "Your input exceeds the context window of this model.")])
    assert code_of(info) == ErrorCode.CONTEXT_OVERFLOW


async def test_failed_response_without_an_error_object_is_a_server_error():
    event = {"type": "response.failed", "response": {"status": "failed", "output": []}}
    with pytest.raises(LLMError) as info:
        await run([event])
    assert code_of(info) == ErrorCode.SERVER


async def test_completed_event_whose_status_says_failed_is_treated_as_failed():
    event = completed([], status="failed", error={"code": "server_error", "message": "late"})
    with pytest.raises(LLMError) as info:
        await run([event])
    assert code_of(info) == ErrorCode.SERVER and "late" in info.value.message


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        # Documented flat shape.
        (
            {"type": "error", "code": "rate_limit_exceeded", "message": "slow", "param": None},
            ErrorCode.RATE_LIMIT,
        ),
        # Shape observed on the wire: the details are nested under "error".
        (
            {
                "type": "error",
                "sequence_number": 2,
                "error": {"type": "server_error", "code": None, "message": "oops", "param": None},
            },
            ErrorCode.SERVER,
        ),
        (
            {
                "type": "error",
                "error": {"type": "invalid_request_error", "code": None, "message": "bad"},
            },
            ErrorCode.INVALID_REQUEST,
        ),
        (
            {"type": "error", "error": {"code": "context_length_exceeded", "message": "too big"}},
            ErrorCode.CONTEXT_OVERFLOW,
        ),
        ({"type": "error", "error": "plain string error"}, ErrorCode.SERVER),
    ],
)
async def test_mid_stream_error_events_in_both_shapes_are_raised_and_classified(event, expected):
    seen: list[StreamEvent] = []
    stream = stream_of(created(), added(0, message_item()), text_delta(0, "par"), event)
    with pytest.raises(LLMError) as info:
        await collect_into(seen, adapter_for(Recorder(stream)), simple_request())
    assert seen == [TextDelta("par")]  # what was shown stays visible; no Done is produced
    assert code_of(info) == expected


async def test_stream_ending_without_a_terminal_event_is_a_retryable_transport_error():
    with pytest.raises(LLMError) as info:
        await run(text_events()[:-1])
    assert code_of(info) == ErrorCode.TRANSPORT and info.value.retryable


async def test_done_sentinel_without_a_terminal_event_is_still_a_transport_error():
    rec = Recorder(respond(sse_bytes([created(), "[DONE]"])))
    with pytest.raises(LLMError) as info:
        await collect(adapter_for(rec), simple_request())
    assert code_of(info) == ErrorCode.TRANSPORT


async def test_unknown_events_and_fields_are_ignored():
    item = message_item()
    noise = [
        {"type": "response.queued", "response": {"id": "resp_1"}},
        {"type": "response.web_search_call.searching", "item_id": "ws_1", "output_index": 0},
        {"type": "response.mcp_call.in_progress", "item_id": "m", "output_index": 0},
        {"type": "response.compaction.compacting", "item_id": "c", "output_index": 0},
        {"type": "response.something_from_2027", "payload": {"a": 1}},
        json.dumps({"no_type_at_all": True}),  # raw data line: no type, no event name
        json.dumps(["not", "an", "object"]),
    ]
    done = await run(
        [created(), *noise, done_item(0, {**item, "future_field": [1]}), completed([])]
    )
    assert done.message.text == "Hello"
    assert done.message.replay["items"][0]["future_field"] == [1]


async def test_event_name_is_used_when_the_payload_has_no_type():
    item = message_item("via event name")
    payload = {"item": item, "output_index": 0}
    final_payload = {"response": {"status": "completed", "output": [item], "usage": None}}
    raw = (
        f"event: response.output_item.done\ndata: {json.dumps(payload)}\n\n"
        f"event: response.completed\ndata: {json.dumps(final_payload)}\n\n"
    ).encode()
    done = await final(adapter_for(Recorder(respond(raw))), simple_request())
    assert done.message.text == "via event name"


async def test_nothing_after_the_terminal_event_is_parsed():
    raw = sse_bytes(text_events("ok"), named=True) + b"data: {this is not json\n\n"
    done = await final(adapter_for(Recorder(respond(raw))), simple_request())
    assert done.message.text == "ok"


async def test_unparseable_chunk_is_a_malformed_response():
    with pytest.raises(LLMError) as info:
        await collect(adapter_for(Recorder(respond(b"data: {not json\n\n"))), simple_request())
    assert code_of(info) == ErrorCode.MALFORMED_RESPONSE


@pytest.mark.parametrize(
    "output",
    [
        [],
        [reasoning_item()],
        [message_item("")],
        [{"id": "ws_1", "type": "web_search_call", "status": "completed"}],
    ],
    ids=["no-output", "reasoning-only", "empty-text", "hosted-tool-only"],
)
async def test_completed_without_text_or_calls_is_a_retryable_empty_response(output):
    events = [done_item(i, item) for i, item in enumerate(output)] + [completed(output)]
    with pytest.raises(LLMError) as info:
        await run(events)
    assert code_of(info) == ErrorCode.EMPTY_RESPONSE and info.value.retryable


@pytest.mark.parametrize("arguments", ["not json at all", "[1, 2]", '{"unterminated": ', ""])
async def test_completed_call_with_malformed_arguments_is_returned_verbatim(arguments):
    item = call_item(arguments=arguments)
    done = await run([*call_events(0, item), completed([item])])
    assert done.finish == "tool_calls"
    assert done.message.tool_calls[0].arguments == arguments


async def test_duplicate_call_ids_in_one_response_are_rejected():
    out = [call_item("same", item_id="fc_1"), call_item("same", item_id="fc_2")]
    with pytest.raises(LLMError, match="duplicate") as info:
        await run([done_item(0, out[0]), done_item(1, out[1]), completed(out)])
    assert code_of(info) == ErrorCode.MALFORMED_RESPONSE


async def test_completed_call_without_a_name_is_a_malformed_response():
    item = {"id": "fc_1", "type": "function_call", "call_id": "call_1", "arguments": "{}"}
    with pytest.raises(LLMError) as info:
        await run([done_item(0, item), completed([item])])
    assert code_of(info) == ErrorCode.MALFORMED_RESPONSE


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"input_tokens": None, "output_tokens": "7", "input_tokens_details": None},
        {
            "input_tokens": True,
            "output_tokens": -1,
            "output_tokens_details": {"reasoning_tokens": None},
        },
        {"input_tokens_details": {"cached_tokens": None, "cache_write_tokens": "x"}},
    ],
)
async def test_missing_or_null_usage_counters_do_not_crash(usage):
    done = await run([*text_events()[:-1], completed([message_item()], usage)])
    assert done.usage.input_tokens is None and done.usage.output_tokens is None
    assert (done.usage.cache_read_tokens, done.usage.cache_write_tokens) == (0, 0)
    assert done.usage.reasoning_tokens == 0


async def test_a_sentinel_after_the_terminal_event_is_harmless():
    raw = sse_bytes(text_events("ok"), named=True) + sse_bytes(["[DONE]"])
    done = await final(adapter_for(Recorder(respond(raw))), simple_request())
    assert done.message.text == "ok"


async def test_a_non_string_type_field_falls_back_to_the_event_name():
    item = message_item("named")
    first = (
        f"event: response.output_item.done\ndata: {json.dumps({'type': ['x'], 'item': item})}\n\n"
    )
    last = f"event: response.completed\ndata: {json.dumps(completed([item]))}\n\n"
    done = await final(adapter_for(Recorder(respond((first + last).encode()))), simple_request())
    assert done.message.text == "named"


async def test_the_message_survives_the_session_log_and_still_replays_exactly():
    out = [reasoning_item(), message_item("Looking", phase="commentary"), call_item()]
    done = await run([done_item(i, item) for i, item in enumerate(out)] + [completed(out)])
    logged = json.loads(json.dumps(done.message.to_dict()))
    restored = Message.from_dict(logged)
    call = restored.tool_calls[0]
    history = [Message("user", [TextBlock("go")]), restored, tool_result(call, "ok")]
    assert bare().serialize(request_of(history))[1:4] == out


async def test_a_mid_stream_server_failure_is_retried_by_the_shared_retry_policy():
    thinking = {"type": "response.reasoning_summary_text.delta", "output_index": 0, "delta": "hm"}
    first = stream_of(created(), thinking, failed("server_error", "overloaded, try again"))
    rec = Recorder(first, stream_of(*text_events("recovered")))
    waits: list[float] = []

    async def no_sleep(seconds: float) -> None:
        waits.append(seconds)

    adapter = adapter_for(rec)
    events = [
        e async for e in stream_with_retry(adapter, simple_request(), RetryPolicy(), no_sleep)
    ]
    retrying = [e for e in events if isinstance(e, Retrying)]
    assert len(retrying) == 1 and retrying[0].discarded and len(waits) == 1
    assert isinstance(events[-1], Done) and events[-1].message.text == "recovered"
    assert len(rec.requests) == 2


async def test_a_quota_failure_is_not_retried():
    rec = Recorder(stream_of(created(), failed("insufficient_quota", "out of credit")))
    policy = RetryPolicy()
    with pytest.raises(LLMError) as info:
        await drain(stream_with_retry(adapter_for(rec), simple_request(), policy))
    assert code_of(info) == ErrorCode.QUOTA and len(rec.requests) == 1


async def test_aclose_closes_the_http_client():
    adapter = bare()
    await adapter.aclose()
    assert adapter._client.is_closed


# -- HTTP errors ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "message", "code", "retryable"),
    [
        (401, "Incorrect API key provided", ErrorCode.AUTH, False),
        (403, "forbidden", ErrorCode.AUTH, False),
        (429, "Rate limit reached for requests", ErrorCode.RATE_LIMIT, True),
        (429, "You exceeded your current quota, insufficient_quota", ErrorCode.QUOTA, False),
        (429, "credit_balance_exhausted", ErrorCode.QUOTA, False),
        (500, "The server had an error while processing your request", ErrorCode.SERVER, True),
        (503, "overloaded", ErrorCode.SERVER, True),
        (404, "model not found", ErrorCode.INVALID_REQUEST, False),
        (
            400,
            "Your input exceeds the context window of this model. Please adjust your input.",
            ErrorCode.CONTEXT_OVERFLOW,
            False,
        ),
    ],
)
async def test_http_errors_are_classified_by_status(status, message, code, retryable):
    with pytest.raises(LLMError) as info:
        await collect(adapter_for(Recorder(json_error(status, message))), simple_request())
    assert info.value.code == code and info.value.retryable is retryable
    assert info.value.status == status and message in info.value.message


async def test_a_rejected_parameter_surfaces_the_api_text():
    text = "Unsupported parameter: 'reasoning.summary' is not supported with this model."
    with pytest.raises(LLMError) as info:
        await collect(adapter_for(Recorder(json_error(400, text))), simple_request())
    assert code_of(info) == ErrorCode.INVALID_REQUEST and not info.value.retryable
    assert text in str(info.value)


async def test_retry_after_header_is_parsed():
    rec = Recorder(json_error(429, "slow down", {"retry-after": "7"}))
    with pytest.raises(LLMError) as info:
        await collect(adapter_for(rec), simple_request())
    assert info.value.retry_after == 7.0


@pytest.mark.parametrize(
    "code",
    [
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
    ],
)
async def test_spend_limit_429s_are_quota_errors_not_retryable_rate_limits(code):
    body = {"error": {"message": "limit reached", "type": "rate_limit_error", "code": code}}
    rec = Recorder(httpx.Response(429, json=body))
    with pytest.raises(LLMError) as info:
        await collect(adapter_for(rec), simple_request())
    assert info.value.code == ErrorCode.QUOTA and not info.value.retryable


VERIFY_SUMMARY = (
    "Your organization must be verified to generate reasoning summaries. Please go to: "
    "https://platform.openai.com/settings/organization/general and click on Verify Organization."
)


def summary_rejection() -> httpx.Response:
    error = {
        "message": VERIFY_SUMMARY,
        "type": "invalid_request_error",
        "param": "reasoning.summary",
        "code": "unsupported_value",
    }
    return httpx.Response(400, json={"error": error})


async def test_an_account_that_may_not_request_summaries_is_downgraded_once_and_remembered():
    rec = Recorder(
        summary_rejection(), stream_of(*text_events("one")), stream_of(*text_events("two"))
    )
    adapter = adapter_for(rec)
    request = simple_request(reasoning_effort="high")
    first = await final(adapter, request)
    second = await final(adapter, request)
    assert (first.message.text, second.message.text) == ("one", "two")
    sent = [rec.body(i)["reasoning"] for i in range(3)]
    assert sent == [{"effort": "high", "summary": "auto"}, {"effort": "high"}, {"effort": "high"}]


async def test_other_400s_and_repeated_rejections_are_not_retried():
    other = json_error(400, "Unsupported parameter: 'temperature' is not supported.")
    rec = Recorder(other)
    with pytest.raises(LLMError, match="temperature"):
        await collect(adapter_for(rec), simple_request(reasoning_effort="high"))
    assert len(rec.requests) == 1
    rec = Recorder(summary_rejection(), summary_rejection())
    with pytest.raises(LLMError, match="verified") as info:
        await collect(adapter_for(rec), simple_request(reasoning_effort="high"))
    assert code_of(info) == ErrorCode.INVALID_REQUEST and len(rec.requests) == 2


async def test_no_downgrade_happens_when_no_summary_was_requested():
    silent = ModelInfo("m", 1, 1, extra={"reasoning_summary": None})
    for adapter, effort in (
        (adapter_for(Recorder(summary_rejection()), info=silent), "high"),
        (adapter_for(Recorder(summary_rejection())), None),
    ):
        with pytest.raises(LLMError, match="verified"):
            await collect(adapter, simple_request(reasoning_effort=effort))


# -- request body -----------------------------------------------------------------------


def test_default_body_is_stateless_streaming_with_encrypted_reasoning():
    tools = [ToolSpec("read", "Read a file", {"type": "object", "properties": {}})]
    request = simple_request(max_tokens=777, tools=tools, session_id="sess-1")
    body = bare().build_body(request)
    assert body == {
        "model": "m",
        "instructions": "sys",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "tools": [
            {
                "type": "function",
                "name": "read",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
        "parallel_tool_calls": True,
        "stream": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "max_output_tokens": 777,
        "prompt_cache_key": "sess-1",
    }


def test_optional_fields_are_left_out_when_unset():
    body = bare().build_body(request_of([Message("user", [TextBlock("hi")])]))
    for key in (
        "instructions",
        "tools",
        "parallel_tool_calls",
        "max_output_tokens",
        "reasoning",
        "temperature",
        "prompt_cache_key",
    ):
        assert key not in body


def test_an_effort_sets_reasoning_with_a_summary_and_suppresses_temperature():
    request = simple_request(reasoning_effort="high", temperature=0.2)
    body = bare().build_body(request)
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert "temperature" not in body and "top_p" not in body


@pytest.mark.parametrize("effort", ["off", "none"])
def test_turning_reasoning_off_sends_none_only_to_models_that_accept_it(effort):
    can_disable = ModelInfo("m", 1, 1, efforts=("none", "low", "high"))
    cannot = ModelInfo("m", 1, 1, efforts=("low", "high"))
    request = simple_request(reasoning_effort=effort, temperature=0.3)
    with_none = adapter_for(Recorder(), info=can_disable).build_body(request)
    assert with_none["reasoning"] == {"effort": "none"} and with_none["temperature"] == 0.3
    for info in (cannot, None):
        assert "reasoning" not in adapter_for(Recorder(), info=info).build_body(request)


def test_sampling_is_never_sent_to_models_that_do_not_accept_it():
    request = simple_request(temperature=0.5)
    fixed = ModelInfo("m", 1, 1, sampling=False)
    free = ModelInfo("m", 1, 1, sampling=True)
    assert "temperature" not in adapter_for(Recorder(), info=fixed).build_body(request)
    assert adapter_for(Recorder(), info=free).build_body(request)["temperature"] == 0.5
    assert bare().build_body(request)["temperature"] == 0.5  # unknown model: caller's choice


def test_prompt_cache_key_is_clamped_to_the_api_limit_of_64_characters():
    body = bare().build_body(simple_request(session_id="s" * 100))
    assert body["prompt_cache_key"] == "s" * 64


def test_tools_carry_strict_only_when_the_model_info_says_so():
    tools = [ToolSpec("read", "Read", {"type": "object"})]
    request = simple_request(tools=tools)
    assert "strict" not in bare().build_body(request)["tools"][0]
    for value in (True, False):
        info = ModelInfo("m", 1, 1, extra={"strict_tools": value})
        body = adapter_for(Recorder(), info=info).build_body(request)
        assert body["tools"][0]["strict"] is value
    junk = ModelInfo("m", 1, 1, extra={"strict_tools": "yes"})
    assert "strict" not in adapter_for(Recorder(), info=junk).build_body(request)["tools"][0]


def test_the_reasoning_summary_request_can_be_changed_or_disabled_per_model():
    request = simple_request(reasoning_effort="low")
    detailed = ModelInfo("m", 1, 1, extra={"reasoning_summary": "detailed"})
    silent = ModelInfo("m", 1, 1, extra={"reasoning_summary": None})
    assert adapter_for(Recorder(), info=detailed).build_body(request)["reasoning"] == {
        "effort": "low",
        "summary": "detailed",
    }
    assert adapter_for(Recorder(), info=silent).build_body(request)["reasoning"] == {
        "effort": "low"
    }


async def test_bearer_auth_and_extra_headers_are_sent():
    rec = Recorder(stream_of(*text_events()))
    adapter = adapter_for(rec, extra_headers={"x-grok-conv-id": "abc", "OpenAI-Project": "p1"})
    await collect(adapter, simple_request())
    headers = rec.requests[0].headers
    assert headers["authorization"] == "Bearer k"
    assert headers["x-grok-conv-id"] == "abc" and headers["openai-project"] == "p1"
    assert headers["accept"] == "text/event-stream"


async def test_no_authorization_header_without_an_api_key():
    rec = Recorder(stream_of(*text_events()))
    await collect(adapter_for(rec, api_key=None), simple_request())
    assert "authorization" not in rec.requests[0].headers


# -- input serialization ----------------------------------------------------------------


def test_user_text_and_images_become_input_parts():
    message = Message("user", [TextBlock("look"), ImageBlock("image/png", "QUJD"), TextBlock("")])
    assert bare().serialize(request_of([message])) == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "look"},
                {"type": "input_image", "image_url": "data:image/png;base64,QUJD"},
            ],
        }
    ]


def test_tool_results_follow_their_calls_and_assistants_are_rebuilt_from_blocks():
    first = ToolCallBlock("c1", "read", '{"file_path": "a"}')
    second = ToolCallBlock("c2", "glob", "{}")
    messages = [
        Message("user", [TextBlock("go")]),
        Message("assistant", [ReasoningBlock("thinking"), TextBlock("looking "), first, second]),
        tool_result(second, "second result"),
        tool_result(first, ""),
        Message("assistant", [TextBlock("all "), TextBlock("done")]),
    ]
    wire = bare().serialize(request_of(messages))
    assert wire[1:] == [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "looking "}],
        },
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "read",
            "arguments": '{"file_path": "a"}',
        },
        {"type": "function_call", "call_id": "c2", "name": "glob", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c2", "output": "second result"},
        {"type": "function_call_output", "call_id": "c1", "output": "(no output)"},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "all done"}],
        },
    ]


def test_a_tool_result_with_an_image_is_sent_as_a_list_of_parts():
    call = ToolCallBlock("c1", "screenshot", "{}")
    result = Message(
        "tool",
        [TextBlock("captured"), ImageBlock("image/png", "QUJD")],
        tool_call_id="c1",
        name="screenshot",
    )
    wire = bare().serialize(request_of([Message("assistant", [call]), result]))
    assert wire[1]["output"] == [
        {"type": "input_text", "text": "captured"},
        {"type": "input_image", "image_url": "data:image/png;base64,QUJD"},
    ]


def test_empty_assistant_messages_and_non_object_arguments_are_handled():
    broken = ToolCallBlock("c2", "read", "not json at all")
    messages = [
        Message("user", [TextBlock("go")]),
        Message("assistant", [ReasoningBlock("only thinking")]),  # nothing to send
        Message("assistant", [broken]),
        tool_result(broken, "Invalid arguments"),
    ]
    wire = bare().serialize(request_of(messages))
    assert [i.get("type", i.get("role")) for i in wire] == [
        "user",
        "function_call",
        "function_call_output",
    ]
    assert wire[1]["arguments"] == "{}"


@pytest.mark.parametrize(
    ("build", "needle"),
    [
        (lambda c: [Message("assistant", [c])], "have no result"),
        (lambda c: [Message("assistant", [c]), Message("user", [TextBlock("next")])], "no result"),
        (lambda c: [tool_result(c, "orphan")], "no matching function call"),
        (
            lambda c: [Message("assistant", [c]), tool_result(c, "a"), tool_result(c, "b")],
            "repeats",
        ),
        (lambda c: [tool_result(c, "early"), Message("assistant", [c])], "no matching"),
        (
            lambda c: [
                Message("assistant", [c, ToolCallBlock("c1", "x", "{}")]),
                tool_result(c, "a"),
            ],
            "missing or repeated",
        ),
        (
            lambda c: [
                Message("assistant", [c]),
                Message("tool", [TextBlock("x")], tool_call_id=None),
            ],
            "no matching function call",
        ),
    ],
    ids=["unanswered", "interrupted", "orphan", "duplicate", "early", "repeated-id", "no-id"],
)
def test_inconsistent_history_is_an_invalid_request(build, needle):
    call = ToolCallBlock("c1", "read", "{}")
    with pytest.raises(LLMError, match=needle) as info:
        bare().serialize(request_of(build(call)))
    assert info.value.code == ErrorCode.INVALID_REQUEST and not info.value.retryable


def stored(items: list[Any], origin: str = "openai/m", kind: str = "openai-responses") -> Message:
    return Message(
        "assistant", [TextBlock("text")], origin=origin, replay={"kind": kind, "items": items}
    )


def test_replay_is_ignored_unless_both_its_kind_and_its_origin_match():
    items = [message_item("from replay")]
    for message in (stored(items, origin="openai/other"), stored(items, kind="something-else")):
        wire = bare().serialize(request_of([message]))
        assert wire == [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "text"}],
            }
        ]
    assert bare().serialize(request_of([stored(items)])) == items


def test_replayed_items_are_checked_for_results_like_any_other_call():
    message = stored([call_item("call_x")])
    with pytest.raises(LLMError, match="call_x"):
        bare().serialize(request_of([message]))
    answered = [message, Message("tool", [TextBlock("ok")], tool_call_id="call_x", name="read")]
    assert bare().serialize(request_of(answered))[1]["call_id"] == "call_x"


def test_replay_drops_only_what_the_api_would_reject():
    lost = {**reasoning_item("rs_a"), "encrypted_content": None}  # null is not accepted
    cut = {**reasoning_item("rs_b"), "status": "incomplete"}
    kept_reasoning = reasoning_item("rs_c")
    msg = message_item("hi")
    trailing = reasoning_item("rs_d")
    wire = bare().serialize(request_of([stored([lost, cut, kept_reasoning, msg, trailing])]))
    assert wire == [kept_reasoning, msg]


def test_replay_with_nothing_sendable_adds_nothing():
    wire = bare().serialize(
        request_of([stored([reasoning_item()]), Message("user", [TextBlock("x")])])
    )
    assert [i.get("role") for i in wire] == ["user"]


def test_replay_sends_broken_call_arguments_as_an_empty_object_without_touching_the_log():
    broken = call_item("call_x", arguments='{"truncated": ')
    message = stored([broken])
    answered = [message, Message("tool", [TextBlock("ok")], tool_call_id="call_x", name="read")]
    wire = bare().serialize(request_of(answered))
    assert wire[0]["arguments"] == "{}" and wire[0]["id"] == "fc_1"
    assert message.replay["items"][0]["arguments"] == '{"truncated": '


def test_serializing_does_not_change_the_messages():
    out = [reasoning_item(), message_item("a"), call_item()]
    message = stored(out)
    snapshot = json.dumps(message.to_dict(), sort_keys=True)
    bare().serialize(
        request_of(
            [message, Message("tool", [TextBlock("ok")], tool_call_id="call_1", name="read")]
        )
    )
    assert json.dumps(message.to_dict(), sort_keys=True) == snapshot
