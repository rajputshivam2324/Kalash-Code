"""Hook event enum and typed payload schemas.

Defines all hook trigger points and the structured payloads
delivered to hook handlers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class HookEvent(StrEnum):
    """All hook trigger events in Kalash."""

    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    PRE_FILE_EDIT = "PreFileEdit"
    POST_FILE_EDIT = "PostFileEdit"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    PRE_RECALL = "PreRecall"
    POST_RECALL = "PostRecall"
    PRE_MEMORY_WRITE = "PreMemoryWrite"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    PRE_TASK_EXEC = "PreTaskExec"
    POST_TASK_EXEC = "PostTaskExec"
    SCHEDULE_TRIGGER = "ScheduleTrigger"
    NOTIFICATION = "Notification"
    STOP = "Stop"
    PLUGIN_LOAD = "PluginLoad"


@dataclass
class HookPayload:
    """Base payload delivered to all hook handlers."""

    event: HookEvent
    session_id: str | None = None
    run_id: str | None = None
    timestamp: str = ""  # ISO 8601 UTC
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionStartPayload(HookPayload):
    """Payload for SessionStart event."""

    project_dir: str = ""
    agent: str = "default"


@dataclass
class SessionEndPayload(HookPayload):
    """Payload for SessionEnd event."""

    reason: str = ""  # "user_exit" | "budget_exceeded" | "error"
    duration_ms: int = 0


@dataclass
class UserPromptSubmitPayload(HookPayload):
    """Payload for UserPromptSubmit event."""

    prompt_text: str = ""
    # Hook can modify or block this prompt


@dataclass
class ToolUsePayload(HookPayload):
    """Payload for PreToolUse and PostToolUse events."""

    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    result: Any = None  # Only for PostToolUse
    duration_ms: int | None = None  # Only for PostToolUse
    blocked: bool = False  # Only for PreToolUse (set by hook)


@dataclass
class FileEditPayload(HookPayload):
    """Payload for PreFileEdit and PostFileEdit events."""

    path: str = ""
    operation: str = ""  # "create" | "edit" | "delete"
    content_preview: str = ""  # First N chars for inspection


@dataclass
class CompactPayload(HookPayload):
    """Payload for PreCompact and PostCompact events."""

    tokens_before: int = 0
    tokens_after: int = 0  # Only for PostCompact
    strategy: str = ""


@dataclass
class RecallPayload(HookPayload):
    """Payload for PreRecall and PostRecall events."""

    query: str = ""
    results_count: int = 0  # Only for PostRecall
    providers: list[str] = field(default_factory=list)


@dataclass
class MemoryWritePayload(HookPayload):
    """Payload for PreMemoryWrite event."""

    memory_type: str = ""  # "episode" | "entity" | "preference"
    content_preview: str = ""


@dataclass
class SubagentPayload(HookPayload):
    """Payload for SubagentStart and SubagentStop events."""

    child_run_id: str = ""
    parent_run_id: str = ""
    agent: str = ""
    depth: int = 0
    status: str = ""  # Only for SubagentStop


@dataclass
class TaskExecPayload(HookPayload):
    """Payload for PreTaskExec and PostTaskExec events."""

    task_id: str = ""
    task_description: str = ""
    status: str = ""  # Only for PostTaskExec


@dataclass
class ScheduleTriggerPayload(HookPayload):
    """Payload for ScheduleTrigger event."""

    schedule_id: str = ""
    schedule_name: str = ""
    cron: str = ""


@dataclass
class NotificationPayload(HookPayload):
    """Payload for Notification event."""

    level: str = "info"  # "info" | "warning" | "error"
    title: str = ""
    body: str = ""


@dataclass
class StopPayload(HookPayload):
    """Payload for Stop event."""

    reason: str = ""  # "user_request" | "budget" | "error"


@dataclass
class PluginLoadPayload(HookPayload):
    """Payload for PluginLoad event."""

    plugin_name: str = ""
    plugin_version: str = ""
    manifest_path: str = ""


# Map event type to its payload class
PAYLOAD_REGISTRY: dict[HookEvent, type[HookPayload]] = {
    HookEvent.SESSION_START: SessionStartPayload,
    HookEvent.SESSION_END: SessionEndPayload,
    HookEvent.USER_PROMPT_SUBMIT: UserPromptSubmitPayload,
    HookEvent.PRE_TOOL_USE: ToolUsePayload,
    HookEvent.POST_TOOL_USE: ToolUsePayload,
    HookEvent.PRE_FILE_EDIT: FileEditPayload,
    HookEvent.POST_FILE_EDIT: FileEditPayload,
    HookEvent.PRE_COMPACT: CompactPayload,
    HookEvent.POST_COMPACT: CompactPayload,
    HookEvent.PRE_RECALL: RecallPayload,
    HookEvent.POST_RECALL: RecallPayload,
    HookEvent.PRE_MEMORY_WRITE: MemoryWritePayload,
    HookEvent.SUBAGENT_START: SubagentPayload,
    HookEvent.SUBAGENT_STOP: SubagentPayload,
    HookEvent.PRE_TASK_EXEC: TaskExecPayload,
    HookEvent.POST_TASK_EXEC: TaskExecPayload,
    HookEvent.SCHEDULE_TRIGGER: ScheduleTriggerPayload,
    HookEvent.NOTIFICATION: NotificationPayload,
    HookEvent.STOP: StopPayload,
    HookEvent.PLUGIN_LOAD: PluginLoadPayload,
}
