"""Versioned message storage and recovery of interrupted tool exchanges."""

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
            return {
                "t": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
                "thought_signature": block.thought_signature,
            }
        case ToolResultBlock():
            content = block.content
            if not isinstance(content, str):
                # Nested blocks flatten to text; the structure carries no meaning
                # the model needs on replay.
                content = "\n".join(b.text for b in content if isinstance(b, TextBlock))
            return {
                "t": "tool_result",
                "tool_use_id": block.tool_use_id,
                "content": content,
                "is_error": block.is_error,
                "evidence": block.evidence,
            }
        case ThinkingBlock():
            return {"t": "thinking", "thinking": block.thinking, "signature": block.signature}
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
                    thought_signature=raw.get("thought_signature"),
                )
            case "tool_result":
                return ToolResultBlock(
                    tool_use_id=str(raw["tool_use_id"]),
                    content=str(raw.get("content", "")),
                    is_error=bool(raw.get("is_error", False)),
                    evidence=dict(raw.get("evidence") or {}),
                )
            case "thinking":
                return ThinkingBlock(
                    thinking=str(raw.get("thinking", "")), signature=raw.get("signature")
                )
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

    return _repair_interrupted_tools(messages)


def _repair_interrupted_tools(messages: list[Message]) -> list[Message]:
    """Preserve intent and mark missing durable outcomes; never replay effects."""
    repaired: list[Message] = []
    pending: list[str] = []
    for message in messages:
        if pending:
            results = message.content if message.role == Role.USER else []
            answered = {b.tool_use_id for b in results if isinstance(b, ToolResultBlock)}
            missing: list[ContentBlock] = [
                ToolResultBlock(
                    tool_use_id=call_id,
                    content="No durable result. Outcome unknown; inspect workspace before retrying.",
                    is_error=True,
                )
                for call_id in pending
                if call_id not in answered
            ]
            if missing:
                if message.role == Role.USER:
                    message = Message(role=Role.USER, content=[*missing, *message.content])
                else:
                    repaired.append(Message(role=Role.USER, content=missing))
        repaired.append(message)
        pending = [b.id for b in message.content if isinstance(b, ToolUseBlock)]
    if pending:
        repaired.append(
            Message(
                role=Role.USER,
                content=[
                    ToolResultBlock(
                        tool_use_id=call_id,
                        content="No durable result. Outcome unknown; inspect workspace before retrying.",
                        is_error=True,
                    )
                    for call_id in pending
                ],
            )
        )
    return repaired


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
