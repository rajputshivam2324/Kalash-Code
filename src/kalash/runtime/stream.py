"""Incremental output handling.

Processes StreamEvent objects from the model gateway, accumulating
partial responses and building complete ContentBlock lists.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from kalash.models.normalize import (
    BlockDelta,
    BlockMetadata,
    BlockStart,
    BlockStop,
    ContentBlock,
    MessageStart,
    MessageStop,
    StopReason,
    StreamError,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UsageUpdate,
)

logger = logging.getLogger(__name__)


def _parse_tool_json(raw: str) -> dict[str, Any]:
    """Accept JSON objects only. The model must correct invalid arguments."""
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"_raw": raw}
    return value if isinstance(value, dict) else {"_raw": raw}


@dataclass
class _BlockAccumulator:
    """Accumulates deltas for a single content block."""

    index: int
    block_type: str
    tool_use_id: str | None = None
    tool_name: str | None = None
    thought_signature: Any = None
    chunks: list[str] = field(default_factory=list)
    complete: bool = False
    signature: str = ""

    def append_delta(self, delta: str) -> None:
        self.chunks.append(delta)

    @property
    def text(self) -> str:
        return "".join(self.chunks)

    def to_content_block(self) -> ContentBlock:
        """Build the final ContentBlock from accumulated data."""
        match self.block_type:
            case "text":
                return TextBlock(text=self.text)
            case "tool_use":
                # Preserve invalid arguments as a rejected call
                raw_input = self.text
                parsed_input = _parse_tool_json(raw_input)
                return ToolUseBlock(
                    id=self.tool_use_id or "",
                    name=self.tool_name or "",
                    input=parsed_input,
                    thought_signature=self.thought_signature,
                )
            case "thinking":
                return ThinkingBlock(thinking=self.text, signature=self.signature or None)
            case _:
                # Fallback: treat as text
                return TextBlock(text=self.text)


# ---------------------------------------------------------------------------
# Stream result
# ---------------------------------------------------------------------------


@dataclass
class StreamResult:
    """Final result after processing a complete stream."""

    message_id: str
    model: str
    content: list[ContentBlock]
    stop_reason: StopReason
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    error: StreamError | None = None
    usage_observed: bool = False
    usage_unknown: bool = False
    attempt_id: str = ""

    @property
    def tool_calls(self) -> list[ToolUseBlock]:
        """Extract all tool use blocks from the response."""
        return [b for b in self.content if isinstance(b, ToolUseBlock)]

    @property
    def has_tool_calls(self) -> bool:
        return any(isinstance(b, ToolUseBlock) for b in self.content)

    @property
    def text_content(self) -> str:
        """Concatenate all text blocks."""
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))


# ---------------------------------------------------------------------------
# Stream handler
# ---------------------------------------------------------------------------


@dataclass
class StreamHandler:
    """Processes StreamEvent objects and builds a StreamResult.

    Usage::

        handler = StreamHandler()
        async for event in gateway.stream(messages):
            handler.feed(event)
            if handler.is_complete:
                break
        result = handler.result()
    """

    # Callback for real-time text deltas (e.g. TUI display)
    on_text_delta: Any | None = None  # Callable[[str], None] | None
    on_tool_start: Any | None = None  # Callable[[str, str], None] | None

    # Internal state
    _message_id: str = field(init=False, default="")
    _model: str = field(init=False, default="")
    _blocks: dict[int, _BlockAccumulator] = field(init=False, default_factory=dict)
    _stop_reason: StopReason = field(init=False, default=StopReason.UNKNOWN)
    _usage: UsageUpdate = field(init=False, default_factory=UsageUpdate)
    _error: StreamError | None = field(init=False, default=None)
    _complete: bool = field(init=False, default=False)
    _usage_observed: bool = field(init=False, default=False)

    @property
    def is_complete(self) -> bool:
        return self._complete

    def feed(self, event: StreamEvent) -> None:
        """Process a single stream event."""
        match event:
            case MessageStart(id=msg_id, model=model):
                self._handle_message_start(msg_id, model)
            case BlockStart() as bs:
                self._handle_block_start(bs)
            case BlockDelta() as bd:
                self._handle_block_delta(bd)
            case BlockMetadata() as metadata:
                block = self._blocks.get(metadata.index)
                if block is None or block.complete:
                    self._error = StreamError(
                        "Metadata for an absent or closed block", "invalid_stream"
                    )
                else:
                    if metadata.tool_use_id:
                        block.tool_use_id = (block.tool_use_id or "") + metadata.tool_use_id
                    if metadata.tool_name:
                        block.tool_name = (block.tool_name or "") + metadata.tool_name
                    if metadata.signature:
                        block.signature += metadata.signature
            case BlockStop() as bstop:
                self._handle_block_stop(bstop)
            case UsageUpdate() as usage:
                self._handle_usage(usage)
            case MessageStop(stop_reason=reason):
                self._handle_message_stop(reason)
            case StreamError() as err:
                self._handle_error(err)

    def result(self) -> StreamResult:
        """Build the final StreamResult. Call after stream completes."""
        error = self._error
        if error is None:
            if not self._complete or self._stop_reason == StopReason.UNKNOWN:
                error = StreamError(
                    error=(
                        "Provider stream ended without a recognized finish reason. "
                        "No tools were executed; retry the request after checking the provider."
                    ),
                    code="incomplete_stream",
                )
            elif self._stop_reason == StopReason.MAX_TOKENS:
                error = StreamError(
                    error=(
                        "Model output was truncated at the generation token limit (max_tokens). "
                        "No tools were executed; narrow the requested work or explicitly "
                        "adjust the output/reasoning settings within the available budget."
                    ),
                    code="output_truncated",
                )
            elif self._stop_reason == StopReason.CONTENT_FILTER:
                error = StreamError(
                    error="Provider stopped for content_filter; no tools were executed.",
                    code="content_filter",
                )
            elif any(not block.complete for block in self._blocks.values()):
                error = StreamError(
                    error="Provider ended with unclosed content blocks; no tools were executed.",
                    code="incomplete_stream",
                )
        # Build content blocks in index order
        content: list[ContentBlock] = []
        for idx in sorted(self._blocks.keys()):
            acc = self._blocks[idx]
            content.append(acc.to_content_block())
        calls = [b for b in content if isinstance(b, ToolUseBlock)]
        if error is None and (
            any(not call.id or not call.name for call in calls)
            or len({call.id for call in calls}) != len(calls)
        ):
            error = StreamError(
                "Missing or duplicate tool identity; no tools were executed.",
                "invalid_tool_identity",
            )

        return StreamResult(
            message_id=self._message_id,
            model=self._model,
            content=content,
            stop_reason=self._stop_reason,
            input_tokens=self._usage.input_tokens,
            output_tokens=self._usage.output_tokens,
            cache_read_tokens=self._usage.cache_read_tokens,
            cache_write_tokens=self._usage.cache_write_tokens,
            reasoning_tokens=self._usage.reasoning_tokens,
            error=error,
            usage_observed=self._usage_observed,
        )

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _handle_message_start(self, msg_id: str, model: str) -> None:
        self._message_id = msg_id
        self._model = model

    def _handle_block_start(self, event: BlockStart) -> None:
        if event.index in self._blocks:
            self._error = StreamError("Duplicate content block start", "invalid_stream")
            return
        acc = _BlockAccumulator(
            index=event.index,
            block_type=event.block_type,
            tool_use_id=event.tool_use_id,
            tool_name=event.tool_name,
            thought_signature=event.thought_signature,
        )
        self._blocks[event.index] = acc

        # Notify tool start callback
        if event.block_type == "tool_use" and self.on_tool_start:
            self.on_tool_start(event.tool_name or "", event.tool_use_id or "")

    def _handle_block_delta(self, event: BlockDelta) -> None:
        acc = self._blocks.get(event.index)
        if acc is None:
            logger.warning("block_delta for unknown index %d", event.index)
            return

        acc.append_delta(event.delta)

        # Notify text delta callback
        if acc.block_type == "text" and self.on_text_delta:
            self.on_text_delta(event.delta)

    def _handle_block_stop(self, event: BlockStop) -> None:
        acc = self._blocks.get(event.index)
        if acc:
            acc.complete = True

    def _handle_usage(self, event: UsageUpdate) -> None:
        self._usage_observed = True
        self._usage = self._usage.merge(event)

    def _handle_message_stop(self, reason: StopReason) -> None:
        self._stop_reason = reason
        self._complete = True

    def _handle_error(self, error: StreamError) -> None:
        self._error = error
        if not error.recoverable:
            self._complete = True
