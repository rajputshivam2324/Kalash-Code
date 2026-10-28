"""Helpers for wire-level adapter tests: real adapters over ``httpx.MockTransport``."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from kalash.llm.anthropic import AnthropicAdapter
from kalash.llm.catalog import ModelInfo
from kalash.llm.http import make_client
from kalash.llm.openai_chat import OpenAIChatAdapter, OpenAIDialect
from kalash.llm.types import Done, Message, Request, StreamEvent, TextBlock


def sse_bytes(events: list[dict[str, Any] | str], *, named: bool = False) -> bytes:
    out = []
    for event in events:
        if isinstance(event, str):
            out.append(f"data: {event}\n\n")
        elif named:
            out.append(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n")
        else:
            out.append(f"data: {json.dumps(event)}\n\n")
    return "".join(out).encode()


def respond(
    content: bytes, status: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(
        status, content=content, headers={"content-type": "text/event-stream", **(headers or {})}
    )


def json_error(status: int, message: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": message}}, headers=headers)


Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    def __init__(self, *responses: httpx.Response | Handler) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected HTTP request: wire script exhausted")
        item = self.responses.pop(0)
        return item(request) if callable(item) else item

    def body(self, index: int = 0) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.requests[index].content)
        return loaded


def openai_adapter(
    recorder: Recorder,
    *,
    provider: str = "openai",
    model: str = "m",
    dialect: OpenAIDialect | None = None,
    window: int = 100_000,
    info: ModelInfo | None = None,
) -> OpenAIChatAdapter:
    client = make_client(transport=httpx.MockTransport(recorder))
    return OpenAIChatAdapter(
        provider=provider,
        model=model,
        base_url="https://x.test/v1",
        api_key="k",
        context_window=window,
        dialect=dialect,
        info=info,
        client=client,
    )


def anthropic_adapter(
    recorder: Recorder,
    *,
    provider: str = "anthropic",
    model: str = "m",
    dialect: str = "anthropic",
    info: ModelInfo | None = None,
    version: str = "2023-06-01",
) -> AnthropicAdapter:
    client = make_client(transport=httpx.MockTransport(recorder))
    return AnthropicAdapter(
        provider=provider,
        model=model,
        base_url="https://x.test",
        api_key="k",
        context_window=200_000,
        default_max_tokens=4096,
        dialect=dialect,  # type: ignore[arg-type]
        info=info,
        version=version,
        client=client,
    )


def simple_request(text: str = "hi", **kw: Any) -> Request:
    return Request(model="m", system="sys", messages=[Message("user", [TextBlock(text)])], **kw)


async def collect(adapter: Any, request: Request) -> list[StreamEvent]:
    return [event async for event in adapter.stream(request)]


async def final(adapter: Any, request: Request) -> Done:
    events = await collect(adapter, request)
    done = events[-1]
    assert isinstance(done, Done)
    return done
