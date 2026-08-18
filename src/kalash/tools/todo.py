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

import time
from typing import Any

from pydantic import BaseModel, Field

from kalash.core.ids import generate_id
from kalash.tools.base import (
    SideEffect,
    ToolContext,
    ToolEnvelope,
)


# ---------------------------------------------------------------------------
# In-memory task store (per-session, managed by orchestration layer)
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


# Session-scoped task lists: session_id -> TaskList
_session_lists: dict[str, TaskList] = {}


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

        _session_lists[ctx.session_id] = task_list

        import json

        return ToolEnvelope.success(
            content=json.dumps(task_list.to_dict(), indent=2),
            metadata={"task_list_id": task_list.id, "task_count": len(task_list.tasks)},
        )

    def _add(self, args: TodoParams, ctx: ToolContext) -> ToolEnvelope:
        task_list = _session_lists.get(ctx.session_id)
        if not task_list:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message="No task list exists. Create one first.",
                recoverable=True,
            )

        for task_def in args.tasks:
            item = TaskItem(description=task_def.task_description, details=task_def.details)
            task_list.tasks.append(item)

        import json

        return ToolEnvelope.success(
            content=json.dumps(task_list.to_dict(), indent=2),
            metadata={"added": len(args.tasks)},
        )

    def _complete(self, args: TodoParams, ctx: ToolContext) -> ToolEnvelope:
        task_list = _session_lists.get(ctx.session_id)
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

        import json

        return ToolEnvelope.success(
            content=json.dumps(task_list.to_dict(), indent=2),
            metadata={
                "completed_count": completed_count,
                "context_update": args.context_update,
            },
        )

    def _remove(self, args: TodoParams, ctx: ToolContext) -> ToolEnvelope:
        task_list = _session_lists.get(ctx.session_id)
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

        import json

        return ToolEnvelope.success(
            content=json.dumps(task_list.to_dict(), indent=2),
            metadata={"removed_count": removed},
        )

    def _list(self, ctx: ToolContext) -> ToolEnvelope:
        task_list = _session_lists.get(ctx.session_id)
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
