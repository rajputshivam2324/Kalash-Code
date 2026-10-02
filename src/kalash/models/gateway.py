"""Normalized provider calls, bounded transient retries and usage totals."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from kalash.core.budget import Pricing, Usage
from kalash.core.errors import (
    ModelAllFailedError,
    ModelContextExceededError,
)

from .normalize import (
    Message,
    ModelCapabilities,
    ModelResponse,
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

    def stream(
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


def classify_error(exc: Exception, model_id: str = "") -> str:
    """Classify provider failures without changing request content."""
    msg = str(exc).lower()

    # Context window exceeded
    if any(
        s in msg for s in ("context length", "context_length", "too many tokens", "maximum context")
    ):
        return ErrorKind.CONTEXT_EXCEEDED

    # Auth failures
    if any(
        s in msg for s in ("authentication", "unauthorized", "invalid api key", "permission denied")
    ):
        return ErrorKind.AUTH

    if "413" in msg or ("tokens per minute" in msg and "request too large" in msg):
        return ErrorKind.PERMANENT

    # Transient rate limits and service errors
    if any(
        s in msg
        for s in (
            "rate limit",
            "429",
            "tpm",
            "tokens per minute",
            "overloaded",
            "503",
            "502",
            "500",
            "timeout",
            "connection",
        )
    ):
        return ErrorKind.TRANSIENT

    # Tool argument parse failure (Groq / OpenAI upstream) — retryable with remedy
    if any(
        s in msg
        for s in (
            "failed to parse tool call",
            "parse tool call arguments",
            "failed to call a function",
            "failed_generation",
            "cutoff by max_tokens",
        )
    ):
        return ErrorKind.TRANSIENT

    # Bad request — permanent
    if any(s in msg for s in ("400", "invalid", "malformed")):
        return ErrorKind.PERMANENT

    return ErrorKind.UNKNOWN


def _extract_retry_after(exc: Exception) -> float | None:
    """Extract retry-after delay from provider error messages and HTTP headers."""
    # Check HTTP Retry-After header if response object exists
    resp = getattr(exc, "response", None)
    if resp and hasattr(resp, "headers"):
        header = (
            resp.headers.get("retry-after")
            or resp.headers.get("Retry-After")
            or resp.headers.get("retry-after-ms")
        )
        if header:
            try:
                val = float(header)
                # Values > 1000 are likely milliseconds
                return val / 1000.0 if val > 1000 else val
            except ValueError:
                pass

    # Parse from error message text
    msg = str(exc)
    patterns = [
        r"try again in ([\d.]+)\s*(?:s|sec)",
        r"retry after ([\d.]+)\s*(?:s|sec)",
        r"wait ([\d.]+)\s*(?:s|sec)",
        r"retry.after\s*[:=]\s*([\d.]+)",
        r"in ([\d.]+)\s*ms",  # milliseconds
    ]
    for pat in patterns:
        m = re.search(pat, msg, re.I)
        if m:
            try:
                val = float(m.group(1))
                # "in 200ms" pattern returns ms
                if "ms" in pat:
                    return val / 1000.0
                return val
            except ValueError:
                pass
    return None


@dataclass(frozen=True, slots=True)
class RetryConfig:
    """Retry parameters for a single provider attempt."""

    max_attempts: int = 6
    initial_delay_s: float = 1.0
    max_delay_s: float = 30.0
    jitter_factor: float = 0.25
    budget_s: float = 180.0  # 3 min — enough for a Groq 429 retry-after (typ 12-30s)

    def delay_for_attempt(self, attempt: int) -> float:
        """Compute delay with exponential backoff and jitter."""
        base = min(self.initial_delay_s * (2**attempt), self.max_delay_s)
        jitter = base * self.jitter_factor * random.random()
        return float(base + jitter)


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
    """Unified interface to LLM providers with bounded retries and fallback."""

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

    def _accumulate_usage(self, usage: Usage, model_id: str = "") -> None:
        """Accumulate usage from a call into the gateway total and rate pacer."""
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

    async def _stream_with_retry(
        self,
        provider: ProviderProtocol,
        messages: list[Message],
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        start = time.monotonic()
        last_exc: Exception | None = None
        yielded_any = False

        for attempt in range(self._retry.max_attempts):
            elapsed = time.monotonic() - start
            if elapsed >= self._retry.budget_s:
                logger.warning(
                    "Retry budget exhausted for %s after %.1fs",
                    provider.name,
                    elapsed,
                )
                break

            try:
                stream = provider.stream(messages, **kwargs)
                try:
                    async for event in stream:
                        yielded_any = True
                        yield event
                finally:
                    close = getattr(stream, "aclose", None)
                    if close is not None:
                        await close()
                return
            except Exception as exc:
                last_exc = exc
                last_kind = classify_error(exc, model_id=provider.name)

                if last_kind in (ErrorKind.AUTH, ErrorKind.PERMANENT, ErrorKind.CONTEXT_EXCEEDED):
                    raise exc

                # If we already yielded events, don't retry from scratch
                # as it would duplicate output
                if yielded_any:
                    raise exc

                if attempt < self._retry.max_attempts - 1:
                    retry_after = _extract_retry_after(exc)
                    if retry_after is not None:
                        delay = retry_after + 0.5
                    else:
                        delay = self._retry.delay_for_attempt(attempt)
                    remaining = self._retry.budget_s - (time.monotonic() - start)
                    delay = min(delay, max(0, remaining - 0.1))
                    if delay > 0:
                        logger.info(
                            "Retrying stream %s in %.2fs (attempt %d, %s)",
                            provider.name,
                            delay,
                            attempt + 1,
                            last_kind,
                        )
                        await asyncio.sleep(delay)

        if last_exc:
            raise last_exc

    async def _attempt_with_retry(
        self,
        provider: ProviderProtocol,
        messages: list[Message],
        *,
        mode: str = "complete",
        **kwargs: Any,
    ) -> ProviderAttemptResult:
        """Try a provider without changing the requested prompt or output budget and retry logic."""
        start = time.monotonic()
        last_exc: Exception | None = None
        last_kind = ErrorKind.UNKNOWN

        for attempt in range(self._retry.max_attempts):
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
                    self._accumulate_usage(response.usage, model_id=provider.name)
                    return ProviderAttemptResult(
                        success=True,
                        response=response,
                        usage=response.usage,
                    )
                stream = self._stream_with_retry(provider, messages, **kwargs)
                return ProviderAttemptResult(success=True, stream=stream)

            except Exception as exc:
                last_exc = exc
                last_kind = classify_error(exc, model_id=provider.name)

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
                    retry_after = _extract_retry_after(exc)
                    if retry_after is not None:
                        delay = retry_after + 0.5
                    else:
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
        """Send messages to the model and get a complete response."""

        call_kwargs: dict[str, Any] = {
            "system": system,
            "tools": tools,
            "temperature": temperature,
            "stop_sequences": stop_sequences,
            **kwargs,
        }

        providers = [self._primary, *self._fallbacks]
        errors: list[tuple[str, Exception, str]] = []

        for provider in providers:
            provider_kwargs = dict(call_kwargs)
            if max_tokens is not None:
                provider_kwargs["max_tokens"] = max_tokens

            result = await self._attempt_with_retry(
                provider, messages, mode="complete", **provider_kwargs
            )

            if result.success and result.response is not None:
                return result.response

            if result.error:
                errors.append((provider.name, result.error, result.error_kind))

                if result.error_kind == ErrorKind.CONTEXT_EXCEEDED:
                    if provider == providers[-1]:
                        raise ModelContextExceededError(
                            f"Context exceeded on all providers: {[e[0] for e in errors]}",
                            recoverable=False,
                        )
                    continue

                if result.error_kind == ErrorKind.AUTH:
                    logger.warning("Auth failed for %s, trying next", provider.name)
                    continue

        error_summary = "; ".join(f"{name}: {exc} ({kind})" for name, exc, kind in errors)
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
        """Stream responses from the model without changing the requested prompt or output budget."""

        call_kwargs: dict[str, Any] = {
            "system": system,
            "tools": tools,
            "temperature": temperature,
            "stop_sequences": stop_sequences,
            **kwargs,
        }

        providers = [self._primary, *self._fallbacks]
        errors: list[tuple[str, Exception, str]] = []

        for provider in providers:
            provider_kwargs = dict(call_kwargs)
            if max_tokens is not None:
                provider_kwargs["max_tokens"] = max_tokens

            result = await self._attempt_with_retry(
                provider, messages, mode="stream", **provider_kwargs
            )

            if result.success and result.stream is not None:
                tracked = self._track_stream_usage(result.stream, model_id=provider.name)
                try:
                    async for event in tracked:
                        yield event
                finally:
                    close = getattr(tracked, "aclose", None)
                    if close is not None:
                        await close()
                return

            if result.error:
                errors.append((provider.name, result.error, result.error_kind))
                if result.error_kind in (ErrorKind.AUTH, ErrorKind.PERMANENT):
                    continue

        error_summary = "; ".join(f"{name}: {exc}" for name, exc, _ in errors)
        yield StreamError(
            error=f"All providers failed: {error_summary}",
            code="all_failed",
            recoverable=False,
        )

    async def _track_stream_usage(
        self, stream: AsyncIterator[StreamEvent], model_id: str = ""
    ) -> AsyncIterator[StreamEvent]:
        """Wrap a stream to accumulate usage from UsageUpdate events."""
        from .normalize import UsageUpdate as _UsageUpdate

        try:
            async for event in stream:
                if isinstance(event, _UsageUpdate):
                    usage = Usage(
                        input_tokens=event.input_tokens,
                        output_tokens=event.output_tokens,
                        cache_read_tokens=event.cache_read_tokens,
                        cache_write_tokens=event.cache_write_tokens,
                        reasoning_tokens=event.reasoning_tokens,
                    )
                    self._accumulate_usage(usage, model_id=model_id)
                yield event
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()

    async def close(self) -> None:
        """Close all provider connections."""
        providers = [self._primary, *self._fallbacks]
        for provider in providers:
            try:
                await provider.close()
            except Exception as exc:
                logger.warning("Error closing %s: %s", provider.name, exc)
