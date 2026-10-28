"""Shared HTTP/SSE transport for adapters: one call = one provider attempt (no hidden retries)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from kalash.llm.errors import ErrorCode, LLMError, from_http
from kalash.llm.types import Request, StreamEvent

DEFAULT_IDLE_TIMEOUT_S = 300.0


class Adapter(Protocol):
    """A provider route. ``stream`` performs exactly one attempt and ends with ``Done``."""

    provider: str
    model: str
    context_window: int

    def stream(self, request: Request) -> AsyncIterator[StreamEvent]: ...

    async def aclose(self) -> None: ...


@dataclass
class SSEEvent:
    event: str | None
    data: str


def make_client(
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT_S, transport: Any = None
) -> httpx.AsyncClient:
    # httpx applies the read timeout per network read, so it doubles as a stream idle timeout.
    timeout = httpx.Timeout(connect=30.0, read=idle_timeout, write=60.0, pool=30.0)
    return httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False)


async def post_sse(
    client: httpx.AsyncClient,
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    provider: str,
) -> AsyncIterator[SSEEvent]:
    """POST ``body`` and yield SSE events. HTTP and transport failures become ``LLMError``."""
    try:
        async with client.stream("POST", url, json=body, headers=headers) as response:
            if response.status_code >= 400:
                raw = await response.aread()
                raise from_http(response.status_code, raw, response.headers, provider)
            event: str | None = None
            data: list[str] = []
            async for line in response.aiter_lines():
                if line == "":
                    if data:
                        yield SSEEvent(event, "\n".join(data))
                    event, data = None, []
                    continue
                if line.startswith(":"):
                    continue
                field, _, value = line.partition(":")
                value = value.removeprefix(" ")
                if field == "data":
                    data.append(value)
                elif field == "event":
                    event = value
            if data:
                yield SSEEvent(event, "\n".join(data))
    except httpx.TimeoutException as error:
        raise LLMError(
            f"request timed out ({type(error).__name__})", ErrorCode.TIMEOUT, provider=provider
        ) from error
    except httpx.TransportError as error:
        raise LLMError(
            f"connection failed: {error}", ErrorCode.TRANSPORT, provider=provider
        ) from error


def parse_json(data: str, provider: str) -> Any:
    try:
        return json.loads(data)
    except ValueError as error:
        raise LLMError(
            f"unparseable stream chunk: {data[:200]!r}",
            ErrorCode.MALFORMED_RESPONSE,
            provider=provider,
        ) from error


def int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
