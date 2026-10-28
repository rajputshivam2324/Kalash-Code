"""Live events the agent emits for UIs. Durable facts live in the session log, not here."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from kalash.llm.types import Usage

Reason = Literal[
    "completed",  # the model finished and nothing blocked it
    "max_tokens",  # the model hit its output limit
    "limit",  # a configured step/token/cost limit stopped the turn
    "cancelled",
    "error",  # provider or runtime failure
    "refused",  # the model declined (refusal / content filter)
    "stuck",  # the same command kept failing with identical output; needs a different approach
]


@dataclass
class TurnStart:
    prompt: str


@dataclass
class TextChunk:
    text: str


@dataclass
class ReasoningChunk:
    text: str


@dataclass
class ToolPending:
    call_id: str
    name: str


@dataclass
class ToolRunning:
    call_id: str
    name: str
    summary: str
    arguments: dict[str, Any]


@dataclass
class ToolOutput:
    call_id: str
    text: str


@dataclass
class ToolDone:
    call_id: str
    name: str
    text: str
    is_error: bool
    denied: bool = False
    diff: dict[str, Any] | None = None  # FileDiff.as_dict() for write/edit; UI only


@dataclass
class Retry:
    attempt: int
    delay: float
    error: str


@dataclass
class Compacted:
    tokens_before: int
    tokens_after: int


@dataclass
class Notice:
    text: str
    level: Literal["info", "warning", "error"] = "info"


@dataclass
class ContextUpdate:
    used: int
    window: int
    usage: Usage
    cost: float | None


@dataclass
class TurnResult:
    reason: Reason
    text: str = ""
    error: str | None = None
    error_code: str | None = None  # LLMError code when the provider failed
    steps: int = 0
    usage: Usage = field(default_factory=Usage)
    cost: float | None = None
    denials: list[str] = field(default_factory=list)
    tool_calls: int = 0


@dataclass
class ModeChanged:
    mode: str
    label: str


@dataclass
class TurnEnd:
    result: TurnResult


AgentEvent = (
    TurnStart
    | TextChunk
    | ReasoningChunk
    | ToolPending
    | ToolRunning
    | ToolOutput
    | ToolDone
    | Retry
    | Compacted
    | Notice
    | ContextUpdate
    | ModeChanged
    | TurnEnd
)
