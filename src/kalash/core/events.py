"""In-process async event bus.

Sideways communication between peer modules goes through this,
never through direct imports.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


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


# Type for event handlers
EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """Async in-process event bus with subscription support."""

    def __init__(self) -> None:
        self._handlers: dict[EventType, list[EventHandler]] = defaultdict(list)
        self._global_handlers: list[EventHandler] = []

    def on(self, event_type: EventType, handler: EventHandler) -> None:
        """Subscribe to a specific event type."""
        self._handlers[event_type].append(handler)

    def on_all(self, handler: EventHandler) -> None:
        """Subscribe to all events."""
        self._global_handlers.append(handler)

    def off(self, event_type: EventType, handler: EventHandler) -> None:
        """Unsubscribe from an event type."""
        handlers = self._handlers[event_type]
        if handler in handlers:
            handlers.remove(handler)

    async def emit(self, event: Event) -> None:
        """Emit an event to all subscribers. Errors are logged, never propagated."""
        handlers = list(self._handlers.get(event.type, [])) + list(self._global_handlers)
        if not handlers:
            return

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
