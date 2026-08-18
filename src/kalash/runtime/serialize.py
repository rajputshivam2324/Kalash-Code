"""Message serialization — the session resume read path.

``SessionRepository.get_session_messages()`` returns exactly the rows a resume
needs, ordered by turn then message sequence. It had zero call sites, because
nothing existed to turn those rows back into
:class:`~kalash.models.normalize.Message` objects. Every run therefore started
with an empty history, and ``kalash resume`` restored a session's *metadata*
while silently discarding its conversation.

This module is both halves of that round trip. It is deliberately explicit
rather than using ``pickle`` or ``dataclasses.asdict``: the stored form is a
durable format that has to survive code changes, so each block type is written
and read by name.

Tool-use and tool-result blocks matter most. A resumed conversation that drops a
``tool_use`` block but keeps the assistant text leaves the provider with a
dangling reference and the request is rejected, so the pairing has to survive
storage intact.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kalash.models.normalize import (
    ContentBlock,
    ImageBlock,
    MemoryBlock,
    Message,
    Role,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

logger = logging.getLogger(__name__)

# Bumped when the stored shape changes incompatibly.
FORMAT_VERSION = 1


def _block_to_json(block: ContentBlock) -> dict[str, Any] | None:
    """Convert one block to a JSON-safe dict, or None if not persistable."""
    match block:
        case TextBlock():
            return {"t": "text", "text": block.text}
        case ToolUseBlock():
            return {"t": "tool_use", "id": block.id, "name": block.name, "input": block.input}
        case ToolResultBlock():
            content = block.content
            if not isinstance(content, str):
                # Nested blocks flatten to text; the structure carries no meaning
                # the model needs on replay.
                content = "\n".join(
                    b.text for b in content if isinstance(b, TextBlock)
                )
            return {
                "t": "tool_result",
                "tool_use_id": block.tool_use_id,
                "content": content,
                "is_error": block.is_error,
            }
        case ThinkingBlock():
            # Reasoning payloads are provider-specific and not portable across a
            # model switch, so the signature is intentionally dropped.
            return {"t": "thinking", "thinking": block.thinking}
        case ImageBlock():
            return {
                "t": "image",
                "source_type": block.source_type,
                "media_type": block.media_type,
                "data": block.data,
            }
        case MemoryBlock():
            return {
                "t": "memory",
                "memory_id": block.memory_id,
                "content": block.content,
                "source": block.source,
                "relevance": block.relevance,
            }
        case _:
            return None


def _block_from_json(raw: dict[str, Any]) -> ContentBlock | None:
    """Rebuild one block, or None if the record is unusable."""
    kind = raw.get("t")
    try:
        match kind:
            case "text":
                return TextBlock(text=str(raw.get("text", "")))
            case "tool_use":
                return ToolUseBlock(
                    id=str(raw["id"]),
                    name=str(raw["name"]),
                    input=dict(raw.get("input") or {}),
                )
            case "tool_result":
                return ToolResultBlock(
                    tool_use_id=str(raw["tool_use_id"]),
                    content=str(raw.get("content", "")),
                    is_error=bool(raw.get("is_error", False)),
                )
            case "thinking":
                return ThinkingBlock(thinking=str(raw.get("thinking", "")))
            case "image":
                return ImageBlock(
                    source_type=str(raw["source_type"]),
                    media_type=str(raw["media_type"]),
                    data=str(raw["data"]),
                )
            case "memory":
                return MemoryBlock(
                    memory_id=str(raw["memory_id"]),
                    content=str(raw.get("content", "")),
                    source=str(raw.get("source", "")),
                    relevance=float(raw.get("relevance", 1.0)),
                )
            case _:
                return None
    except (KeyError, TypeError, ValueError):
        return None


def serialize_blocks(blocks: list[ContentBlock]) -> str:
    """Serialize content blocks for storage."""
    payload = {
        "v": FORMAT_VERSION,
        "blocks": [j for b in blocks if (j := _block_to_json(b)) is not None],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def deserialize_blocks(text: str | None) -> list[ContentBlock]:
    """Rebuild content blocks from storage, tolerating older plain-text rows."""
    if not text:
        return []
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        # Rows written before this format existed held bare text.
        return [TextBlock(text=text)]

    if not isinstance(payload, dict) or "blocks" not in payload:
        return [TextBlock(text=text)]

    blocks: list[ContentBlock] = []
    for raw in payload.get("blocks") or []:
        if isinstance(raw, dict) and (block := _block_from_json(raw)) is not None:
            blocks.append(block)
    return blocks


def rehydrate_messages(rows: list[dict[str, Any]]) -> list[Message]:
    """Turn stored message rows into a conversation history.

    Rows arrive ordered by turn sequence then message sequence. Consecutive rows
    with the same role are merged, because the provider APIs reject two adjacent
    messages with the same role and a resumed session must not produce one.
    """
    messages: list[Message] = []

    for row in rows:
        role_text = str(row.get("role") or "").lower()
        if role_text == "assistant":
            role = Role.ASSISTANT
        elif role_text in ("user", "tool", "tool_result"):
            role = Role.USER
        else:
            # System content is rebuilt from the prompt each turn, never replayed.
            continue

        blocks = deserialize_blocks(row.get("content"))
        if not blocks:
            continue

        if messages and messages[-1].role == role:
            merged = [*messages[-1].content, *blocks]
            messages[-1] = Message(role=role, content=merged)
        else:
            messages.append(Message(role=role, content=blocks))

    return _drop_dangling_tool_calls(messages)


def _drop_dangling_tool_calls(messages: list[Message]) -> list[Message]:
    """Remove tool_use blocks whose results were never stored.

    A truncated or interrupted session can persist an assistant turn containing
    a ``tool_use`` block without the matching ``tool_result``. Providers reject
    that pairing, so replaying it would make a resumed session permanently
    unusable. Dropping the unanswered call is the recoverable choice.
    """
    answered: set[str] = set()
    for message in messages:
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                answered.add(block.tool_use_id)

    cleaned: list[Message] = []
    for message in messages:
        kept: list[ContentBlock] = [
            block
            for block in message.content
            if not (isinstance(block, ToolUseBlock) and block.id not in answered)
        ]
        if len(kept) != len(message.content):
            logger.debug("dropped %d unanswered tool call(s) on resume",
                         len(message.content) - len(kept))
        if kept:
            cleaned.append(Message(role=message.role, content=kept))

    return cleaned


def split_system(messages: list[Message]) -> tuple[str, list[Message]]:
    """Separate system content from the conversation.

    The assembler emits system content as ``Role.SYSTEM`` messages, but both
    provider APIs take the system prompt as a separate top-level parameter. It
    also emits literal ``<cache_breakpoint/>`` text messages that were meant to
    be translated by the gateway and never were — those are dropped here rather
    than sent to the model as content.
    """
    system_parts: list[str] = []
    conversation: list[Message] = []

    for message in messages:
        if message.role != Role.SYSTEM:
            conversation.append(message)
            continue
        for block in message.content:
            if isinstance(block, TextBlock):
                text = block.text.strip()
                if text and text != "<cache_breakpoint/>":
                    system_parts.append(text)

    return "\n\n".join(system_parts), conversation
