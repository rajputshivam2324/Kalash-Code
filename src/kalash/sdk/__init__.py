"""Kalash SDK — Public Python API for programmatic embedding.

Usage:
    from kalash.sdk import KalashClient

    async with KalashClient().session() as sess:
        response = await sess.complete("Explain this error")
        print(response.text)

    # Single-shot (outside session context)
    client = KalashClient()
    response = await client.complete("What does this code do?")
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kalash.core.public_events import public_event
from kalash.runtime.agent import Agent, build_agent

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator
    from pathlib import Path

    from kalash.core.events import Event
    from kalash.runtime.completion import CompletionContract


@dataclass
class CompletionResponse:
    """Response from a single completion request."""

    text: str
    model: str
    tokens_used: int
    cost_usd: float | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    session_id: str | None = None
    termination_reason: str = ""
    completion_status: str = "stopped_unverified"
    error: str | None = None
    usage_unknown: bool = False
    run_id: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    verification: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamChunk:
    """A single chunk in a streaming response."""

    text: str
    done: bool = False
    tool_call: dict[str, Any] | None = None
    completion_status: str | None = None
    termination_reason: str | None = None
    usage_unknown: bool = False
    event: dict[str, Any] | None = None
    run_id: str = ""
    tokens_used: int = 0
    error: str | None = None
    verification: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SessionInfo:
    """Information about the current session."""

    id: str
    model: str
    turn_count: int
    tokens_used: int


class KalashSession:
    """An active session context for multi-turn conversations."""

    def __init__(self, agent: Agent, client: KalashClient) -> None:
        self._agent = agent
        self._client = client
        self._call_lock = asyncio.Lock()

    @asynccontextmanager
    async def _options(
        self, model: str | None, max_turns: int | None, tools: list[str] | None
    ) -> AsyncGenerator[None, None]:
        """Per-call routing/filtering never permanently mutates session defaults."""
        from kalash.models.gateway import ModelGateway
        from kalash.models.resolve import build_provider
        from kalash.tools.registry import ToolRegistry

        async with self._call_lock:
            loop, host = self._agent.loop, self._agent.host
            old = (
                loop.gateway,
                loop.model_id,
                loop.max_iterations,
                host.registry,
                host.model_id,
                loop.provider_id,
                host.provider_id,
                loop.assembler.context_window,
            )
            temporary = None
            try:
                if max_turns is not None:
                    if max_turns < 1:
                        raise ValueError("max_turns must be positive")
                    loop.max_iterations = max_turns
                if model is not None:
                    provider_id, model_id = self._client._parse_model(model)  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
                    resolution = build_provider(provider_id or loop.provider_id, model_id)
                    if not resolution.ok or resolution.provider is None:
                        raise ValueError(resolution.reason)
                    temporary = resolution.provider
                    loop.gateway = ModelGateway(temporary)
                    loop.model_id = host.model_id = resolution.model_id or temporary.name
                    loop.provider_id = host.provider_id = resolution.provider_id or ""
                    loop.assembler.context_window = temporary.capabilities.context_window
                if tools is not None:
                    registry = ToolRegistry()
                    for name in tools:
                        registry.register(
                            host.registry.get(name), source=host.registry.source(name)
                        )
                    host.registry = registry
                yield
            finally:
                (
                    loop.gateway,
                    loop.model_id,
                    loop.max_iterations,
                    host.registry,
                    host.model_id,
                    loop.provider_id,
                    host.provider_id,
                    loop.assembler.context_window,
                ) = old
                if temporary is not None:
                    await temporary.close()

    @property
    def session_id(self) -> str:
        return self._agent.session_id

    async def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_turns: int | None = None,
        tools: list[str] | None = None,
        completion_contract: CompletionContract | None = None,
    ) -> CompletionResponse:
        """Run a single-shot completion within this session."""
        async with self._options(model, max_turns, tools):
            initial_usage = self._agent.loop.gateway.total_usage
            observed: list[dict[str, Any]] = []
            overflow = False

            async def record(event: Event) -> None:
                nonlocal overflow
                if len(observed) >= 2000:
                    overflow = True
                    self._agent.cancel()
                    return
                observed.append(public_event(event))

            bus = self._agent.loop.event_bus
            bus.on_all(record)
            try:
                result = await self._agent.send(prompt, completion_contract=completion_contract)
            finally:
                bus.off_all(record)
            return CompletionResponse(
                text=result.final_response,
                model=result.model or self._agent.loop.model_id,
                tokens_used=result.call_tokens,
                cost_usd=None
                if result.usage_unknown or self._agent.loop.gateway.primary.pricing is None
                else float(result.call_cost_usd),
                session_id=self._agent.session_id,
                termination_reason=result.termination_reason.value,
                completion_status=result.completion_status.value,
                error="SDK event collection exhausted; events are incomplete"
                if overflow
                else result.error,
                usage_unknown=result.usage_unknown,
                run_id=result.run_id,
                events=observed,
                verification=result.verification,
                usage={
                    **{
                        key: getattr(self._agent.loop.gateway.total_usage, key)
                        - getattr(initial_usage, key)
                        for key in (
                            "input_tokens",
                            "output_tokens",
                            "cache_read_tokens",
                            "cache_write_tokens",
                            "reasoning_tokens",
                        )
                    },
                    "usage_unknown": result.usage_unknown,
                    "source": "observed_partial" if result.usage_unknown else "provider_reported",
                },
                tool_calls=[e["data"] for e in observed if e["event"] == "tool.started"],
            )

    async def stream(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_turns: int | None = None,
        tools: list[str] | None = None,
        completion_contract: CompletionContract | None = None,
        include_events: bool = False,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a response within this session."""
        async with self._options(model, max_turns, tools):
            queue: asyncio.Queue[StreamChunk | Exception | None] = asyncio.Queue(maxsize=128)
            overflow = False

            async def on_event(event: Event) -> None:
                nonlocal overflow
                if not include_events:
                    return
                projected = public_event(event)
                try:
                    queue.put_nowait(
                        StreamChunk(
                            text="",
                            event=projected,
                            run_id=event.run_id or "",
                            tool_call=projected["data"]
                            if projected["event"] == "tool.started"
                            else None,
                        )
                    )
                except asyncio.QueueFull:
                    overflow = True
                    self._agent.cancel()

            def on_delta(text: str) -> None:
                try:
                    queue.put_nowait(StreamChunk(text=text))
                except asyncio.QueueFull:
                    raise RuntimeError(
                        "SDK stream consumer is too slow; run stopped without dropping output"
                    ) from None

            async def produce() -> None:
                bus = self._agent.loop.event_bus
                bus.on_all(on_event)
                try:
                    result = await self._agent.send(
                        prompt, on_text_delta=on_delta, completion_contract=completion_contract
                    )
                    await queue.put(
                        StreamChunk(
                            text=f"[error: {result.error}]" if result.error else "",
                            done=True,
                            completion_status=result.completion_status.value,
                            termination_reason=result.termination_reason.value,
                            usage_unknown=result.usage_unknown,
                            run_id=result.run_id,
                            tokens_used=result.call_tokens,
                            error="SDK event queue exhausted; events are incomplete"
                            if overflow
                            else result.error,
                            verification=result.verification,
                        )
                    )
                except Exception as error:
                    await queue.put(error)
                finally:
                    bus.off_all(on_event)
                    task = asyncio.current_task()
                    if task is None or not task.cancelling():
                        await queue.put(None)

            producer = asyncio.create_task(produce())
            try:
                while (chunk := await queue.get()) is not None:
                    if isinstance(chunk, Exception):
                        raise chunk
                    yield chunk
            finally:
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)

    async def info(self) -> SessionInfo:
        from kalash.runtime.session import SessionManager

        row = SessionManager().get_session(self._agent.session_id)
        if row is None:
            return SessionInfo(
                id=self._agent.session_id,
                model=self._agent.loop.model_id,
                turn_count=len(self._agent.history),
                tokens_used=self._agent.budget.tokens_used,
            )
        return SessionInfo(
            id=str(row["id"]),
            model=self._agent.loop.model_id,
            turn_count=int(row.get("turn_count", 0)),
            tokens_used=int(row.get("total_tokens", 0)),
        )

    async def close(self) -> None:
        from kalash.runtime.session import SessionManager

        await self._agent.close()
        SessionManager().close_session(self._agent.session_id)


