"""Kalash SDK — Public Python API for programmatic embedding.

Usage:
    from kalash.sdk import KalashClient

    async with KalashClient().session() as sess:
        response = await sess.complete("Explain this error")
        print(response.text)

    # Streaming
    async with KalashClient().session() as sess:
        async for chunk in sess.stream("Refactor this function"):
            print(chunk.text, end="")

    # Single-shot (outside session context)
    client = KalashClient()
    response = await client.complete("What does this code do?")
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, AsyncIterator, Optional


@dataclass
class CompletionResponse:
    """Response from a single completion request."""

    text: str
    model: str
    tokens_used: int
    cost_usd: float
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    session_id: Optional[str] = None


@dataclass
class StreamChunk:
    """A single chunk in a streaming response."""

    text: str
    done: bool = False
    tool_call: Optional[dict[str, Any]] = None


@dataclass
class SessionInfo:
    """Information about the current session."""

    id: str
    model: str
    turn_count: int
    tokens_used: int


class KalashSession:
    """An active session context for multi-turn conversations.

    Use as an async context manager via KalashClient.session().
    """

    def __init__(self, session_id: str, client: "KalashClient") -> None:
        self._session_id = session_id
        self._client = client

    @property
    def session_id(self) -> str:
        """The session's unique identifier."""
        return self._session_id

    async def complete(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        max_turns: Optional[int] = None,
        tools: Optional[list[str]] = None,
    ) -> CompletionResponse:
        """Run a single-shot completion within this session.

        Args:
            prompt: The user message to process.
            model: Override the default model for this request.
            max_turns: Maximum agentic turns (tool use loops).
            tools: Restrict available tools to this list.

        Returns:
            CompletionResponse with the assistant's reply.
        """
        from kalash.runtime.loop import AgentLoop

        loop = AgentLoop(session_id=self._session_id)
        result = await loop.run_to_completion(
            prompt, model=model, max_turns=max_turns, tools=tools
        )

        return CompletionResponse(
            text=result.text,
            model=result.model,
            tokens_used=result.tokens_used,
            cost_usd=result.cost_usd,
            tool_calls=result.tool_calls,
            session_id=self._session_id,
        )

    async def stream(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        max_turns: Optional[int] = None,
        tools: Optional[list[str]] = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a response within this session.

        Args:
            prompt: The user message to process.
            model: Override the default model for this request.
            max_turns: Maximum agentic turns (tool use loops).
            tools: Restrict available tools to this list.

        Yields:
            StreamChunk objects as they arrive.
        """
        from kalash.runtime.loop import AgentLoop

        loop = AgentLoop(session_id=self._session_id)

        async for event in loop.run(
            prompt, model=model, max_turns=max_turns, tools=tools
        ):
            match event.type:
                case "chunk":
                    yield StreamChunk(text=event.content)
                case "tool_call_end":
                    yield StreamChunk(
                        text="",
                        tool_call={
                            "name": event.tool_name,
                            "result": event.result,
                            "success": event.success,
                        },
                    )
                case "done":
                    yield StreamChunk(text="", done=True)

    async def info(self) -> SessionInfo:
        """Get current session information."""
        from kalash.runtime.session import SessionManager

        manager = SessionManager()
        session = manager.get_session(self._session_id)

        return SessionInfo(
            id=session.id,
            model=session.model,
            turn_count=session.turn_count,
            tokens_used=session.total_tokens,
        )

    async def close(self) -> None:
        """Close this session."""
        from kalash.runtime.session import SessionManager

        manager = SessionManager()
        manager.close_session(self._session_id)


class KalashClient:
    """Public Python API for embedding Kalash programmatically.

    Provides both single-shot and session-based interaction patterns.
    All operations delegate to the Kalash runtime layer.
    """

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        config_overrides: Optional[dict[str, Any]] = None,
    ) -> None:
        """Initialize the Kalash client.

        Args:
            model: Default model to use for completions.
            config_overrides: Override configuration values for this client instance.
        """
        self._default_model = model
        self._config_overrides = config_overrides or {}

    @asynccontextmanager
    async def session(
        self,
        *,
        resume: Optional[str] = None,
        model: Optional[str] = None,
    ) -> AsyncGenerator[KalashSession, None]:
        """Create or resume a session as an async context manager.

        Args:
            resume: Session ID to resume. Creates a new session if None.
            model: Model override for this session.

        Yields:
            A KalashSession instance for multi-turn interaction.
        """
        from kalash.runtime.session import SessionManager

        manager = SessionManager()

        if resume:
            session = manager.resume(resume)
        else:
            session = manager.create(
                model=model or self._default_model,
                config_overrides=self._config_overrides,
            )

        kalash_session = KalashSession(session_id=session.id, client=self)

        try:
            yield kalash_session
        finally:
            await kalash_session.close()

    async def complete(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        max_turns: Optional[int] = None,
        tools: Optional[list[str]] = None,
    ) -> CompletionResponse:
        """Run a single-shot completion (creates a temporary session).

        Args:
            prompt: The user message to process.
            model: Override the default model.
            max_turns: Maximum agentic turns.
            tools: Restrict available tools.

        Returns:
            CompletionResponse with the assistant's reply.
        """
        async with self.session(model=model) as sess:
            return await sess.complete(
                prompt, model=model, max_turns=max_turns, tools=tools
            )

    async def stream(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        max_turns: Optional[int] = None,
        tools: Optional[list[str]] = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a single-shot response (creates a temporary session).

        Args:
            prompt: The user message to process.
            model: Override the default model.
            max_turns: Maximum agentic turns.
            tools: Restrict available tools.

        Yields:
            StreamChunk objects as they arrive.
        """
        async with self.session(model=model) as sess:
            async for chunk in sess.stream(
                prompt, model=model, max_turns=max_turns, tools=tools
            ):
                yield chunk


__all__ = [
    "KalashClient",
    "KalashSession",
    "CompletionResponse",
    "StreamChunk",
    "SessionInfo",
]
