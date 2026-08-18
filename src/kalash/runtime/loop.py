"""The agent turn loop.

Main loop: assemble → stream → parse tool calls → execute → append → repeat.
Terminates on: no tool calls, budget exhausted, user interrupt, or Stop hook.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from kalash.core.budget import BudgetState, Usage
from kalash.core.events import Event, EventBus, EventType
from kalash.core.ids import generate_id
from kalash.models.gateway import ModelGateway
from kalash.models.normalize import (
    ContentBlock,
    Message,
    Role,
    StopReason,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from kalash.storage.repositories.sessions import SessionRepository

from .context import ContextAssembler
from .stream import StreamHandler, StreamResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool classification for scheduling
# ---------------------------------------------------------------------------


class ToolCategory(StrEnum):
    """Tool concurrency category."""

    READ = "read"  # Concurrent via semaphore
    WRITE = "write"  # Serialized
    EXEC = "exec"  # Serialized


# ---------------------------------------------------------------------------
# Termination reasons
# ---------------------------------------------------------------------------


class TerminationReason(StrEnum):
    """Why the loop stopped."""

    NO_TOOL_CALLS = "no_tool_calls"
    BUDGET_EXHAUSTED = "budget_exhausted"
    USER_INTERRUPT = "user_interrupt"
    STOP_HOOK = "stop_hook"
    MAX_ITERATIONS = "max_iterations"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Loop result
# ---------------------------------------------------------------------------


@dataclass
class LoopResult:
    """Outcome of a complete agent loop run."""

    termination_reason: TerminationReason
    iterations: int = 0
    total_tokens: int = 0
    final_response: str = ""
    error: str | None = None


# ---------------------------------------------------------------------------
# Tool registry protocol
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Protocol for the tool dispatch system.

    Concrete implementation lives in kalash.tools — this defines the
    interface the loop depends on.
    """

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        tool_use_id: str,
    ) -> str:
        """Execute a tool and return the result string."""
        raise NotImplementedError

    def category(self, name: str) -> ToolCategory:
        """Return the concurrency category of a tool."""
        raise NotImplementedError

    def schemas(self) -> list[dict[str, Any]]:
        """Return JSON schemas for all registered tools."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


@dataclass
class AgentLoop:
    """The core agent turn loop.

    Orchestrates: context assembly → model streaming → tool dispatch → repeat.
    """

    gateway: ModelGateway
    tool_registry: ToolRegistry
    session_repo: SessionRepository
    event_bus: EventBus
    budget: BudgetState
    assembler: ContextAssembler

    # Configuration
    session_id: str = ""
    max_iterations: int = 50
    read_concurrency: int = 5

    # Internal state
    _running: bool = field(init=False, default=False)
    _cancelled: bool = field(init=False, default=False)
    _iteration: int = field(init=False, default=0)
    _conversation: list[Message] = field(init=False, default_factory=list)
    _read_semaphore: asyncio.Semaphore = field(init=False, default=None)  # type: ignore[assignment]
    _write_lock: asyncio.Lock = field(init=False, default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._read_semaphore = asyncio.Semaphore(self.read_concurrency)
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        *,
        user_message: Message,
        system_identity: str,
        skills_catalog: list[dict[str, str]] | None = None,
        kalash_md_chain: list[str] | None = None,
        memory_blocks: list[str] | None = None,
        compacted_summary: str | None = None,
        recent_turns: list[Message] | None = None,
        on_text_delta: Any | None = None,
    ) -> LoopResult:
        """Run the agent loop until termination.

        Args:
            user_message: The current user message to process.
            system_identity: The system prompt/identity text.
            skills_catalog: Available skills (name + description).
            kalash_md_chain: KALASH.md hierarchy contents.
            memory_blocks: Retrieved memory for this turn.
            compacted_summary: Compacted history summary.
            recent_turns: Recent conversation turns.
            on_text_delta: Callback for streaming text output.

        Returns:
            LoopResult describing how/why the loop terminated.
        """
        self._running = True
        self._cancelled = False
        self._iteration = 0

        # Seed conversation with the user message
        self._conversation = list(recent_turns or [])
        self._conversation.append(user_message)

        try:
            while self._running and self._iteration < self.max_iterations:
                self._iteration += 1

                # Check budget before each iteration
                ceiling = self.budget.check_ceiling()
                if ceiling is not None:
                    return LoopResult(
                        termination_reason=TerminationReason.BUDGET_EXHAUSTED,
                        iterations=self._iteration,
                        total_tokens=self.budget.tokens_used,
                    )

                # Assemble context
                messages = await self.assembler.assemble(
                    system_identity=system_identity,
                    tool_schemas=self.tool_registry.schemas(),
                    skills_catalog=skills_catalog or [],
                    kalash_md_chain=kalash_md_chain or [],
                    memory_blocks=memory_blocks or [],
                    compacted_summary=compacted_summary,
                    recent_turns=self._conversation[:-1],
                    current_message=self._conversation[-1],
                )

                # Emit turn start
                await self.event_bus.emit(Event(
                    type=EventType.TURN_START,
                    session_id=self.session_id,
                    data={"iteration": self._iteration},
                ))

                # Stream model response
                stream_result = await self._stream_response(
                    messages, on_text_delta=on_text_delta
                )

                # Record usage
                usage = Usage(
                    input_tokens=stream_result.input_tokens,
                    output_tokens=stream_result.output_tokens,
                    cache_read_tokens=stream_result.cache_read_tokens,
                    cache_write_tokens=stream_result.cache_write_tokens,
                )
                self.budget.record_usage(usage)

                # Build assistant message and append to conversation
                assistant_msg = Message(
                    role=Role.ASSISTANT,
                    content=stream_result.content,
                )
                self._conversation.append(assistant_msg)

                # Persist the assistant turn
                await self._persist_turn(assistant_msg, usage)

                # Check for tool calls
                if not stream_result.has_tool_calls:
                    await self.event_bus.emit(Event(
                        type=EventType.TURN_COMPLETE,
                        session_id=self.session_id,
                        data={"iteration": self._iteration},
                    ))
                    return LoopResult(
                        termination_reason=TerminationReason.NO_TOOL_CALLS,
                        iterations=self._iteration,
                        total_tokens=self.budget.tokens_used,
                        final_response=stream_result.text_content,
                    )

                # Execute tool calls
                tool_results = await self._execute_tools(stream_result.tool_calls)

                # Build tool result message and append
                tool_result_msg = Message(
                    role=Role.USER,
                    content=tool_results,
                )
                self._conversation.append(tool_result_msg)

                # Check for interruption
                if self._cancelled:
                    return LoopResult(
                        termination_reason=TerminationReason.USER_INTERRUPT,
                        iterations=self._iteration,
                        total_tokens=self.budget.tokens_used,
                    )

            return LoopResult(
                termination_reason=TerminationReason.MAX_ITERATIONS,
                iterations=self._iteration,
                total_tokens=self.budget.tokens_used,
            )

        except asyncio.CancelledError:
            return LoopResult(
                termination_reason=TerminationReason.USER_INTERRUPT,
                iterations=self._iteration,
                total_tokens=self.budget.tokens_used,
            )
        except Exception as exc:
            logger.exception("Agent loop error")
            return LoopResult(
                termination_reason=TerminationReason.ERROR,
                iterations=self._iteration,
                total_tokens=self.budget.tokens_used,
                error=str(exc),
            )
        finally:
            self._running = False

    def cancel(self) -> None:
        """Signal the loop to stop after the current iteration."""
        self._cancelled = True

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def _stream_response(
        self,
        messages: list[Message],
        *,
        on_text_delta: Any | None = None,
    ) -> StreamResult:
        """Stream a response from the model gateway."""
        handler = StreamHandler(on_text_delta=on_text_delta)

        stream: AsyncIterator[StreamEvent] = await self.gateway.stream(messages)
        async for event in stream:
            handler.feed(event)
            if handler.is_complete:
                break

        return handler.result()

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tools(
        self, tool_calls: list[ToolUseBlock]
    ) -> list[ContentBlock]:
        """Execute tool calls with appropriate concurrency.

        READ tools run concurrently (semaphore-limited).
        WRITE/EXEC tools run serialized.
        """
        results: list[ContentBlock] = []

        # Partition by category
        reads: list[ToolUseBlock] = []
        serialized: list[ToolUseBlock] = []

        for call in tool_calls:
            cat = self.tool_registry.category(call.name)
            if cat == ToolCategory.READ:
                reads.append(call)
            else:
                serialized.append(call)

        # Execute reads concurrently
        if reads:
            read_tasks = [
                self._execute_single_tool_read(call) for call in reads
            ]
            read_results = await asyncio.gather(*read_tasks, return_exceptions=True)
            for call, result in zip(reads, read_results):
                if isinstance(result, Exception):
                    results.append(ToolResultBlock(
                        tool_use_id=call.id,
                        content=f"Error: {result}",
                        is_error=True,
                    ))
                else:
                    results.append(result)

        # Execute writes/execs serialized
        for call in serialized:
            if self._cancelled:
                results.append(ToolResultBlock(
                    tool_use_id=call.id,
                    content="Cancelled by user.",
                    is_error=True,
                ))
                break
            result = await self._execute_single_tool_write(call)
            results.append(result)

        return results

    async def _execute_single_tool_read(self, call: ToolUseBlock) -> ToolResultBlock:
        """Execute a READ-category tool with semaphore."""
        async with self._read_semaphore:
            return await self._execute_tool_with_hooks(call)

    async def _execute_single_tool_write(self, call: ToolUseBlock) -> ToolResultBlock:
        """Execute a WRITE/EXEC-category tool with serialization lock."""
        async with self._write_lock:
            return await self._execute_tool_with_hooks(call)

    async def _execute_tool_with_hooks(self, call: ToolUseBlock) -> ToolResultBlock:
        """Execute a tool call with PreToolUse/PostToolUse hooks."""
        # Emit PreToolUse
        pre_event = Event(
            type=EventType.TOOL_START,
            session_id=self.session_id,
            data={
                "tool_name": call.name,
                "tool_use_id": call.id,
                "arguments": call.input,
            },
        )
        await self.event_bus.emit(pre_event)

        start_time = time.monotonic()
        try:
            result_str = await self.tool_registry.execute(
                call.name, call.input, tool_use_id=call.id
            )
            duration_ms = int((time.monotonic() - start_time) * 1000)

            # Emit PostToolUse
            await self.event_bus.emit(Event(
                type=EventType.TOOL_COMPLETE,
                session_id=self.session_id,
                data={
                    "tool_name": call.name,
                    "tool_use_id": call.id,
                    "duration_ms": duration_ms,
                },
            ))

            self.budget.tool_calls_used += 1

            return ToolResultBlock(
                tool_use_id=call.id,
                content=result_str,
                is_error=False,
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.warning("Tool %s failed: %s", call.name, exc)

            await self.event_bus.emit(Event(
                type=EventType.TOOL_COMPLETE,
                session_id=self.session_id,
                data={
                    "tool_name": call.name,
                    "tool_use_id": call.id,
                    "duration_ms": duration_ms,
                    "error": str(exc),
                },
            ))

            return ToolResultBlock(
                tool_use_id=call.id,
                content=f"Error executing {call.name}: {exc}",
                is_error=True,
            )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _persist_turn(self, message: Message, usage: Usage) -> None:
        """Persist an assistant turn to storage."""
        try:
            turn_id = await self.session_repo.create_turn(
                session_id=self.session_id,
                seq=self._iteration,
                role="assistant",
                model=self.gateway.primary.name(),
            )
            await self.session_repo.complete_turn(
                turn_id=turn_id,
                state="COMPLETED",
                token_count=usage.total_tokens,
            )
        except Exception:
            logger.warning("Failed to persist turn", exc_info=True)
