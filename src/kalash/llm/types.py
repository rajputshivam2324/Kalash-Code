"""Provider-neutral message, content, usage and stream vocabulary.

Messages are what the session log stores and what adapters serialize. Roles are
``user``, ``assistant`` and ``tool`` (one tool result per message); adapters map them
onto each wire format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["user", "assistant", "tool"]
FinishReason = Literal["stop", "tool_calls", "max_tokens", "content_filter", "refusal"]


@dataclass
class TextBlock:
    text: str
    extra: dict[str, Any] | None = None  # opaque provider data (e.g. a Gemini thoughtSignature)
    type: Literal["text"] = "text"
    # "context" marks text the harness added around what the user typed (instructions, runtime
    # snapshot, notes). The model sees it; UIs and exports show only the user's own words.
    source: str | None = None


@dataclass
class ReasoningBlock:
    """Model reasoning. ``signature``/``redacted`` are only replayed to the producing model."""

    text: str
    signature: str | None = None
    redacted: str | None = None
    extra: dict[str, Any] | None = None  # opaque provider data (e.g. a Responses item id)
    type: Literal["reasoning"] = "reasoning"


@dataclass
class ToolCallBlock:
    """A tool call. ``arguments`` is the raw JSON text exactly as the model produced it."""

    id: str
    name: str
    arguments: str
    extra: dict[str, Any] | None = None  # opaque provider replay data (e.g. Gemini signatures)
    type: Literal["tool_call"] = "tool_call"


@dataclass
class ImageBlock:
    media_type: str
    data: str  # base64
    type: Literal["image"] = "image"


Block = TextBlock | ReasoningBlock | ToolCallBlock | ImageBlock


@dataclass
class Message:
    role: Role
    content: list[Block]
    tool_call_id: str | None = None  # tool role only
    name: str | None = None  # tool role: the tool's name
    is_error: bool = False  # tool role only
    origin: str | None = None  # assistant: "provider/model" that produced it
    # Opaque provider data (raw output items) that is only valid when replayed to the route in
    # ``origin``. Adapters must fall back to rebuilding the message from its blocks otherwise.
    replay: dict[str, Any] | None = None

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    @property
    def visible_text(self) -> str:
        """The text without harness-injected context blocks (what the user actually wrote)."""
        return "".join(
            b.text for b in self.content if isinstance(b, TextBlock) and b.source != "context"
        )

    @property
    def tool_calls(self) -> list[ToolCallBlock]:
        return [b for b in self.content if isinstance(b, ToolCallBlock)]

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "role": self.role,
            "content": [_block_dict(b) for b in self.content],
        }
        if self.tool_call_id is not None:
            data["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            data["name"] = self.name
        if self.is_error:
            data["is_error"] = True
        if self.origin is not None:
            data["origin"] = self.origin
        if self.replay is not None:
            data["replay"] = self.replay
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        return cls(
            role=data["role"],
            content=[_block_from(b) for b in data.get("content", [])],
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
            is_error=bool(data.get("is_error", False)),
            origin=data.get("origin"),
            replay=data.get("replay"),
        )


def user_text(text: str) -> Message:
    return Message("user", [TextBlock(text)])


def tool_result(call: ToolCallBlock, text: str, *, is_error: bool = False) -> Message:
    return Message(
        "tool", [TextBlock(text)], tool_call_id=call.id, name=call.name, is_error=is_error
    )


def _block_dict(block: Block) -> dict[str, Any]:
    return {k: v for k, v in block.__dict__.items() if v is not None}


def _block_from(data: dict[str, Any]) -> Block:
    kind = data.get("type")
    if kind == "text":
        return TextBlock(data.get("text", ""), data.get("extra"), source=data.get("source"))
    if kind == "reasoning":
        return ReasoningBlock(
            data.get("text", ""), data.get("signature"), data.get("redacted"), data.get("extra")
        )
    if kind == "tool_call":
        return ToolCallBlock(data["id"], data["name"], data.get("arguments", ""), data.get("extra"))
    if kind == "image":
        return ImageBlock(data["media_type"], data["data"])
    raise ValueError(f"unknown content block type {kind!r}")


@dataclass
class Usage:
    """Token counts for one provider response. ``None`` means the provider did not report it."""

    input_tokens: int | None = None  # total prompt tokens, including cache reads/writes
    output_tokens: int | None = None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total(self) -> int:
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    def to_dict(self) -> dict[str, int | None]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Usage:
        if not data:
            return cls()
        return cls(
            input_tokens=data.get("input_tokens"),
            output_tokens=data.get("output_tokens"),
            cache_read_tokens=data.get("cache_read_tokens") or 0,
            cache_write_tokens=data.get("cache_write_tokens") or 0,
            reasoning_tokens=data.get("reasoning_tokens") or 0,
        )


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class Request:
    model: str
    system: str
    messages: list[Message]
    tools: list[ToolSpec] = field(default_factory=list)
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    temperature: float | None = None
    session_id: str | None = None  # stable per session; used for provider cache routing


# ---------------------------------------------------------------------------
# Stream events. Text/reasoning deltas are for live display; ``Done`` carries the
# assembled, authoritative message.
# ---------------------------------------------------------------------------


@dataclass
class TextDelta:
    text: str


@dataclass
class ReasoningDelta:
    text: str


@dataclass
class ToolCallStarted:
    index: int
    id: str
    name: str


@dataclass
class Done:
    message: Message
    finish: FinishReason
    usage: Usage


@dataclass
class Retrying:
    """The previous attempt failed before producing output; another attempt follows."""

    attempt: int
    delay: float
    error: str
    discarded: bool = False  # partial output of the failed attempt must be thrown away


@dataclass
class AttemptUsage:
    """Provider-reported usage for a response rejected before it could become history."""

    usage: Usage
    error: str


StreamEvent = TextDelta | ReasoningDelta | ToolCallStarted | Done | Retrying | AttemptUsage
