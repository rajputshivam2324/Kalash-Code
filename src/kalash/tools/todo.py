"""Task list tool.

Provides structured task management for multi-step work:
- create: Initialize a new task list
- add: Add tasks to an existing list
- complete: Mark tasks as done
- remove: Remove tasks from the list
- list: Show current tasks and status

Output is structured for UI rendering.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from kalash.core.ids import generate_id
from kalash.tools.base import (
    SideEffect,
    ToolContext,
    ToolEnvelope,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Durable task store (per-session, managed by orchestration layer)
# ---------------------------------------------------------------------------


class TaskItem:
    """A single task in a task list."""

    __slots__ = ("id", "description", "details", "completed", "created_at", "completed_at")

    def __init__(self, description: str, details: str = "") -> None:
        self.id: str = generate_id("tsk_")
        self.description: str = description
        self.details: str = details
        self.completed: bool = False
        self.created_at: float = time.time()
        self.completed_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "details": self.details,
            "completed": self.completed,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


class TaskList:
    """A named task list containing multiple tasks."""

    __slots__ = ("id", "description", "tasks", "created_at")

    def __init__(self, description: str) -> None:
        self.id: str = generate_id("tdl_")
        self.description: str = description
        self.tasks: list[TaskItem] = []
        self.created_at: float = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "tasks": [t.to_dict() for t in self.tasks],
            "total": len(self.tasks),
            "completed": sum(1 for t in self.tasks if t.completed),
            "created_at": self.created_at,
        }


# Session-scoped task lists: session_id -> TaskList.
#
# This is a read-through cache over durable state, not the state itself. The list
# is persisted to the scratchpad on every mutation, so a plan survives a crash,
# a restart, and `kalash --resume`. An in-memory-only plan is worse than no plan:
# the user is invited to rely on it and then loses it exactly when a long build
# gets interrupted.
_session_lists: dict[str, TaskList] = {}

# Filename inside the session's scratchpad directory.
_TODO_STATE = "todo.json"


def _todo_path(session_id: str) -> "Path":
    from kalash.runtime.scratchpad import get_scratchpad

    pad = get_scratchpad(session_id)
    return pad.index_path.parent / f"{pad.index_path.stem}.{_TODO_STATE}"


def _save_list(session_id: str, task_list: TaskList) -> None:
    """Persist a task list. Failures are non-fatal — the plan still works."""
    import json

    try:
        path = _todo_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "id": task_list.id,
            "description": task_list.description,
            "created_at": task_list.created_at,
            "tasks": [t.to_dict() for t in task_list.tasks],
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.debug("could not persist the task list", exc_info=True)


def _load_list(session_id: str) -> TaskList | None:
    """Restore a persisted task list, or None."""
    import json

    try:
        path = _todo_path(session_id)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    try:
        restored = TaskList(str(payload.get("description", "")))
        restored.id = str(payload.get("id") or restored.id)
        restored.created_at = float(payload.get("created_at") or restored.created_at)
        for raw in payload.get("tasks") or []:
            item = TaskItem(
                str(raw.get("description", "")), str(raw.get("details", "") or "")
            )
            item.id = str(raw.get("id") or item.id)
            item.completed = bool(raw.get("completed", False))
            item.created_at = float(raw.get("created_at") or item.created_at)
            item.completed_at = raw.get("completed_at")
            restored.tasks.append(item)
        return restored
    except (TypeError, ValueError):
        return None


def get_task_list(session_id: str) -> TaskList | None:
    """Current task list for a session, loading from disk on first access."""
    if session_id in _session_lists:
        return _session_lists[session_id]
    restored = _load_list(session_id)
    if restored is not None:
        _session_lists[session_id] = restored
    return restored


def set_task_list(session_id: str, task_list: TaskList) -> None:
    """Replace and persist a session's task list."""
    _session_lists[session_id] = task_list
    _save_list(session_id, task_list)


