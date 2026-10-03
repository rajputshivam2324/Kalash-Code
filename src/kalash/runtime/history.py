"""Token estimation and bounded history reduction without splitting tool exchanges."""

from __future__ import annotations

import json
from typing import Any

from kalash.models.normalize import Message, TextBlock, ToolResultBlock, ToolUseBlock


def split_history(messages: list[Message], keep: int = 3) -> tuple[list[Message], list[Message]]:
    cut = max(0, len(messages) - keep)
    pending: set[str] = set()
    safe = 0
    for index, message in enumerate(messages[:cut]):
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                pending.add(block.id)
            elif isinstance(block, ToolResultBlock):
                pending.discard(block.tool_use_id)
        if not pending:
            safe = index + 1
    return messages[:safe], messages[safe:]


def extractive_summary(
    messages: list[Message], max_chars: int = 6_000, block_chars: int = 400
) -> str:
    lines: list[str] = []
    for message in messages:
        parts: list[str] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                parts.append(block.text[:block_chars])
            elif isinstance(block, ToolUseBlock):
                parts.append(f"Called {block.name}: {json.dumps(block.input)[:block_chars]}")
            elif isinstance(block, ToolResultBlock):
                parts.append(
                    f"Result {block.tool_use_id} (error={block.is_error}): {str(block.content)[:block_chars]}"
                )
        if parts:
            lines.append(f"[{message.role}] " + " ".join(parts))
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    return text[:1_000] + "\n[intermediate observations omitted]\n" + text[-(max_chars - 1_050) :]


def trim_oversized_results(
    messages: list[Message], *, max_chars: int = 12_000, session_id: str | None = None
) -> list[Message]:
    result: list[Message] = []
    for message in messages:
        blocks = []
        for block in message.content:
            if (
                isinstance(block, ToolResultBlock)
                and isinstance(block.content, str)
                and len(block.content) > max_chars
            ):
                hint = "Rerun with narrower output."
                if session_id:
                    from kalash.runtime.scratchpad import get_scratchpad

                    try:
                        stored = get_scratchpad(session_id).put(
                            "other", f"Result {block.tool_use_id}", block.content
                        )
                        hint = f"Full result: expand({stored.ref}, grep=...) or offset/limit."
                    except OSError:
                        hint = "Full result could not be stored; rerun with narrower output."
                block = ToolResultBlock(
                    tool_use_id=block.tool_use_id,
                    content=block.content[:max_chars] + f"\n[observation truncated. {hint}]",
                    is_error=block.is_error,
                    evidence=block.evidence,
                )
            blocks.append(block)
        result.append(Message(role=message.role, content=blocks))
    return result


def estimate_request_tokens(
    system: str,
    messages: list[Message],
    tools: list[dict[str, Any]],
) -> int:
    """Estimate what a request will cost the provider to accept.

    Include tool schemas and every serialized block when estimating context
    occupancy. This is a context-window estimate, not a throughput quota.
    """
    import json

    from kalash.core.budget import estimate_tokens

    total = estimate_tokens(system, mode="prose") if system else 0

    if tools:
        total += estimate_tokens(json.dumps(tools, separators=(",", ":")))

    for message in messages:
        for block in message.content:
            text = getattr(block, "text", None)
            if text is None:
                text = getattr(block, "thinking", None)
            if isinstance(text, str):
                total += estimate_tokens(text)
                continue
            content = getattr(block, "content", None)
            if isinstance(content, str):
                total += estimate_tokens(content)
                continue
            payload = getattr(block, "input", None)
            if isinstance(payload, dict):
                total += estimate_tokens(json.dumps(payload, separators=(",", ":")))
            else:
                total += 50

    # Provider-side framing we do not model: wire envelopes, role markers.
    return int(total * 1.05) + 32
