"""Normalized message types for the model gateway.

All providers map their wire formats into these types.
Frozen dataclasses ensure messages are immutable once constructed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Role(StrEnum):
    """Message role."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class StopReason(StrEnum):
    """Why the model stopped generating."""

    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    CONTENT_FILTER = "content_filter"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Content blocks
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextBlock:
    """Plain text content."""

    text: str
    type: str = field(default="text", init=False)


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    """A tool call from the assistant."""

    id: str
    name: str
    input: dict[str, Any]
    thought_signature: Any = None
    type: str = field(default="tool_use", init=False)


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    """Result of a tool execution, sent in user messages."""

    tool_use_id: str
    content: str | list[ContentBlock]
    is_error: bool = False
    type: str = field(default="tool_result", init=False)


@dataclass(frozen=True, slots=True)
class ThinkingBlock:
    """Model reasoning/thinking content."""

    thinking: str
    signature: str | None = None
    type: str = field(default="thinking", init=False)


@dataclass(frozen=True, slots=True)
class ImageBlock:
    """Image content (base64 or URL)."""

    source_type: str  # "base64" or "url"
    media_type: str  # e.g. "image/png"
    data: str  # base64 data or URL
    type: str = field(default="image", init=False)


@dataclass(frozen=True, slots=True)
class MemoryBlock:
    """Injected memory context."""

    memory_id: str
    content: str
    source: str  # provider that supplied it
    relevance: float = 1.0
    type: str = field(default="memory", init=False)


@dataclass(frozen=True, slots=True)
class OpaquePayload:
    """Provider-specific data we carry but don't interpret.

    Used for things like Anthropic thinking signatures that must
    be round-tripped but have no normalized meaning.
    """

    provider: str
    model_family: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OpaqueBlock:
    """Opaque provider-specific content block.

    Carries data that must be round-tripped to the same provider
    but is meaningless to the gateway.
    """

    payload: OpaquePayload
    type: str = field(default="opaque", init=False)


# Union of all content block types
ContentBlock = (
    TextBlock
    | ToolUseBlock
    | ToolResultBlock
    | ThinkingBlock
    | ImageBlock
    | MemoryBlock
    | OpaqueBlock
)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Message:
    """A single message in the conversation."""

    role: Role
    content: list[ContentBlock]
    name: str | None = None  # optional participant name


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """Complete (non-streaming) response from a model."""

    id: str
    model: str
    content: list[ContentBlock]
    stop_reason: StopReason
    usage: Any  # kalash.core.budget.Usage — avoid circular import at type level


# ---------------------------------------------------------------------------
# Stream events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MessageStart:
    """Stream started — contains response metadata."""

    id: str
    model: str
    type: str = field(default="message_start", init=False)


@dataclass(frozen=True, slots=True)
class BlockStart:
    """A new content block is beginning."""

    index: int
    block_type: str  # "text", "tool_use", "thinking"
    # For tool_use blocks, these are populated at start
    tool_use_id: str | None = None
    tool_name: str | None = None
    thought_signature: Any = None
    type: str = field(default="block_start", init=False)


@dataclass(frozen=True, slots=True)
class BlockDelta:
    """Incremental content within a block."""

    index: int
    delta: str  # text chunk, partial JSON, thinking chunk
    type: str = field(default="block_delta", init=False)


@dataclass(frozen=True, slots=True)
class BlockStop:
    """A content block finished."""

    index: int
    type: str = field(default="block_stop", init=False)


@dataclass(frozen=True, slots=True)
class UsageUpdate:
    """Incremental usage information during streaming."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    type: str = field(default="usage_update", init=False)


@dataclass(frozen=True, slots=True)
class MessageStop:
    """Stream finished."""

    stop_reason: StopReason
    type: str = field(default="message_stop", init=False)


@dataclass(frozen=True, slots=True)
class StreamError:
    """An error occurred during streaming."""

    error: str
    code: str = "unknown"
    recoverable: bool = False
    type: str = field(default="stream_error", init=False)


# Union of all stream event types
StreamEvent = (
    MessageStart | BlockStart | BlockDelta | BlockStop | UsageUpdate | MessageStop | StreamError
)


# ---------------------------------------------------------------------------
# Model capabilities
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """What a model can do — used for routing and context assembly."""

    context_window: int
    max_output_tokens: int
    tool_use: bool = True
    vision: bool = False
    reasoning: bool = False
    streaming: bool = True
    json_mode: bool = False
    system_messages: bool = True
    stop_sequences: bool = True
    cache_control: bool = False
    parallel_tool_use: bool = True
    extended_thinking: bool = False
    max_thinking_tokens: int | None = None