def render_task_list(session_id: str) -> str:
    """Render the plan for the context window and for `/status`."""
    task_list = get_task_list(session_id)
    if task_list is None or not task_list.tasks:
        return ""
    lines = [f"<plan>{task_list.description}"]
    for index, task in enumerate(task_list.tasks, start=1):
        mark = "x" if task.completed else " "
        lines.append(f"[{mark}] {index}. {task.description}")
    done = sum(1 for t in task_list.tasks if t.completed)
    lines.append(f"({done}/{len(task_list.tasks)} complete)")
    lines.append("</plan>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TodoTool
# ---------------------------------------------------------------------------


class TaskDef(BaseModel):
    """Definition of a single task."""

    task_description: str = Field(description="Task description")
    details: str = Field(default="", description="Optional detailed information")


class TodoParams(BaseModel):
    """Parameters for task list operations."""

    command: str = Field(
        description="Operation: 'create', 'add', 'complete', 'remove', or 'list'"
    )
    task_list_description: str = Field(
        default="", description="Description for the task list (required for 'create')"
    )
    tasks: list[TaskDef] = Field(
        default_factory=list, description="Tasks to add (required for 'create' and 'add')"
    )
    completed_task_ids: list[str] = Field(
        default_factory=list, description="Task IDs to mark complete (required for 'complete')"
    )
    remove_task_ids: list[str] = Field(
        default_factory=list, description="Task IDs to remove (required for 'remove')"
    )
    context_update: str = Field(
        default="", description="Context information when completing tasks"
    )


class TodoTool:
    """Task list management for structured multi-step work."""

    @property
    def name(self) -> str:
        return "todo"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return (
            "Manage a task list for multi-step work. "
            "Commands: create, add, complete, remove, list."
        )

    @property
    def params(self) -> type[BaseModel]:
        return TodoParams

    @property
    def side_effect(self) -> SideEffect:
        return SideEffect.NONE

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset()

    @property
    def timeout_s(self) -> float:
        return 5.0

    @property
    def max_output_bytes(self) -> int:
        return 32768

    @property
    def idempotent(self) -> bool:
        return False

    @property
    def cancellable(self) -> bool:
        return False

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:
        return frozenset()

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        assert isinstance(args, TodoParams)

        match args.command:
            case "create":
                return self._create(args, ctx)
            case "add":
                return self._add(args, ctx)
            case "complete":
                return self._complete(args, ctx)
            case "remove":
                return self._remove(args, ctx)
            case "list":
                return self._list(ctx)
            case _:
                return ToolEnvelope.fail(
                    code="KALASH_TOOL_INVALID_ARGS",
                    message=f"Unknown command: '{args.command}'",
                    recoverable=True,
                    remediation="Use one of: create, add, complete, remove, list.",
                )

    def _create(self, args: TodoParams, ctx: ToolContext) -> ToolEnvelope:
        if not args.task_list_description:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_INVALID_ARGS",
                message="task_list_description is required for 'create'.",
                recoverable=True,
            )

        task_list = TaskList(description=args.task_list_description)
        for task_def in args.tasks:
            item = TaskItem(description=task_def.task_description, details=task_def.details)
            task_list.tasks.append(item)

        set_task_list(ctx.session_id, task_list)

        import json

        return ToolEnvelope.success(
            content=json.dumps(task_list.to_dict(), indent=2),
            metadata={"task_list_id": task_list.id, "task_count": len(task_list.tasks)},
        )

    def _add(self, args: TodoParams, ctx: ToolContext) -> ToolEnvelope:
        task_list = get_task_list(ctx.session_id)
        if not task_list:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message="No task list exists. Create one first.",
                recoverable=True,
            )

        for task_def in args.tasks:
            item = TaskItem(description=task_def.task_description, details=task_def.details)
            task_list.tasks.append(item)

        _save_list(ctx.session_id, task_list)
        import json

        return ToolEnvelope.success(
            content=json.dumps(task_list.to_dict(), indent=2),
            metadata={"added": len(args.tasks)},
        )

    def _complete(self, args: TodoParams, ctx: ToolContext) -> ToolEnvelope:
        task_list = get_task_list(ctx.session_id)
        if not task_list:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message="No task list exists.",
                recoverable=True,
            )

        completed_count = 0
        for task_id in args.completed_task_ids:
            for task in task_list.tasks:
                if task.id == task_id and not task.completed:
                    task.completed = True
                    task.completed_at = time.time()
                    completed_count += 1
                    break

        _save_list(ctx.session_id, task_list)
        import json

        return ToolEnvelope.success(
            content=json.dumps(task_list.to_dict(), indent=2),
            metadata={
                "completed_count": completed_count,
                "context_update": args.context_update,
            },
        )

    def _remove(self, args: TodoParams, ctx: ToolContext) -> ToolEnvelope:
        task_list = get_task_list(ctx.session_id)
        if not task_list:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message="No task list exists.",
                recoverable=True,
            )

        remove_set = set(args.remove_task_ids)
        before = len(task_list.tasks)
        task_list.tasks = [t for t in task_list.tasks if t.id not in remove_set]
        removed = before - len(task_list.tasks)

        _save_list(ctx.session_id, task_list)
        import json

        return ToolEnvelope.success(
            content=json.dumps(task_list.to_dict(), indent=2),
            metadata={"removed_count": removed},
        )

    def _list(self, ctx: ToolContext) -> ToolEnvelope:
        task_list = get_task_list(ctx.session_id)
        if not task_list:
            return ToolEnvelope.success(
                content="No task list exists for this session.",
                metadata={"has_list": False},
            )

        import json

        return ToolEnvelope.success(
            content=json.dumps(task_list.to_dict(), indent=2),
            metadata={"has_list": True, "task_list_id": task_list.id},
        )
