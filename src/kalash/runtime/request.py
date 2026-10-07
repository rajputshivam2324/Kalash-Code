"""Bounded model requests, stream cancellation and usage accounting."""

from __future__ import annotations

import asyncio
import copy
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from kalash.core.budget import BudgetState, Usage
from kalash.core.errors import BudgetError
from kalash.core.events import Event, EventBus, EventType
from kalash.core.ids import generate_id
from kalash.core.privacy import (
    StreamRedactor,
    scrub_blocks,
    scrub_messages,
    scrub_metadata,
    scrub_text,
)
from kalash.models.normalize import Message, StreamError
from kalash.runtime.history import estimate_request_tokens
from kalash.runtime.serialize import serialize_blocks
from kalash.runtime.stream import StreamHandler, StreamResult

if TYPE_CHECKING:
    from kalash.models.gateway import ModelGateway
    from kalash.runtime.journal import RuntimeJournal


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
    journal: RuntimeJournal | None = None,
    recoverable_response: bool = True,
    event_bus: EventBus | None = None,
) -> StreamResult:
    """Reserve an output allowance before sending; close every stream on exit."""
    messages = copy.deepcopy(scrub_messages(messages))
    system = scrub_text(system)
    tools = copy.deepcopy([scrub_metadata(tool) for tool in tools]) if tools is not None else None
    redactor = StreamRedactor()
    visible_chunks: list[str] = []

    def safe_delta(delta: str) -> None:
        text = redactor.feed(delta)
        if text:
            visible_chunks.append(text)

    async def publish_visible() -> None:
        if not visible_chunks:
            return
        text = "".join(visible_chunks)
        visible_chunks.clear()
        if journal is not None:
            await journal.append("response_progress", {"attempt_id": attempt_id, "text": text})
        if on_text_delta is not None:
            on_text_delta(text)

    handler = StreamHandler(on_text_delta=safe_delta)
    prompt_tokens = estimate_request_tokens(system, messages, tools or [])
    output = max_output_tokens
    pricing = getattr(gateway.primary, "pricing", None)
    providers = getattr(gateway, "providers", (gateway.primary,))
    remaining_tokens = budget.remaining_tokens()
    remaining_cost = budget.remaining_cost()
    ceiling = budget.check_ceiling()
    refusal = f"Configured {ceiling.value} limit reached." if ceiling is not None else ""
    if remaining_tokens is not None and prompt_tokens + output > remaining_tokens:
        refusal = (
            f"Configured tokens limit cannot fit the next response: estimated input "
            f"{prompt_tokens:,} + output allowance {output:,}; {remaining_tokens:,} remaining. "
            "Raise budget.max_tokens or explicitly lower model.max_output_tokens."
        )
    if pricing is not None and remaining_cost is not None:
        estimated_cost = max(
            (
                Decimal(prompt_tokens) * p.pricing.input_per_mtok
                + Decimal(output) * p.pricing.output_per_mtok
            )
            / 1_000_000
            for p in providers
        )
        if estimated_cost > remaining_cost:
            refusal = (
                f"Configured cost limit cannot fit the next response: estimated reservation "
                f"{estimated_cost:.6f}; {remaining_cost:.6f} remaining. "
                "Raise budget.max_cost_usd or explicitly lower model.max_output_tokens."
            )
    if output < 1 or refusal:
        handler.feed(
            StreamError(
                error=refusal or "The requested output allowance must be positive",
                code="budget_exhausted",
                recoverable=False,
            )
        )
        return handler.result()

    budget.turns_used += 1
    attempt_id = generate_id("att_")
    if journal is not None:
        await journal.append(
            "attempt_started",
            {
                "attempt_id": attempt_id,
                "model": gateway.primary.name,
                "system": system,
                "messages": [
                    {"role": m.role.value, "content": serialize_blocks(m.content)} for m in messages
                ],
                "tools": tools,
                "output_allowance": output,
                "input_estimate": prompt_tokens,
                "recoverable_response": recoverable_response,
                "pricing": {
                    "input": str(pricing.input_per_mtok),
                    "output": str(pricing.output_per_mtok),
                }
                if pricing
                else None,
            },
            budget=budget,
        )

    physical_attempts = 0
    last_failed = False
    actual_model = getattr(gateway.primary, "name", "unknown")

    async def before_attempt(provider: Any) -> None:
        nonlocal physical_attempts, pricing, last_failed, actual_model
        provider_pricing = getattr(provider, "pricing", None)
        # Each retry/fallback is a new potentially billable dispatch. Previous
        # unreported attempts already hold their reservation at this boundary.
        if physical_attempts:
            ceiling = budget.check_ceiling()
            tokens = budget.remaining_tokens()
            cost = budget.remaining_cost()
            reserved = (
                (
                    (
                        Decimal(prompt_tokens) * provider_pricing.input_per_mtok
                        + Decimal(output) * provider_pricing.output_per_mtok
                    )
                    / 1_000_000
                )
                if provider_pricing is not None
                else Decimal(0)
            )
            if (
                ceiling
                or (tokens is not None and prompt_tokens + output > tokens)
                or (cost is not None and reserved > cost)
            ):
                raise BudgetError(
                    "Configured budget cannot admit another provider attempt; "
                    "previous unreported usage remains reserved"
                )
            budget.turns_used += 1
        pricing = provider_pricing
        previous_model = actual_model
        actual_model = provider.name
        physical_attempts += 1
        last_failed = False
        if event_bus is not None:
            if previous_model != actual_model:
                await event_bus.emit(
                    Event(
                        EventType.MODEL_FALLBACK,
                        {
                            "attempt_id": attempt_id,
                            "from_model": previous_model,
                            "model": actual_model,
                            "dispatch": physical_attempts,
                        },
                    )
                )
            await event_bus.emit(
                Event(
                    EventType.MODEL_REQUEST,
                    {
                        "attempt_id": attempt_id,
                        "dispatch": physical_attempts,
                        "dispatch_id": f"{attempt_id}:{physical_attempts}",
                        "model": provider.name,
                        "output_allowance": output,
                        "input_estimate": prompt_tokens,
                    },
                )
            )
        if journal is not None:
            await journal.append(
                "provider_dispatch",
                {
                    "attempt_id": attempt_id,
                    "sequence": physical_attempts,
                    "model": provider.name,
                    "pricing": {
                        "input": str(pricing.input_per_mtok),
                        "output": str(pricing.output_per_mtok),
                    }
                    if pricing
                    else None,
                },
                budget=budget,
            )

    async def unobserved_failure(provider: Any, error: Exception) -> None:
        nonlocal last_failed
        last_failed = True
        if event_bus is not None:
            await event_bus.emit(
                Event(
                    EventType.MODEL_RESPONSE,
                    {
                        "attempt_id": attempt_id,
                        "dispatch_id": f"{attempt_id}:{physical_attempts}",
                        "dispatch": physical_attempts,
                        "model": provider.name,
                        "usage_unknown": True,
                        "status": "failed",
                        "error": scrub_text(str(error)),
                    },
                )
            )
        budget.unknown_usage_attempts += 1
        budget.unknown_tokens_reserved += prompt_tokens + output
        if pricing is not None:
            budget.unknown_cost_reserved += (
                Decimal(prompt_tokens) * pricing.input_per_mtok
                + Decimal(output) * pricing.output_per_mtok
            ) / 1_000_000
        if journal is not None:
            await journal.append(
                "retry_uncertain",
                {
                    "attempt_id": attempt_id,
                    "sequence": physical_attempts,
                    "model": provider.name,
                    "error": scrub_text(str(error)),
                },
                budget=budget,
            )

    async def consume() -> None:
        callbacks: dict[str, Any] = {}
        from kalash.models.gateway import ModelGateway

        if isinstance(gateway, ModelGateway):
            callbacks = {
                "_before_attempt": before_attempt,
                "_unobserved_failure": unobserved_failure,
            }
        stream = gateway.stream(
            messages,
            system=system or None,
            tools=tools or None,
            max_tokens=output,
            temperature=temperature,
            **callbacks,
        )
        try:
            async for event in stream:
                handler.feed(event)
                await publish_visible()
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
    except asyncio.CancelledError:
        handler.feed(
            StreamError(error="Request cancelled; partial output retained.", code="cancelled")
        )
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise
    except BudgetError as exc:
        handler.feed(StreamError(error=str(exc), code="budget_exhausted", recoverable=False))
    except Exception as exc:
        handler.feed(StreamError(error=str(exc), code="stream_exception", recoverable=False))
    finally:
        reader.cancel()
        cancelled.cancel()
        await asyncio.gather(reader, cancelled, return_exceptions=True)
        result = handler.result()
        # Raw execution arguments remain available to dispatch, but visible and
        # durable assistant text is scrubbed as a whole as well as incrementally.
        result.content = [
            block if getattr(block, "name", None) else scrub_blocks([block])[0]
            for block in result.content
        ]
        trailing = redactor.feed("", final=True)
        if trailing:
            visible_chunks.append(trailing)
        try:
            await publish_visible()
        except Exception as error:
            handler.feed(StreamError(error=scrub_text(str(error)), code="consumer_failed"))
            result = handler.result()
            result.content = scrub_blocks(result.content)
        if result.error is not None:
            from dataclasses import replace

            result.error = replace(result.error, error=scrub_text(result.error.error))
        result.attempt_id = attempt_id
        result.model = actual_model
        refused = (
            result.error is not None
            and result.error.code == "budget_exhausted"
            and not result.usage_observed
            and not result.content
        )
        if refused and not last_failed:
            budget.turns_used -= 1
        result.usage_unknown = last_failed or (
            not refused
            and (
                not result.usage_observed
                or (result.error is not None and result.stop_reason.value == "unknown")
            )
        )
        budget.record_usage(usage_from_result(result), pricing)
        if result.usage_unknown and not last_failed:
            budget.unknown_usage_attempts += 1
            budget.unknown_tokens_reserved += max(
                0, prompt_tokens + output - usage_from_result(result).total_tokens
            )
            if pricing is not None:
                budget.unknown_cost_reserved += max(
                    Decimal(0),
                    (
                        Decimal(prompt_tokens) * pricing.input_per_mtok
                        + Decimal(output) * pricing.output_per_mtok
                    )
                    / 1_000_000,
                )
        if journal is not None:
            await journal.append(
                "attempt_settled",
                {
                    "attempt_id": attempt_id,
                    "recoverable_response": recoverable_response,
                    "model": result.model or gateway.primary.name,
                    "content": serialize_blocks(scrub_blocks(result.content)),
                    "error": scrub_text(result.error.error) if result.error else None,
                    "stop_reason": result.stop_reason.value,
                    "usage_unknown": result.usage_unknown,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                },
                budget=budget,
            )
    if event_bus is not None:
        await event_bus.emit(
            Event(
                EventType.MODEL_RESPONSE,
                {
                    "attempt_id": attempt_id,
                    "model": result.model or actual_model,
                    "dispatch": physical_attempts,
                    "dispatch_id": f"{attempt_id}:{physical_attempts}",
                    "status": "request_settled",
                    "stop_reason": result.stop_reason.value,
                    "usage_unknown": result.usage_unknown,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "cache_read_tokens": result.cache_read_tokens,
                    "cache_write_tokens": result.cache_write_tokens,
                    "reasoning_tokens": result.reasoning_tokens,
                    "error": scrub_text(result.error.error) if result.error else None,
                },
            )
        )
    return result


def usage_from_result(result: StreamResult) -> Usage:
    return Usage(
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_write_tokens=result.cache_write_tokens,
        reasoning_tokens=result.reasoning_tokens,
    )
