"""A scripted model route at the adapter boundary: everything above it is real.

Each ``stream`` call consumes the next script entry. An exhausted script raises, so an
unexpected extra model call fails the test instead of silently answering "done".
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from kalash.llm.errors import ErrorCode, LLMError
from kalash.llm.types import (
    Done,
    FinishReason,
    Message,
    ReasoningBlock,
    ReasoningDelta,
    Request,
    StreamEvent,
    TextBlock,
    TextDelta,
    ToolCallBlock,
    ToolCallStarted,
    Usage,
)


@dataclass
class Reply:
    """One scripted model response."""

    text: str = ""
    calls: list[tuple[str, dict[str, Any] | str]] = field(default_factory=list)
    finish: FinishReason | None = None
    usage: Usage | None = None
    reasoning: str = ""
    fail_after_events: int | None = None  # raise mid-stream after N events
    error: LLMError | None = None  # raise instead of streaming
    hang: bool = False  # never finish (for cancellation tests)
    on_start: Callable[[Request], None] | None = None


def say(text: str, **kw: Any) -> Reply:
    return Reply(text=text, **kw)


def call(name: str, arguments: dict[str, Any] | str, *, text: str = "", **kw: Any) -> Reply:
    return Reply(text=text, calls=[(name, arguments)], **kw)


def calls(*items: tuple[str, dict[str, Any] | str], **kw: Any) -> Reply:
    return Reply(calls=list(items), **kw)


def fail(code: ErrorCode, message: str = "boom", **kw: Any) -> Reply:
    return Reply(error=LLMError(message, code, provider="scripted", **kw))


class ScriptedAdapter:
    provider = "scripted"

    def __init__(self, script: list[Reply], *, model: str = "model", window: int = 100_000) -> None:
        self.script = list(script)
        self.model = model
        self.context_window = window
        self.requests: list[Request] = []
        self.closed = False
        self._counter = 0

    async def aclose(self) -> None:
        self.closed = True

    def assert_exhausted(self) -> None:
        assert not self.script, f"{len(self.script)} scripted replies were never requested"

    async def stream(self, request: Request) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        if not self.script:
            raise AssertionError(
                f"unexpected model request #{len(self.requests)}: the script is exhausted"
            )
        reply = self.script.pop(0)
        if reply.on_start:
            reply.on_start(request)
        if reply.error is not None:
            raise reply.error
        emitted = 0

        def tick() -> None:
            nonlocal emitted
            emitted += 1
            if reply.fail_after_events is not None and emitted > reply.fail_after_events:
                raise LLMError("connection reset", ErrorCode.TRANSPORT, provider="scripted")

        blocks: list[Any] = []
        if reply.reasoning:
            tick()
            yield ReasoningDelta(reply.reasoning)
            blocks.append(ReasoningBlock(reply.reasoning))
        if reply.text:
            for piece in _split(reply.text):
                tick()
                yield TextDelta(piece)
            blocks.append(TextBlock(reply.text))
        for index, (name, arguments) in enumerate(reply.calls):
            self._counter += 1
            call_id = f"call_{self._counter}"
            tick()
            yield ToolCallStarted(index, call_id, name)
            raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
            blocks.append(ToolCallBlock(call_id, name, raw))
        if reply.hang:
            await asyncio.sleep(3600)
        finish: FinishReason = reply.finish or ("tool_calls" if reply.calls else "stop")
        usage = reply.usage or Usage(input_tokens=100 + len(request.messages), output_tokens=20)
        yield Done(Message("assistant", blocks, origin=f"scripted/{self.model}"), finish, usage)


def _split(text: str) -> list[str]:
    midpoint = max(1, len(text) // 2)
    return [text[:midpoint], text[midpoint:]] if len(text) > 1 else [text]
