"""Ordered tool batches, attempted-call budgets and error results."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kalash.core.budget import BudgetState
from kalash.core.events import Event, EventBus, EventType
from kalash.models.normalize import ContentBlock, ToolResultBlock, ToolUseBlock
from kalash.runtime.toolhost import ToolCategory, ToolHostProtocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass
class ToolExecutor:
    host: ToolHostProtocol
    budget: BudgetState
    event_bus: EventBus
    session_id: str
    cancel_event: asyncio.Event
    read_concurrency: int = 5
    settle: Callable[[ToolResultBlock], Awaitable[None]] | None = None
    _read_semaphore: asyncio.Semaphore = field(init=False)
    _write_lock: asyncio.Lock = field(init=False)

    def __post_init__(self) -> None:
        self._read_semaphore = asyncio.Semaphore(self.read_concurrency)
        self._write_lock = asyncio.Lock()

    async def execute(self, tool_calls: list[ToolUseBlock]) -> list[ContentBlock]:
        """Execute tool calls with appropriate concurrency.

        READ tools run concurrently (semaphore-limited).
        WRITE/EXEC tools run serialized.
        """
        results: list[ContentBlock] = []

        index = 0
        while index < len(tool_calls):
            call = tool_calls[index]
            if self.cancel_event.is_set():
                break
            if self.host.category(call.name) == ToolCategory.READ:
                end = index + 1
                while (
                    end < len(tool_calls)
                    and self.host.category(tool_calls[end].name) == ToolCategory.READ
                ):
                    end += 1
                # A failed receipt must cancel and join siblings before returning.
                async with asyncio.TaskGroup() as group:
                    tasks = [
                        group.create_task(self._execute_single_tool_read(item))
                        for item in tool_calls[index:end]
                    ]
                results.extend(task.result() for task in tasks)
                index = end
            else:
                results.append(await self._execute_single_tool_write(call))
                index += 1

        # Every tool_use block must have a matching tool_result or the provider
        # rejects the next request. Breaking out of the loop on cancellation left
        # later calls unanswered, which made an interrupted session unusable.
        answered = {block.tool_use_id for block in results if isinstance(block, ToolResultBlock)}
        for call in tool_calls:
            if call.id not in answered:
                result = ToolResultBlock(
                    tool_use_id=call.id,
                    content="Not executed: the run was interrupted.",
                    is_error=True,
                )
                await self._settle(result)
                results.append(result)

        return results

    async def _execute_single_tool_read(self, call: ToolUseBlock) -> ToolResultBlock:
        """Execute a READ-category tool with semaphore."""
        async with self._read_semaphore:
            result = await self._execute_tool_with_hooks(call)
            await self._settle(result)
            return result

    async def _execute_single_tool_write(self, call: ToolUseBlock) -> ToolResultBlock:
        """Execute a WRITE/EXEC-category tool with serialization lock."""
        async with self._write_lock:
            result = await self._execute_tool_with_hooks(call)
            await self._settle(result)
            return result

    async def _settle(self, result: ToolResultBlock) -> None:
        # Receipt failures propagate; they are never disguised as tool errors.
        if self.settle is not None:
            await self.settle(result)

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
        if self.budget.tool_calls_used >= self.budget.max_tool_calls:
            return ToolResultBlock(
                tool_use_id=call.id, content="Not executed: run budget exhausted", is_error=True
            )
        self.budget.tool_calls_used += 1
        if "_raw" in call.input:
            return ToolResultBlock(
                tool_use_id=call.id,
                content="Invalid or truncated tool arguments; issue a complete call.",
                is_error=True,
            )
        try:
            execute_result = getattr(self.host, "execute_result", None)
            if execute_result is not None:
                result = await execute_result(call.name, call.input, tool_use_id=call.id)
                result_str, is_error = result.content, result.is_error
                evidence = getattr(result, "evidence", {})
            else:
                result_str = await self.host.execute(call.name, call.input, tool_use_id=call.id)
                is_error = False
                evidence = {}

            return ToolResultBlock(
                tool_use_id=call.id,
                content=result_str,
                is_error=is_error,
                evidence=evidence,
            )
        except Exception as exc:
            logger.warning("Tool %s failed: %s", call.name, exc)
            duration_ms = int((time.monotonic() - start_time) * 1000)

            await self.event_bus.emit(
                Event(
                    type=EventType.TOOL_COMPLETE,
                    session_id=self.session_id,
                    data={
                        "tool_name": call.name,
                        "tool_use_id": call.id,
                        "duration_ms": duration_ms,
                        "ok": False,
                        "error": str(exc),
                    },
                )
            )

            return ToolResultBlock(
                tool_use_id=call.id,
                content=f"Error executing {call.name}: {exc}",
                is_error=True,
            )
