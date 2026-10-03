"""Scrub observation text and copies of metadata without changing tool effects.

This is defense in depth, not a credential broker or a complete DLP system.
Opaque provider reasoning/signatures and image bytes are left intact.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace
from typing import Any

from kalash.core.redact import redact
from kalash.models.normalize import (
    ContentBlock,
    MemoryBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

_SECRET_NAME = re.compile(r"(?:^|_)(?:API_KEY|KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL)$", re.I)


def scrub_text(text: str) -> str:
    """Recognize credential formats and exact credential values in this process's env."""
    known = [
        value for name, value in os.environ.items() if _SECRET_NAME.search(name) and len(value) >= 8
    ]
    return redact(text, known_secrets=known).text


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return scrub_metadata(value)
    if isinstance(value, (list, tuple)):
        return [_scrub_value(item) for item in value]
    return value


def scrub_metadata(data: dict[str, Any]) -> dict[str, Any]:
    """Preserve keys and counters; redact explicit credential fields even at low entropy."""
    return {
        key: redact(str(value), known_secrets=[str(value)]).text
        if _SECRET_NAME.search(key) and isinstance(value, str) and value
        else _scrub_value(value)
        for key, value in data.items()
    }


def scrub_blocks(blocks: list[ContentBlock]) -> list[ContentBlock]:
    result: list[ContentBlock] = []
    for block in blocks:
        if isinstance(block, TextBlock):
            block = replace(block, text=scrub_text(block.text))
        elif isinstance(block, ToolUseBlock):
            block = replace(block, input=scrub_metadata(block.input))
        elif isinstance(block, ToolResultBlock):
            content = (
                scrub_text(block.content)
                if isinstance(block.content, str)
                else scrub_blocks(block.content)
            )
            block = replace(block, content=content, evidence=scrub_metadata(block.evidence))
        elif isinstance(block, MemoryBlock):
            block = replace(block, content=scrub_text(block.content))
        result.append(block)
    return result


def scrub_messages(messages: list[Message]) -> list[Message]:
    return [replace(message, content=scrub_blocks(message.content)) for message in messages]
