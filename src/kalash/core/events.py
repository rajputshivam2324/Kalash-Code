"""In-process async event bus.

Sideways communication between peer modules goes through this,
never through direct imports.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from kalash.core.privacy import scrub_metadata


class EventType(StrEnum):
    """All event types in Kalash."""

    # Session lifecycle
    SESSION_START = "session.start"
    SESSION_END = "session.end"
    SESSION_IDLE = "session.idle"
    SESSION_RESUME = "session.resume"

    # Turn lifecycle
    TURN_START = "turn.start"
    TURN_COMPLETE = "turn.complete"
    TURN_FAILED = "turn.failed"

    # Tool lifecycle
    TOOL_START = "tool.start"
    TOOL_OUTPUT = "tool.output"
    TOOL_COMPLETE = "tool.complete"
    TOOL_DENIED = "tool.denied"

    # Memory
    MEMORY_RECALL = "memory.recall"
    MEMORY_WRITE = "memory.write"
    MEMORY_FORGET = "memory.forget"

    # Permission
    PERMISSION_REQUESTED = "permission.requested"
    PERMISSION_DECIDED = "permission.decided"

    # Model
    MODEL_REQUEST = "model.request"
    MODEL_RESPONSE = "model.response"
    MODEL_FALLBACK = "model.fallback"

    # Budget
    BUDGET_WARNING = "budget.warning"
    BUDGET_SOFT_LIMIT = "budget.soft_limit"
    BUDGET_EXCEEDED = "budget.exceeded"

    # Compaction
    COMPACT_START = "compact.start"
    COMPACT_COMPLETE = "compact.complete"

    # Agent
    AGENT_SPAWN = "agent.spawn"
    AGENT_COMPLETE = "agent.complete"

    # Scheduler
    SCHEDULE_FIRE = "schedule.fire"
    SCHEDULE_COMPLETE = "schedule.complete"

    # Hook
    HOOK_RUN = "hook.run"

    # Notification
    NOTIFICATION = "notification"


@dataclass
class Event:
    """An event dispatched through the bus."""

    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    run_id: str | None = None
    sequence: int = 0
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


# Type for event handlers
EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """Async in-process event bus with subscription support."""

    def __init__(self) -> None:
        self._handlers: dict[EventType, list[EventHandler]] = defaultdict(list)
        self._global_handlers: list[EventHandler] = []
        self._presentation_handlers: dict[EventType, list[EventHandler]] = defaultdict(list)
        self._presentation_queue: asyncio.Queue[tuple[Event, list[EventHandler]]] = asyncio.Queue(
            256
        )
        self._presentation_worker: asyncio.Task[None] | None = None
        self.presentation_errors: list[str] = []
        self._scope: ContextVar[dict[str, Any] | None] = ContextVar("event_scope", default=None)

    @contextmanager
    def scope(self, session_id: str, run_id: str) -> Iterator[None]:
        """Task-local run identity inherited by its tools, never shared runs."""
        token = self._scope.set({"session_id": session_id, "run_id": run_id, "sequence": 0})
        try:
            yield
        finally:
            self._scope.reset(token)

    def on_presentation(self, event_type: EventType, handler: EventHandler) -> None:
        """Ordered bounded advisory delivery; never subscribe policy here."""
        self._presentation_handlers[event_type].append(handler)

    def _enqueue_presentation(self, event: Event) -> None:
        handlers = list(self._presentation_handlers.get(event.type, []))
        if not handlers:
            return
        if self._presentation_worker is None or self._presentation_worker.done():
            self._presentation_worker = asyncio.create_task(self._present())
        try:
            self._presentation_queue.put_nowait((event, handlers))
        except asyncio.QueueFull:
            self._presentation_failure("Presentation queue exhausted; output is incomplete")
            self._presentation_handlers.clear()

    def _presentation_failure(self, message: str) -> None:
        if len(self.presentation_errors) < 16:
            self.presentation_errors.append(message)

    async def _present(self) -> None:
        while True:
            event, handlers = await self._presentation_queue.get()
            try:
                for handler in handlers:
                    try:
                        await handler(event)
                    except Exception as error:
                        self._presentation_failure(
                            f"Presentation handler failed: {type(error).__name__}"
                        )
            finally:
                self._presentation_queue.task_done()
            if self._presentation_queue.empty():
                return

    async def drain_presentation(self, timeout: float = 2) -> None:
        try:
            await asyncio.wait_for(self._presentation_queue.join(), timeout)
        except TimeoutError:
            self._presentation_failure("Presentation deadline exceeded; output is incomplete")
            await self.close_presentation()

    async def close_presentation(self) -> None:
        if self._presentation_worker is not None:
            self._presentation_worker.cancel()
            await asyncio.gather(self._presentation_worker, return_exceptions=True)
            self._presentation_worker = None
        while not self._presentation_queue.empty():
            self._presentation_queue.get_nowait()
            self._presentation_queue.task_done()

    def on(self, event_type: EventType, handler: EventHandler) -> None:
        """Subscribe to a specific event type."""
        self._handlers[event_type].append(handler)

    def on_all(self, handler: EventHandler) -> None:
        """Subscribe to all events."""
        self._global_handlers.append(handler)

    def off_all(self, handler: EventHandler) -> None:
        if handler in self._global_handlers:
            self._global_handlers.remove(handler)

    def off(self, event_type: EventType, handler: EventHandler) -> None:
        """Unsubscribe from an event type."""
        handlers = self._handlers[event_type]
        if handler in handlers:
            handlers.remove(handler)
        advisory = self._presentation_handlers[event_type]
        if handler in advisory:
            advisory.remove(handler)

    async def emit(self, event: Event) -> None:
        """Emit an event to all subscribers. Errors are logged, never propagated."""
        handlers = list(self._handlers.get(event.type, [])) + list(self._global_handlers)
        scope = self._scope.get()
        if scope is not None:
            scope["sequence"] += 1
        event = replace(
            event,
            data=scrub_metadata(event.data),
            session_id=event.session_id or (scope["session_id"] if scope else None),
            run_id=event.run_id or (scope["run_id"] if scope else None),
            sequence=scope["sequence"] if scope else event.sequence,
        )
        self._enqueue_presentation(event)
        tasks = [asyncio.create_task(_safe_call(h, event)) for h in handlers]
        if tasks:
            await asyncio.gather(*tasks)

    def emit_sync(self, event: Event) -> None:
        """Emit an event from a sync context (fire and forget)."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.emit(event))
        except RuntimeError:
            # No running loop — skip
            pass


async def _safe_call(handler: EventHandler, event: Event) -> None:
    """Call a handler, catching exceptions to prevent bus disruption."""
    try:
        await handler(event)
    except Exception:
        # Log but never propagate — bus must not disrupt callers.
        # Fall back to stdlib logging when structlog is unavailable (A-5).
        try:
            import structlog

            logger = structlog.get_logger()
            logger.warning(
                "event_handler_error",
                event_type=event.type,
                handler=handler.__qualname__,
                exc_info=True,
            )
        except ImportError:
            import logging

            logging.getLogger(__name__).warning(
                "event handler %s raised for %s",
                handler.__qualname__,
                event.type,
                exc_info=True,
            )


# Global singleton
_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Get the global event bus singleton."""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
