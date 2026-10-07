"""Local SDK wire reproductions of capped and interrupted provider responses."""

import asyncio
import json

import httpx
import openai
import pytest
from evals.compact_budget import MeteredProvider, SuiteLedger

from kalash.models.gateway import ModelGateway, RetryConfig
from kalash.models.normalize import BlockStop, MessageStop, StopReason, UsageUpdate
from kalash.models.providers.sarvam import SarvamProvider
from kalash.runtime.loop import TerminationReason
from tests.unit.test_agent_loop import (
    AutoApprover,
    FakeGateway,
    make_host,
    make_loop,
    send,
    tool_turn,
)


def wire_chunk(*, delta=None, finish_reason=None, usage=None, choices=True):
    return {
        "id": "local-response",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "sarvam-105b",
        "choices": (
            [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}] if choices else []
        ),
        "usage": usage,
    }


def wire_usage():
    return {
        "prompt_tokens": 100,
        "completion_tokens": 30,
        "total_tokens": 130,
        "prompt_tokens_details": {"cached_tokens": 80},
        "completion_tokens_details": {"reasoning_tokens": 20},
    }


def sse_payload(chunks):
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks).encode()


def wire_provider(chunks, *, continuation=None, body=None):
    requests = []

    def server(request):
        requests.append(json.loads(request.content))
        if body is not None:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)
        response = continuation if continuation is not None and len(requests) > 1 else chunks
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_payload(response) + b"data: [DONE]\n\n",
        )

    provider = SarvamProvider(api_key="local-fixture", max_tokens=4096)
    provider._client = openai.AsyncOpenAI(  # noqa: SLF001 -- Inject a transport without live calls.
        api_key="local-fixture",
        base_url="https://local.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
    )
    return provider, requests


@pytest.mark.parametrize("finish_reason", [None, "length", "content_filter", "unrecognized"])
@pytest.mark.parametrize("arguments", ['{"path":"a.txt","content":"new"}', '{"path":'])
async def test_incomplete_wire_response_never_executes_tool(tmp_path, finish_reason, arguments):
    provider, requests = wire_provider(
        [
            wire_chunk(delta={"reasoning_content": "inspect source"}),
            wire_chunk(
                delta={
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "write-call",
                            "type": "function",
                            "function": {"name": "write", "arguments": arguments},
                        }
                    ]
                },
                finish_reason=finish_reason,
            ),
            wire_chunk(usage=wire_usage(), choices=False),
        ]
    )
    gateway = ModelGateway(provider, retry_config=RetryConfig(max_attempts=1))
    loop = make_loop(gateway, make_host(tmp_path, ui=AutoApprover()))
    try:
        result = await send(loop, "write a file")
    finally:
        await gateway.close()
    assert not (tmp_path / "a.txt").exists()
    assert loop.budget.tool_calls_used == 0
    assert result.termination_reason == TerminationReason.ERROR
    assert result.error
    if finish_reason == "length":
        assert "truncated" in result.error
        assert "generation token limit" in result.error
        assert requests[0]["max_tokens"] == loop.max_output_tokens
    assert len(requests) == 1
    assert loop.budget.tokens_used == 130
    assert gateway.total_usage.total_tokens == 130


async def test_eof_without_finish_reason_is_not_success(tmp_path):
    provider, requests = wire_provider(
        [wire_chunk(delta={"content": "unfinished answer"}, usage=wire_usage())]
    )
    gateway = ModelGateway(provider, retry_config=RetryConfig(max_attempts=1))
    loop = make_loop(gateway, make_host(tmp_path))
    try:
        result = await send(loop, "finish the task")
    finally:
        await gateway.close()
    assert result.termination_reason == TerminationReason.ERROR
    assert "finish" in result.error.lower()
    assert len(requests) == 1
    assert loop.budget.tokens_used == 130


