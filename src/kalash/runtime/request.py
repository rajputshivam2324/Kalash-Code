"""Bounded model requests, stream cancellation and usage accounting."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from kalash.core.budget import BudgetState, Usage
from kalash.models.gateway import ModelGateway
from kalash.models.normalize import Message, StreamError
from kalash.runtime.history import estimate_request_tokens
from kalash.runtime.stream import StreamHandler, StreamResult


async def request(
    gateway: ModelGateway,
    budget: BudgetState,
    cancel_event: asyncio.Event,
    messages: list[Message],
    *,
    system: str,
    tools: list[dict[str, Any]] | None,
    max_output_tokens: int,
    temperature: float | None,
    on_text_delta: Any | None,
) -> StreamResult:
    """Reserve an output allowance before sending; close every stream on exit."""
    handler = StreamHandler(on_text_delta=on_text_delta)
    prompt_tokens = estimate_request_tokens(system, messages, tools or [])
    output = min(max_output_tokens, budget.remaining_tokens() - prompt_tokens)
    pricing = getattr(gateway.primary, "pricing", None)
    if pricing is not None:
        remaining = (
            budget.remaining_cost() - Decimal(prompt_tokens) * pricing.input_per_mtok / 1_000_000
        )
        if remaining <= 0:
            output = 0
        elif pricing.output_per_mtok > 0:
            output = min(output, int(remaining * 1_000_000 / pricing.output_per_mtok))
    if output < 1 or budget.check_ceiling() is not None:
        handler.feed(
            StreamError(
                error="Insufficient run budget for the next request",
                code="budget_exhausted",
                recoverable=False,
            )
        )
        return handler.result()

    budget.turns_used += 1

    async def consume() -> None:
        stream = gateway.stream(
            messages,
            system=system or None,
            tools=tools or None,
            max_tokens=output,
            temperature=temperature,
        )
        try:
            async for event in stream:
                handler.feed(event)
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()

    reader = asyncio.create_task(consume())
    cancelled = asyncio.create_task(cancel_event.wait())
    try:
        done, _ = await asyncio.wait({reader, cancelled}, return_when=asyncio.FIRST_COMPLETED)
        if cancelled in done and cancel_event.is_set():
            raise asyncio.CancelledError
        await reader
    except Exception as exc:
        handler.feed(StreamError(error=str(exc), code="stream_exception", recoverable=False))
    finally:
        reader.cancel()
        cancelled.cancel()
        await asyncio.gather(reader, cancelled, return_exceptions=True)
        result = handler.result()
        budget.record_usage(usage_from_result(result), pricing)
    return result


def usage_from_result(result: StreamResult) -> Usage:
    return Usage(
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_write_tokens=result.cache_write_tokens,
        reasoning_tokens=result.reasoning_tokens,
    )
