"""Unified model gateway — single interface for all LLM providers.

The gateway handles:
- Provider routing (primary + fallback chain)
- Proactive sliding-window TPM/RPM rate pacing (preventing 429 overages before calling)
- Dynamic runtime limit learning & constraint caching
- Retry with jittered exponential backoff (4 attempts, 60s budget)
- Error classification (transient vs permanent vs context exceeded)
- Usage tracking and token budget enforcement
- Streaming normalization
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from kalash.core.budget import Pricing, Usage, estimate_tokens
from kalash.core.errors import (
    ModelAllFailedError,
    ModelContextExceededError,
    ModelError,
)
from kalash.models import limits
from kalash.models.limits import ConstraintCache, effective_limits

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
# Error classification & dynamic limit learning
# ---------------------------------------------------------------------------


class ErrorKind:
    """Classify provider errors for retry decisions."""

    TRANSIENT = "transient"  # retry-worthy: 429, 500, 502, 503, network
    CONTEXT_EXCEEDED = "context_exceeded"  # shrink context, don't retry blindly
    AUTH = "auth"  # never retry
    PERMANENT = "permanent"  # bad request, never retry
    UNKNOWN = "unknown"  # retry once, then give up


def classify_error(exc: Exception, model_id: str = "") -> str:
    """Classify an exception into an ErrorKind and learn constraints if present."""
    msg = str(exc).lower()

    # Context window exceeded
    if any(s in msg for s in ("context length", "context_length", "too many tokens", "maximum context")):
        return ErrorKind.CONTEXT_EXCEEDED

    # Auth failures
    if any(s in msg for s in ("authentication", "unauthorized", "invalid api key", "permission denied")):
        return ErrorKind.AUTH

    # Rate limits (TPM / RPM)
    if any(s in msg for s in ("rate limit", "429", "tpm", "tokens per minute", "overloaded", "503", "502", "500", "timeout", "connection")):
        # Extract TPM limit from error if provider supplied it: e.g. "Limit 8000, Requested 10692"
        match = re.search(r"limit\s+(\d[\d,]*)", msg, re.I)
        if match and model_id:
            try:
                tpm_val = int(match.group(1).replace(",", ""))
                ConstraintCache.record_tpm(model_id, tpm_val)
                logger.info("Learned dynamic TPM constraint for %s: %d", model_id, tpm_val)
            except Exception:
                pass
        return ErrorKind.TRANSIENT

    # Tool argument parse failure (Groq / OpenAI upstream) — retryable with remedy
    if any(s in msg for s in ("failed to parse tool call", "parse tool call arguments", "failed to call a function", "failed_generation", "cutoff by max_tokens")):
        return ErrorKind.TRANSIENT

    # Bad request — permanent
    if any(s in msg for s in ("400", "invalid", "malformed")):
        return ErrorKind.PERMANENT

    return ErrorKind.UNKNOWN


# ---------------------------------------------------------------------------
# Proactive Sliding-Window Rate Pacer
# ---------------------------------------------------------------------------


@dataclass
class SlidingWindowRatePacer:
    """Sliding 60-second window token and request rate pacer across all providers.
    
    Prevents 429 TPM/RPM ceiling breaches proactively before sending requests.
    """

    _window_tokens: dict[str, deque[tuple[float, int]]] = field(default_factory=dict)
    _window_requests: dict[str, deque[float]] = field(default_factory=dict)

    def record_usage(self, model_id: str, tokens: int) -> None:
        """Record consumed tokens into the sliding window."""
        if tokens <= 0:
            return
        now = time.monotonic()
        key = model_id.strip().lower()
        if key not in self._window_tokens:
            self._window_tokens[key] = deque()
            self._window_requests[key] = deque()

        self._window_tokens[key].append((now, tokens))
        self._window_requests[key].append(now)
        self._cleanup(key, now)

    def _cleanup(self, key: str, now: float) -> None:
        cutoff = now - 60.0
        t_queue = self._window_tokens.get(key)
        if t_queue:
            while t_queue and t_queue[0][0] < cutoff:
                t_queue.popleft()
        r_queue = self._window_requests.get(key)
        if r_queue:
            while r_queue and r_queue[0] < cutoff:
                r_queue.popleft()

    async def pace(self, model_id: str, estimated_tokens: int, tpm_limit: int | None) -> float:
        """Calculate necessary proactive sleep time to avoid TPM breach, and sleep if needed."""
        if not tpm_limit or tpm_limit <= 0:
            return 0.0

        now = time.monotonic()
        key = model_id.strip().lower()
        self._cleanup(key, now)

        t_queue = self._window_tokens.get(key, deque())
        used_in_window = sum(count for _, count in t_queue)

        # Leave 5% safety margin
        effective_ceiling = int(tpm_limit * 0.95)
        if used_in_window + estimated_tokens > effective_ceiling:
            needed_reduction = (used_in_window + estimated_tokens) - effective_ceiling
            freed = 0
            wait_time = 0.0
            for ts, count in t_queue:
                freed += count
                wait_time = max(wait_time, 60.0 - (now - ts) + 0.05)
                if freed >= needed_reduction:
                    break

            if wait_time > 0:
                logger.info(
                    "Pacing request for %s: %d tokens in window + %d requested exceeds TPM %d. Pacing for %.2fs",
                    model_id, used_in_window, estimated_tokens, tpm_limit, wait_time,
                )
                await asyncio.sleep(min(wait_time, 60.0))
                return wait_time
        return 0.0


# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------


def _extract_retry_after(exc: Exception) -> float | None:
    """Extract retry-after delay from provider error messages and HTTP headers."""
    # Check HTTP Retry-After header if response object exists
    resp = getattr(exc, "response", None)
    if resp and hasattr(resp, "headers"):
        header = (resp.headers.get("retry-after")
                  or resp.headers.get("Retry-After")
                  or resp.headers.get("retry-after-ms"))
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
        base = min(self.initial_delay_s * (2 ** attempt), self.max_delay_s)
        jitter = base * self.jitter_factor * random.random()
        return base + jitter


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
    """Unified interface to LLM providers with retry, proactive rate pacing, and fallback."""

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
        self._pacer = SlidingWindowRatePacer()

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
        if model_id:
            total_call_tokens = usage.input_tokens + usage.output_tokens
            self._pacer.record_usage(model_id, total_call_tokens)

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
                async for event in stream:
                    yielded_any = True
                    yield event
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
                            provider.name, delay, attempt + 1, last_kind
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
        """Try a provider with proactive rate pacing and retry logic."""
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
                else:
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
        prompt_tokens = _estimate_message_tokens(messages)

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
            adjusted_max, _ = limits.fit_request_size(
                provider.name, prompt_tokens, max_tokens or 4096
            )
            provider_kwargs["max_tokens"] = adjusted_max

            # Proactive sliding-window rate pacing
            model_lims = effective_limits(provider.name)
            await self._pacer.pace(provider.name, prompt_tokens + min(adjusted_max, 1024), model_lims.tokens_per_minute)

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
        """Stream responses from the model with proactive rate pacing."""
        prompt_tokens = _estimate_message_tokens(messages)

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
            adjusted_max, _ = limits.fit_request_size(
                provider.name, prompt_tokens, max_tokens or 4096
            )
            provider_kwargs["max_tokens"] = adjusted_max

            # Proactive sliding-window rate pacing
            model_lims = effective_limits(provider.name)
            await self._pacer.pace(provider.name, prompt_tokens + min(adjusted_max, 1024), model_lims.tokens_per_minute)

            result = await self._attempt_with_retry(
                provider, messages, mode="stream", **provider_kwargs
            )

            if result.success and result.stream is not None:
                async for event in self._track_stream_usage(result.stream, model_id=provider.name):
                    yield event
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

        async for event in stream:
            if isinstance(event, _UsageUpdate):
                usage = Usage(
                    input_tokens=event.input_tokens,
                    output_tokens=event.output_tokens,
                    cache_read_tokens=event.cache_read_tokens,
                    cache_write_tokens=event.cache_write_tokens,
                )
                self._accumulate_usage(usage, model_id=model_id)
            yield event

    async def close(self) -> None:
        """Close all provider connections."""
        providers = [self._primary, *self._fallbacks]
        for provider in providers:
            try:
                await provider.close()
            except Exception as exc:
                logger.warning("Error closing %s: %s", provider.name, exc)


def _estimate_message_tokens(messages: list[Message]) -> int:
    """Estimate total tokens across a list of normalized messages."""
    import json
    total = 0
    for m in messages:
        for b in m.content:
            if hasattr(b, "text") and getattr(b, "text"):
                total += estimate_tokens(getattr(b, "text"))
            elif hasattr(b, "input") and isinstance(getattr(b, "input"), dict):
                total += estimate_tokens(json.dumps(getattr(b, "input")))
            elif hasattr(b, "content") and isinstance(getattr(b, "content"), str):
                total += estimate_tokens(getattr(b, "content"))
            else:
                total += 40
    return max(total, 10)
