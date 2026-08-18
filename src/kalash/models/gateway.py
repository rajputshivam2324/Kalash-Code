"""Unified model gateway — single interface for all LLM providers.

The gateway handles:
- Provider routing (primary + fallback chain)
- Retry with jittered exponential backoff (4 attempts, 60s budget)
- Error classification (transient vs permanent)
- Usage tracking and budget enforcement
- Streaming normalization
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from kalash.core.budget import Pricing, Usage
from kalash.core.errors import (
    ModelAllFailedError,
    ModelContextExceededError,
    ModelError,
)

from .normalize import (
    ContentBlock,
    Message,
    ModelCapabilities,
    ModelResponse,
    StopReason,
    StreamError,
    StreamEvent,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------


class ProviderProtocol(Protocol):
    """Interface that all provider adapters must satisfy."""

    @property
    def name(self) -> str: ...

    @property
    def capabilities(self) -> ModelCapabilities: ...

    @property
    def pricing(self) -> Pricing: ...

    async def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        stop_sequences: list[str] | None = None,
        **kwargs: Any,
    ) -> ModelResponse: ...

    async def stream(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        stop_sequences: list[str] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]: ...

    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


class ErrorKind:
    """Classify provider errors for retry decisions."""

    TRANSIENT = "transient"  # retry-worthy: 429, 500, 502, 503, network
    CONTEXT_EXCEEDED = "context_exceeded"  # shrink context, don't retry blindly
    AUTH = "auth"  # never retry
    PERMANENT = "permanent"  # bad request, never retry
    UNKNOWN = "unknown"  # retry once, then give up


def classify_error(exc: Exception) -> str:
    """Classify an exception into an ErrorKind."""
    msg = str(exc).lower()

    # Context window exceeded
    if any(s in msg for s in ("context length", "context_length", "too many tokens", "maximum context")):
        return ErrorKind.CONTEXT_EXCEEDED

    # Auth failures
    if any(s in msg for s in ("authentication", "unauthorized", "invalid api key", "permission denied")):
        return ErrorKind.AUTH

    # Rate limits and server errors → transient
    if any(s in msg for s in ("rate limit", "429", "overloaded", "503", "502", "500", "timeout", "connection")):
        return ErrorKind.TRANSIENT

    # Bad request — permanent
    if any(s in msg for s in ("400", "invalid", "malformed")):
        return ErrorKind.PERMANENT

    return ErrorKind.UNKNOWN


# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetryConfig:
    """Retry parameters for a single provider attempt."""

    max_attempts: int = 4
    initial_delay_s: float = 0.5
    max_delay_s: float = 16.0
    jitter_factor: float = 0.25
    budget_s: float = 60.0

    def delay_for_attempt(self, attempt: int) -> float:
        """Compute delay with exponential backoff and jitter."""
        base = min(self.initial_delay_s * (2 ** attempt), self.max_delay_s)
        jitter = base * self.jitter_factor * random.random()
        return base + jitter


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------


@dataclass
class ProviderAttemptResult:
    """Result of trying a single provider."""

    success: bool
    response: ModelResponse | None = None
    stream: AsyncIterator[StreamEvent] | None = None
    error: Exception | None = None
    error_kind: str = ErrorKind.UNKNOWN
    usage: Usage = field(default_factory=Usage)


class ModelGateway:
    """Unified interface to LLM providers with retry and fallback.

    Usage:
        gateway = ModelGateway(primary=anthropic_provider, fallbacks=[openai_provider])
        response = await gateway.complete(messages, system="You are a helpful assistant.")
    """

    def __init__(
        self,
        primary: ProviderProtocol,
        fallbacks: list[ProviderProtocol] | None = None,
        retry_config: RetryConfig | None = None,
    ) -> None:
        self._primary = primary
        self._fallbacks = fallbacks or []
        self._retry = retry_config or RetryConfig()
        self._total_usage = Usage()
        self._call_count = 0

    @property
    def primary(self) -> ProviderProtocol:
        return self._primary

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._primary.capabilities

    @property
    def total_usage(self) -> Usage:
        return self._total_usage

    @property
    def call_count(self) -> int:
        return self._call_count

    def _accumulate_usage(self, usage: Usage) -> None:
        """Accumulate usage from a call into the gateway total."""
        self._total_usage = Usage(
            input_tokens=self._total_usage.input_tokens + usage.input_tokens,
            output_tokens=self._total_usage.output_tokens + usage.output_tokens,
            cache_read_tokens=self._total_usage.cache_read_tokens + usage.cache_read_tokens,
            cache_write_tokens=self._total_usage.cache_write_tokens + usage.cache_write_tokens,
            reasoning_tokens=self._total_usage.reasoning_tokens + usage.reasoning_tokens,
            source="aggregated",
        )
        self._call_count += 1

    # --- Retry loop for a single provider ---

    async def _attempt_with_retry(
        self,
        provider: ProviderProtocol,
        messages: list[Message],
        *,
        mode: str = "complete",
        **kwargs: Any,
    ) -> ProviderAttemptResult:
        """Try a provider with retry logic. Returns result or last error."""
        start = time.monotonic()
        last_exc: Exception | None = None
        last_kind = ErrorKind.UNKNOWN

        for attempt in range(self._retry.max_attempts):
            # Check time budget
            elapsed = time.monotonic() - start
            if elapsed >= self._retry.budget_s:
                logger.warning(
                    "Retry budget exhausted for %s after %.1fs",
                    provider.name,
                    elapsed,
                )
                break

            try:
                if mode == "complete":
                    response = await provider.complete(messages, **kwargs)
                    self._accumulate_usage(response.usage)
                    return ProviderAttemptResult(
                        success=True,
                        response=response,
                        usage=response.usage,
                    )
                else:
                    stream = provider.stream(messages, **kwargs)
                    return ProviderAttemptResult(success=True, stream=stream)

            except Exception as exc:
                last_exc = exc
                last_kind = classify_error(exc)

                # Don't retry permanent or auth errors
                if last_kind in (ErrorKind.AUTH, ErrorKind.PERMANENT):
                    logger.error(
                        "Non-retryable error from %s: %s (%s)",
                        provider.name,
                        exc,
                        last_kind,
                    )
                    break

                # Context exceeded — signal up, don't retry
                if last_kind == ErrorKind.CONTEXT_EXCEEDED:
                    logger.warning("Context exceeded on %s", provider.name)
                    break

                # Transient — wait and retry
                if attempt < self._retry.max_attempts - 1:
                    delay = self._retry.delay_for_attempt(attempt)
                    remaining_budget = self._retry.budget_s - (time.monotonic() - start)
                    delay = min(delay, max(0, remaining_budget - 0.1))
                    if delay > 0:
                        logger.info(
                            "Retrying %s in %.2fs (attempt %d, %s)",
                            provider.name,
                            delay,
                            attempt + 1,
                            last_kind,
                        )
                        await asyncio.sleep(delay)

        return ProviderAttemptResult(
            success=False,
            error=last_exc,
            error_kind=last_kind,
        )

    # --- Public interface ---

    async def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        stop_sequences: list[str] | None = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Send messages to the model and get a complete response.

        Tries primary provider first, then each fallback in order.
        Raises ModelAllFailedError if all providers fail.
        Raises ModelContextExceededError if context is too large for all providers.
        """
        call_kwargs: dict[str, Any] = {
            "system": system,
            "tools": tools,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stop_sequences": stop_sequences,
            **kwargs,
        }

        providers = [self._primary, *self._fallbacks]
        errors: list[tuple[str, Exception, str]] = []

        for provider in providers:
            result = await self._attempt_with_retry(
                provider, messages, mode="complete", **call_kwargs
            )

            if result.success and result.response is not None:
                return result.response

            if result.error:
                errors.append((provider.name, result.error, result.error_kind))

                # Context exceeded on all providers is a hard stop
                if result.error_kind == ErrorKind.CONTEXT_EXCEEDED:
                    if provider == providers[-1]:
                        raise ModelContextExceededError(
                            f"Context exceeded on all providers: {[e[0] for e in errors]}",
                            recoverable=False,
                        )
                    # Try next provider (might have larger window)
                    continue

                # Auth errors on fallback providers are worth skipping
                if result.error_kind == ErrorKind.AUTH:
                    logger.warning("Auth failed for %s, trying next", provider.name)
                    continue

        # All failed
        error_summary = "; ".join(
            f"{name}: {exc} ({kind})" for name, exc, kind in errors
        )
        raise ModelAllFailedError(
            f"All providers failed: {error_summary}",
            recoverable=False,
        )

    async def stream(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        stop_sequences: list[str] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Stream responses from the model.

        Tries primary provider first, then each fallback.
        Yields StreamEvent objects. On unrecoverable failure,
        yields a StreamError event and stops.
        """
        call_kwargs: dict[str, Any] = {
            "system": system,
            "tools": tools,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stop_sequences": stop_sequences,
            **kwargs,
        }

        providers = [self._primary, *self._fallbacks]
        errors: list[tuple[str, Exception, str]] = []

        for provider in providers:
            result = await self._attempt_with_retry(
                provider, messages, mode="stream", **call_kwargs
            )

            if result.success and result.stream is not None:
                # Wrap the stream to track usage from UsageUpdate events
                async for event in self._track_stream_usage(result.stream):
                    yield event
                return

            if result.error:
                errors.append((provider.name, result.error, result.error_kind))
                if result.error_kind in (ErrorKind.AUTH, ErrorKind.PERMANENT):
                    continue

        # All providers failed — yield error event
        error_summary = "; ".join(f"{name}: {exc}" for name, exc, _ in errors)
        yield StreamError(
            error=f"All providers failed: {error_summary}",
            code="all_failed",
            recoverable=False,
        )

    async def _track_stream_usage(
        self, stream: AsyncIterator[StreamEvent]
    ) -> AsyncIterator[StreamEvent]:
        """Wrap a stream to accumulate usage from UsageUpdate events."""
        from .normalize import UsageUpdate as _UsageUpdate

        async for event in stream:
            if isinstance(event, _UsageUpdate):
                usage = Usage(
                    input_tokens=event.input_tokens,
                    output_tokens=event.output_tokens,
                    cache_read_tokens=event.cache_read_tokens,
                    cache_write_tokens=event.cache_write_tokens,
                )
                self._accumulate_usage(usage)
            yield event

    async def close(self) -> None:
        """Close all provider connections."""
        providers = [self._primary, *self._fallbacks]
        for provider in providers:
            try:
                await provider.close()
            except Exception as exc:
                logger.warning("Error closing %s: %s", provider.name, exc)
