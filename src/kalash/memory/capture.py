"""Persist explicit preferences once after a user turn."""

from __future__ import annotations

from pathlib import Path

from kalash.core.config import KalashConfig
from kalash.memory.pipeline.extract import ExtractionContext, run_extraction
from kalash.memory.protocol import Source
from kalash.memory.service import get_memory_service
from kalash.memory.session import project_scope
from kalash.models.normalize import Message


async def capture_turn_memories(
    *,
    session_id: str,
    cwd: Path,
    user_prompt: str,
    conversation: list[Message],
    turn_seq: int = 0,
    config: KalashConfig | None = None,
) -> int:
    """Capture user preferences; conversation/tool output is never mined."""
    service = await get_memory_service(config=config)
    writes = await run_extraction(
        user_prompt,
        ExtractionContext(source=Source.USER_STATED, session_id=session_id, turn_seq=turn_seq),
        project_scope(cwd, session_id=session_id),
    )
    written = 0
    for intent in writes:
        written += bool(await service.router.write(intent))
    return written
