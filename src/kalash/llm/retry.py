"""Bounded retry of transient provider failures that happen before any output is streamed."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from kalash.llm.errors import ErrorCode, LLMError
from kalash.llm.http import Adapter
from kalash.llm.types import AttemptUsage, Request, Retrying, StreamEvent


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 5
    initial_delay: float = 0.5
    max_delay: float = 10.0
    max_retry_after: float = 60.0  # longer server-requested waits fail instead of hanging
    jitter: float = 0.1
    # An empty completion is retried this many times, not ``max_retries``: asking the same
    # question again rarely changes the answer (seen live), so the caller should change the
    # question instead of waiting through five backoffs.
    max_empty_retries: int = 1

    def delay(self, attempt: int, error: LLMError) -> float | None:
        """Seconds before retry ``attempt`` (1-based), or ``None`` to give up."""
        if not error.retryable or attempt > self.max_retries:
            return None
        if error.retry_after is not None:
            return error.retry_after if error.retry_after <= self.max_retry_after else None
        base = min(self.max_delay, self.initial_delay * 2 ** (attempt - 1))
        return float(base * (1 + random.uniform(-self.jitter, self.jitter)))


MID_STREAM_RETRYABLE = frozenset({ErrorCode.TRANSPORT, ErrorCode.TIMEOUT, ErrorCode.SERVER})

Sleep = Callable[[float], Awaitable[None]]


async def stream_with_retry(
    adapter: Adapter,
    request: Request,
    policy: RetryPolicy,
    sleep: Sleep = asyncio.sleep,
) -> AsyncIterator[StreamEvent]:
    """Yield the adapter's events; retry transparently only while nothing has been yielded."""
    attempt = 0
    empties = 0
    while True:
        produced = False
        try:
            async for event in adapter.stream(request):
                produced = True
                yield event
            return
        except LLMError as error:
            if error.usage is not None:
                yield AttemptUsage(error.usage, str(error))
            attempt += 1
            if error.code == ErrorCode.EMPTY_RESPONSE:
                empties += 1
                if empties > policy.max_empty_retries:
                    raise
            # After output began, only connection-level failures are safe to retry: nothing
            # has been committed (the caller commits a response only on ``Done``).
            if produced and error.code not in MID_STREAM_RETRYABLE:
                raise
            wait = policy.delay(attempt, error)
            if wait is None:
                raise
            yield Retrying(attempt, wait, str(error), discarded=produced)
            await sleep(wait)
