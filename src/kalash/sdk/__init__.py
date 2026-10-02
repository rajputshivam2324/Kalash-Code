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

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kalash.runtime.agent import Agent, build_agent


@dataclass
class CompletionResponse:
    """Response from a single completion request."""

    text: str
    model: str
    tokens_used: int
    cost_usd: float
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    session_id: str | None = None


@dataclass
class StreamChunk:
    """A single chunk in a streaming response."""

    text: str
    done: bool = False
    tool_call: dict[str, Any] | None = None


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
    ) -> CompletionResponse:
        """Run a single-shot completion within this session."""
        if max_turns is not None:
            self._agent.loop.max_iterations = max_turns
        if model is not None:
            self._agent.loop.model_id = model
        if tools is not None:
            registry = self._agent.host.registry
            for tool in registry.list_tools():
                if tool.name not in tools:
                    registry.unregister(tool.name)

        result = await self._agent.send(prompt)
        return CompletionResponse(
            text=result.final_response,
            model=self._agent.loop.model_id,
            tokens_used=result.total_tokens,
            cost_usd=float(self._agent.budget.cost_used),
            session_id=self._agent.session_id,
        )

    async def stream(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_turns: int | None = None,
        tools: list[str] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a response within this session."""
        if max_turns is not None:
            self._agent.loop.max_iterations = max_turns
        if model is not None:
            self._agent.loop.model_id = model
        if tools is not None:
            registry = self._agent.host.registry
            for tool in registry.list_tools():
                if tool.name not in tools:
                    registry.unregister(tool.name)

        buffer: list[str] = []

        def on_delta(text: str) -> None:
            buffer.append(text)

        result = await self._agent.send(prompt, on_text_delta=on_delta)
        yield StreamChunk(text="".join(buffer))
        if result.error:
            yield StreamChunk(text=f"\n[error: {result.error}]", done=True)
        else:
            yield StreamChunk(text="", done=True)

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
        )
        if agent is None:
            raise RuntimeError(reason or "could not build agent")

        await prepare_agent(agent)

        kalash_session = KalashSession(agent, self)
        try:
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
    ) -> CompletionResponse:
        async with self.session(model=model) as sess:
            return await sess.complete(prompt, model=model, max_turns=max_turns, tools=tools)

    async def stream(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_turns: int | None = None,
        tools: list[str] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        async with self.session(model=model) as sess:
            async for chunk in sess.stream(prompt, model=model, max_turns=max_turns, tools=tools):
                yield chunk


__all__ = [
    "KalashClient",
    "KalashSession",
    "CompletionResponse",
    "StreamChunk",
    "SessionInfo",
]