async def test_choice_chunk_usage_is_charged(tmp_path):
    provider, _ = wire_provider(
        [wire_chunk(delta={"content": "done"}, finish_reason="stop", usage=wire_usage())]
    )
    gateway = ModelGateway(provider, retry_config=RetryConfig(max_attempts=1))
    loop = make_loop(gateway, make_host(tmp_path))
    try:
        result = await send(loop, "answer")
    finally:
        await gateway.close()
    assert result.termination_reason == TerminationReason.NO_TOOL_CALLS
    assert loop.budget.tokens_used == 130
    assert gateway.total_usage.total_tokens == 130


async def test_repeated_wire_usage_snapshots_are_not_added_twice(tmp_path):
    provider, _ = wire_provider(
        [
            wire_chunk(delta={"reasoning_content": "inspect"}, usage=wire_usage()),
            wire_chunk(delta={"content": "done"}, finish_reason="stop"),
            wire_chunk(usage=wire_usage(), choices=False),
        ]
    )
    gateway = ModelGateway(provider, retry_config=RetryConfig(max_attempts=1))
    loop = make_loop(gateway, make_host(tmp_path))
    try:
        result = await send(loop, "answer")
    finally:
        await gateway.close()
    assert result.termination_reason == TerminationReason.NO_TOOL_CALLS
    assert gateway.total_usage.total_tokens == loop.budget.tokens_used == 130
    assert gateway.call_count == 1


async def test_partial_usage_updates_retain_input_and_cache_counts(tmp_path):
    class Provider:
        name = "local/partial-usage"

        async def stream(self, *_args, **_kwargs):
            yield UsageUpdate(input_tokens=100, cache_read_tokens=80, cache_write_tokens=5)
            yield UsageUpdate(output_tokens=10)
            yield UsageUpdate(output_tokens=30, reasoning_tokens=20)
            yield MessageStop(StopReason.END_TURN)

    gateway = ModelGateway(Provider(), retry_config=RetryConfig(max_attempts=1))
    loop = make_loop(gateway, make_host(tmp_path))
    result = await send(loop, "answer")
    assert result.termination_reason == TerminationReason.NO_TOOL_CALLS
    assert loop.budget.tokens_used == gateway.total_usage.total_tokens == 135
    assert gateway.total_usage.cache_read_tokens == 80
    assert gateway.total_usage.reasoning_tokens == 20


async def test_cancellation_after_usage_preserves_charge_and_closes_stream(tmp_path):
    started, closed = asyncio.Event(), asyncio.Event()

    class Provider:
        name = "local/cancelled"
        default_output_tokens = 4096

        def serialize_messages(self, _messages, _system):
            return []

        async def stream(self, *_args, **_kwargs):
            try:
                yield UsageUpdate(input_tokens=100, output_tokens=10)
                yield UsageUpdate(output_tokens=30)
                started.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

    ledger = SuiteLedger(tmp_path / "ledger.json")
    gateway = ModelGateway(
        MeteredProvider(Provider(), ledger, "cancelled"),
        retry_config=RetryConfig(max_attempts=1),
    )
    loop = make_loop(gateway, make_host(tmp_path))
    task = asyncio.create_task(send(loop, "answer"))
    await asyncio.wait_for(started.wait(), 1)
    loop.cancel()
    result = await asyncio.wait_for(task, 1)
    assert result.termination_reason == TerminationReason.USER_INTERRUPT
    assert closed.is_set()
    assert loop.budget.tokens_used == gateway.total_usage.total_tokens == 130
    attempt = ledger.attempts[0]
    assert attempt["status"] == "incomplete"
    assert attempt["usage"]["input_tokens"] == 100
    assert attempt["charged_tokens"] == attempt["reserved_tokens"]


