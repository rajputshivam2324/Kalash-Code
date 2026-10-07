"""Render runtime events into the terminal transcript."""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING

from kalash.tui.formatting import summarize_tool_call, summarize_tool_result
from kalash.tui.messages import SubagentLine, SystemMessage, ToolCallLine

if TYPE_CHECKING:
    from collections.abc import Callable

    from kalash.core.events import EventBus
    from kalash.tui.app import KalashApp


def subscribe_tool_events(app: KalashApp, bus: EventBus) -> Callable[[], None]:
    from kalash.core.events import Event, EventType

    reported_errors = 0

    async def on_start(event: Event) -> None:
        name = event.data.get("tool_name", "?")
        label = summarize_tool_call(name, event.data.get("arguments") or {})
        line = ToolCallLine(label, tool_name=name)
        tool_use_id = str(event.data.get("tool_use_id", ""))
        if tool_use_id:
            app._active_tools[tool_use_id] = line  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
            app._active_tool_started[tool_use_id] = time.monotonic()  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        app._active_tool = line  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        app._finish_assistant()  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        await app._post(line)  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.

    async def on_output(event: Event) -> None:
        tool_use_id = str(event.data.get("tool_use_id", ""))
        chunk = str(event.data.get("chunk", ""))
        if not chunk:
            return
        line = app._active_tools.get(tool_use_id) if tool_use_id else app._active_tool  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        if line is not None:
            line.append_output(chunk)

    async def on_complete(event: Event) -> None:
        nonlocal reported_errors
        await bus.drain_presentation()
        for error in bus.presentation_errors[reported_errors:]:
            await app._post(SystemMessage(error, error=True))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        reported_errors = len(bus.presentation_errors)
        tool_use_id = str(event.data.get("tool_use_id", ""))
        tool_name = str(event.data.get("tool_name", ""))
        line = app._active_tools.get(tool_use_id)  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        if not tool_use_id and app._active_tool and not app._active_tools:  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
            line = app._active_tool  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        if line is None:
            return
        exit_code = event.data.get("exit_code")
        try:
            exit_code_i = int(exit_code) if exit_code is not None else None
        except (TypeError, ValueError):
            exit_code_i = None
        with contextlib.suppress(Exception):
            line.finish(
                failed=bool(event.data.get("error"))
                or event.data.get("ok") is False
                or (exit_code_i not in (None, 0)),
                duration_ms=int(event.data.get("duration_ms") or 0),
                exit_code=exit_code_i,
            )
        diff_text = event.data.get("diff")
        if diff_text:
            with contextlib.suppress(Exception):
                line.set_diff(
                    str(event.data.get("path", "")),
                    str(diff_text),
                    str(event.data.get("diff_stat", "")),
                )
        summary = summarize_tool_result(tool_name, event.data)
        if summary:
            with contextlib.suppress(Exception):
                line.set_summary(summary)
        if tool_use_id:
            app._active_tools.pop(tool_use_id, None)  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
            app._active_tool_started.pop(tool_use_id, None)  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        app._active_tool = next(reversed(app._active_tools.values()), None)  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.

    async def on_spawn(event: Event) -> None:
        brief = str(event.data.get("brief") or event.data.get("agent") or "task")
        line = SubagentLine(brief[:70])
        app._subagents[str(event.data.get("child_run_id", brief))] = line  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        await app._post(line)  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.

    async def on_agent_complete(event: Event) -> None:
        key = str(event.data.get("child_run_id", ""))
        line = app._subagents.pop(key, None)  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        if line is None:
            return
        with contextlib.suppress(Exception):
            line.finish(
                failed=str(event.data.get("status", "")) != "completed",
                turns=int(event.data.get("turns") or 0),
                tokens=int(event.data.get("tokens") or 0),
            )

    async def on_denied(event: Event) -> None:
        tool_use_id = str(event.data.get("tool_use_id", ""))
        line = app._active_tools.pop(tool_use_id, None) if tool_use_id else app._active_tool  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        app._active_tool_started.pop(tool_use_id, None)  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        app._active_tool = next(reversed(app._active_tools.values()), None)  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        if line is not None:
            with contextlib.suppress(Exception):
                line.finish(failed=True)
        await app._post(  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
            SystemMessage(f"refused: {event.data.get('reason', 'not permitted')}", error=True)
        )

    async def on_turn_start(event: Event) -> None:
        await app._begin_assistant()  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        app._react_step = int(event.data.get("react_step") or event.data.get("iteration") or 0)  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        fill = event.data.get("fill_ratio")
        if fill is not None:
            app._context_fill = float(fill)  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        elif event.data.get("prompt_tokens") and event.data.get("context_window"):
            app._context_fill = float(event.data["prompt_tokens"]) / float(  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
                event.data["context_window"]
            )
        app._refresh_statusline()  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.

    async def on_budget(event: Event) -> None:
        fill = event.data.get("fill_ratio")
        if fill is not None:
            app._context_fill = float(fill)  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        elif event.data.get("prompt_tokens") and event.data.get("context_window"):
            app._context_fill = float(event.data["prompt_tokens"]) / float(  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
                event.data["context_window"]
            )
        app._refresh_statusline()  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.

    handlers = [
        (EventType.TOOL_START, on_start),
        (EventType.TOOL_OUTPUT, on_output),
        (EventType.TOOL_COMPLETE, on_complete),
        (EventType.TOOL_DENIED, on_denied),
        (EventType.AGENT_SPAWN, on_spawn),
        (EventType.AGENT_COMPLETE, on_agent_complete),
        (EventType.TURN_START, on_turn_start),
        (EventType.BUDGET_WARNING, on_budget),
        (EventType.BUDGET_SOFT_LIMIT, on_budget),
    ]
    for event_type, handler in handlers:
        if event_type == EventType.TOOL_OUTPUT:
            bus.on_presentation(event_type, handler)
        else:
            bus.on(event_type, handler)

    def unsubscribe() -> None:
        for event_type, handler in handlers:
            bus.off(event_type, handler)

    return unsubscribe
