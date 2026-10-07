"""Scrub observation text and copies of metadata without changing tool effects.

This is defense in depth, not a credential broker or a complete DLP system.
Opaque provider reasoning/signatures and image bytes are left intact.
"""

from __future__ import annotations

import contextlib
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


def _known_secrets() -> list[str]:
    known = [
        value for name, value in os.environ.items() if _SECRET_NAME.search(name) and len(value) >= 8
    ]
    from kalash.models.auth_store import load_auth

    with contextlib.suppress(OSError, ValueError):
        known.extend(
            value
            for key, value in load_auth().items()
            if not key.endswith(":base_url") and len(value) >= 8
        )
    return known


def scrub_text(text: str) -> str:
    """Recognize credential formats and exact credential values known to this process."""
    known = _known_secrets()
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


class StreamRedactor:
    """Retain unfinished tokens across chunks; never display a partial secret.

    Bounded lexical buffering covers credential formats and known env values.
    This is defense in depth, not recognition of every possible secret.
    """

    def __init__(self) -> None:
        self.pending = ""
        self.discarding = False
        self.pem_discarding = False
        self.secrets = _known_secrets()

    def feed(self, text: str, *, final: bool = False) -> str:
        self.pending += text
        if self.pem_discarding:
            footer = re.search(r"-----END [A-Z ]*PRIVATE KEY-----", self.pending)
            if footer is None:
                self.pending = self.pending[-80:] if not final else ""
                return ""
            self.pending = self.pending[footer.end() :]
            self.pem_discarding = False
        if self.discarding:
            boundary = re.search(r"\s", self.pending)
            if boundary is None:
                self.pending = ""
                return ""
            self.pending = self.pending[boundary.start() :]
            self.discarding = False
        if final:
            begin = self.pending.find("-----BEGIN")
            if begin >= 0 and not re.search(
                r"-----END [A-Z ]*PRIVATE KEY-----", self.pending[begin:]
            ):
                self.pending = self.pending[:begin] + "[unfinished private key withheld]"
            result = scrub_text(self.pending)
            self.pending = ""
            return result
        boundaries = list(re.finditer(r"\s", self.pending))
        cut = boundaries[-1].end() if boundaries else 0
        begin = self.pending.find("-----BEGIN")
        if begin >= 0 and not re.search(r"-----END [A-Z ]*PRIVATE KEY-----", self.pending[begin:]):
            cut = min(cut, begin)
        # A known value containing whitespace may be split at a lexical boundary.
        for secret in self.secrets:
            for length in range(min(len(secret), cut), 0, -1):
                if self.pending[:cut].endswith(secret[:length]):
                    cut -= length
                    break
        safe, self.pending = self.pending[:cut], self.pending[cut:]
        if len(self.pending) > 8192:
            self.pem_discarding = self.pending.startswith("-----BEGIN")
            self.pending = self.pending[-80:] if self.pem_discarding else ""
            self.discarding = not self.pem_discarding
            safe += "[oversized streaming token withheld]"
        return scrub_text(safe)