@pytest.mark.parametrize("cancel", [False, True])
async def test_sdk_interruption_retains_usage_and_never_replays_mutation(tmp_path, cancel):
    started = asyncio.Event()

    class Body(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield sse_payload(
                [
                    wire_chunk(
                        delta={
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "write-call",
                                    "type": "function",
                                    "function": {
                                        "name": "write",
                                        "arguments": '{"path":"a.txt","content":"new"}',
                                    },
                                }
                            ]
                        },
                        usage=wire_usage(),
                    )
                ]
            )
            started.set()
            if cancel:
                await asyncio.Event().wait()
            raise httpx.ReadError("local SSE connection interrupted")

        async def aclose(self):
            self.closed = True

    body = Body()
    provider, requests = wire_provider([], body=body)
    ledger = SuiteLedger(tmp_path / "ledger.json")
    # Use the default retry policy to prove that emitted content prevents replay.
    gateway = ModelGateway(MeteredProvider(provider, ledger, "interrupted"))
    loop = make_loop(gateway, make_host(tmp_path, ui=AutoApprover()))
    try:
        task = asyncio.create_task(send(loop, "write"))
        await asyncio.wait_for(started.wait(), 1)
        if cancel:
            loop.cancel()
        result = await asyncio.wait_for(task, 1)
    finally:
        await gateway.close()
    expected = TerminationReason.USER_INTERRUPT if cancel else TerminationReason.ERROR
    assert result.termination_reason == expected
    assert body.closed
    assert len(requests) == 1
    assert not (tmp_path / "a.txt").exists()
    assert loop.budget.tool_calls_used == 0
    assert loop.budget.tokens_used == gateway.total_usage.total_tokens == 130
    assert ledger.attempts[0]["charged_tokens"] == ledger.attempts[0]["reserved_tokens"]


async def test_reasoning_only_generation_cap_reports_actual_cause(tmp_path):
    provider, requests = wire_provider(
        [
            wire_chunk(delta={"reasoning_content": "unfinished reasoning"}),
            wire_chunk(finish_reason="length"),
            wire_chunk(usage=wire_usage(), choices=False),
        ]
    )
    gateway = ModelGateway(provider)
    loop = make_loop(gateway, make_host(tmp_path))
    try:
        result = await send(loop, "repair")
    finally:
        await gateway.close()
    assert result.termination_reason == TerminationReason.ERROR
    assert "generation token limit" in result.error
    assert len(requests) == 1
    assert requests[0]["reasoning_effort"] == "high"
    assert loop.budget.tokens_used == 130


async def test_complete_fragmented_wire_tool_call_executes_once(tmp_path):
    provider, requests = wire_provider(
        [
            wire_chunk(
                delta={
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "write-call",
                            "type": "function",
                            "function": {"name": "write", "arguments": '{"path":"a.txt",'},
                        }
                    ]
                }
            ),
            wire_chunk(
                delta={"tool_calls": [{"index": 0, "function": {"arguments": '"content":"new"}'}}]},
                finish_reason="tool_calls",
            ),
            wire_chunk(usage=wire_usage(), choices=False),
        ],
        continuation=[
            wire_chunk(delta={"content": "done"}, finish_reason="stop"),
            wire_chunk(usage=wire_usage(), choices=False),
        ],
    )
    gateway = ModelGateway(provider)
    loop = make_loop(gateway, make_host(tmp_path, ui=AutoApprover()))
    try:
        result = await send(loop, "write a file")
    finally:
        await gateway.close()
    assert result.termination_reason == TerminationReason.NO_TOOL_CALLS
    assert (tmp_path / "a.txt").read_text() == "new"
    assert loop.budget.tool_calls_used == 1
    assert loop.budget.tokens_used == gateway.total_usage.total_tokens == 260
    assert len(requests) == 2
    assert requests[1]["messages"][-1]["tool_call_id"] == "write-call"


async def test_terminal_reason_does_not_authorize_unclosed_tool_block(tmp_path):
    events = [
        event
        for event in tool_turn("write", {"path": "a.txt", "content": "new"})
        if not isinstance(event, BlockStop)
    ]
    gateway = FakeGateway([events])
    loop = make_loop(gateway, make_host(tmp_path, ui=AutoApprover()))
    result = await send(loop, "write")
    assert result.termination_reason == TerminationReason.ERROR
    assert "unclosed content blocks" in result.error
    assert not (tmp_path / "a.txt").exists()
    assert loop.budget.tool_calls_used == 0
    assert loop.budget.tokens_used == 120
    assert len(gateway.calls) == 1
