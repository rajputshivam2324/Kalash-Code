"""Capture explicit user preferences without mining tool output."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from kalash.core.redact import redact
from kalash.memory.protocol import MemoryKind, MemoryWrite, Provenance, Scope, Source, Trust

_PREFERENCE = re.compile(
    r"\b(?:I prefer|I always use|remember that|remember to)\s+[^\n.!?]+[.!?]?",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ExtractionContext:
    source: Source = Source.TOOL_DERIVED
    file_path: str | None = None
    tool_name: str | None = None
    session_id: str | None = None
    turn_seq: int | None = None


async def run_extraction(
    content: str, context: ExtractionContext, scope: Scope
) -> list[MemoryWrite]:
    """Save at most eight explicit preferences from a bounded user message.

    Preferences have no shared subject key: mentioning a second preference
    does not replace the first. Explicit remember/forget tools handle corrections.
    """
    if context.source != Source.USER_STATED:
        return []
    return [
        MemoryWrite(
            kind=MemoryKind.SEMANTIC,
            content=redact(match.group().strip()).text[:500],
            scope=scope,
            provenance=Provenance(
                source=context.source,
                trust=Trust.MEDIUM,
                session_id=context.session_id,
                turn_seq=context.turn_seq,
            ),
            confidence=Decimal("0.9"),
            salience=Decimal("0.8"),
        )
        for match in list(_PREFERENCE.finditer(content[:16_000]))[:8]
    ]
