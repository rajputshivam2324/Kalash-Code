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
    BlockStart,
    BlockStop,
    ContentBlock,
    MessageStart,
    MessageStop,
    StreamError,
    StreamEvent,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UsageUpdate,
)

logger = logging.getLogger(__name__)


def _repair_tool_json(raw: str) -> dict[str, Any]:
    """Multi-layer JSON repair for LLM tool call arguments.
    
    Layer 1: Strip markdown fences and surrounding noise.
    Layer 2: Attempt standard json.loads.
    Layer 3: Use json_repair library for syntax fixes.
    Layer 4: Fall back to {"_raw": raw} as last resort.
    """
    if not raw or not raw.strip():
        return {}
    
    # Layer 1: Strip markdown code fences
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove first line (```json) and last line (```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    
    # Layer 2: Standard parse
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
        return {"_raw": raw}
    except (json.JSONDecodeError, ValueError):
        pass
    
    # Layer 3: json_repair
    try:
        from json_repair import loads as repair_loads
        result = repair_loads(cleaned)
        if isinstance(result, dict):
            logger.info("Repaired malformed tool call JSON (len=%d)", len(raw))
            return result
    except Exception:
        pass
    
    # Layer 4: Try closing unclosed braces manually
    try:
        patched = cleaned
        open_braces = patched.count("{") - patched.count("}")
        open_brackets = patched.count("[") - patched.count("]")
        if open_braces > 0:
            patched += "}" * open_braces
        if open_brackets > 0:
            patched += "]" * open_brackets
        result = json.loads(patched)
        if isinstance(result, dict):
            logger.info("Repaired truncated tool call JSON by closing %d brace(s)", open_braces)
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    
    logger.warning("Could not repair tool call JSON (len=%d): %.100s...", len(raw), raw)
    return {"_raw": raw}


# ---------------------------------------------------------------------------
# Accumulator for a single block
# ---------------------------------------------------------------------------


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
                # Parse accumulated JSON input with repair pipeline
                raw_input = self.text
                parsed_input = _repair_tool_json(raw_input)
                return ToolUseBlock(
                    id=self.tool_use_id or "",
                    name=self.tool_name or "",
                    input=parsed_input,
                    thought_signature=self.thought_signature,
                )
            case "thinking":
                return ThinkingBlock(thinking=self.text)
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
    error: StreamError | None = None

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
        return "".join(
            b.text for b in self.content if isinstance(b, TextBlock)
        )


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
    _input_tokens: int = field(init=False, default=0)
    _output_tokens: int = field(init=False, default=0)
    _cache_read_tokens: int = field(init=False, default=0)
    _cache_write_tokens: int = field(init=False, default=0)
    _error: StreamError | None = field(init=False, default=None)
    _complete: bool = field(init=False, default=False)

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
        # Build content blocks in index order
        content: list[ContentBlock] = []
        for idx in sorted(self._blocks.keys()):
            acc = self._blocks[idx]
            content.append(acc.to_content_block())

        return StreamResult(
            message_id=self._message_id,
            model=self._model,
            content=content,
            stop_reason=self._stop_reason,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            cache_read_tokens=self._cache_read_tokens,
            cache_write_tokens=self._cache_write_tokens,
            error=self._error,
        )

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _handle_message_start(self, msg_id: str, model: str) -> None:
        self._message_id = msg_id
        self._model = model

    def _handle_block_start(self, event: BlockStart) -> None:
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
        self._input_tokens = event.input_tokens
        self._output_tokens = event.output_tokens
        self._cache_read_tokens = event.cache_read_tokens
        self._cache_write_tokens = event.cache_write_tokens

    def _handle_message_stop(self, reason: StopReason) -> None:
        self._stop_reason = reason
        self._complete = True

    def _handle_error(self, error: StreamError) -> None:
        self._error = error
        if not error.recoverable:
            self._complete = True