class KalashClient:
    """Public Python API for embedding Kalash programmatically."""

    def __init__(
        self,
        *,
        model: str | None = None,
        config_overrides: dict[str, Any] | None = None,
        cwd: Path | None = None,
    ) -> None:
        self._default_model = model
        self._config_overrides = config_overrides or {}
        self._cwd = cwd

    def _parse_model(self, model: str | None) -> tuple[str | None, str | None]:
        if not model:
            return None, None
        if "/" in model:
            provider_id, model_id = model.split("/", 1)
            return provider_id, model_id
        return None, model

    @asynccontextmanager
    async def session(
        self,
        *,
        resume: str | None = None,
        model: str | None = None,
    ) -> AsyncGenerator[KalashSession, None]:
        from kalash.core.config import load_config
        from kalash.core.logging import configure_logging
        from kalash.runtime.bootstrap import prepare_agent

        configure_logging()
        provider_id, model_id = self._parse_model(model or self._default_model)
        agent, reason = build_agent(
            cwd=self._cwd,
            provider_id=provider_id,
            model_id=model_id,
            session_id=resume,
            resume=bool(resume),
            interactive=False,
            config=load_config(self._cwd, overrides=self._config_overrides),
        )
        if agent is None:
            raise RuntimeError(reason or "could not build agent")

        kalash_session = KalashSession(agent, self)
        try:
            await prepare_agent(agent)
            yield kalash_session
        finally:
            await kalash_session.close()

    async def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_turns: int | None = None,
        tools: list[str] | None = None,
        completion_contract: CompletionContract | None = None,
    ) -> CompletionResponse:
        async with self.session(model=model) as sess:
            return await sess.complete(
                prompt, max_turns=max_turns, tools=tools, completion_contract=completion_contract
            )

    async def stream(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_turns: int | None = None,
        tools: list[str] | None = None,
        completion_contract: CompletionContract | None = None,
        include_events: bool = False,
    ) -> AsyncIterator[StreamChunk]:
        async with self.session(model=model) as sess:
            async for chunk in sess.stream(
                prompt,
                max_turns=max_turns,
                tools=tools,
                completion_contract=completion_contract,
                include_events=include_events,
            ):
                yield chunk


__all__ = [
    "KalashClient",
    "KalashSession",
    "CompletionResponse",
    "StreamChunk",
    "SessionInfo",
]
