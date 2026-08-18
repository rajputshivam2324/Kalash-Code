"""Automatic memory extraction after agent turns."""

from __future__ import annotations

import logging
from pathlib import Path

from kalash.memory.pipeline.dedupe import run_dedup_pipeline
from kalash.memory.pipeline.extract import ExtractionContext, run_extraction
from kalash.memory.protocol import MemoryEdit, RecallQuery, Scope, Source, Visibility
from kalash.memory.session import project_scope
from kalash.models.normalize import Message, TextBlock, ToolResultBlock

logger = logging.getLogger(__name__)


def _text_from_message(message: Message) -> str:
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, TextBlock) and block.text:
            parts.append(block.text)
        elif isinstance(block, ToolResultBlock) and block.content:
            parts.append(block.content)
    return "\n".join(parts)


async def capture_turn_memories(
    *,
    session_id: str,
    cwd: Path,
    user_prompt: str,
    conversation: list[Message],
    turn_seq: int = 0,
) -> int:
    """Extract, dedupe, and persist memories from a completed turn.

    Returns the number of records written or superseded.
    """
    try:
        from kalash.core.config import load_config

        if not load_config().memory.enabled:
            return 0

        from kalash.memory.service import get_memory_service

        service = await get_memory_service()
        router = service.router
        scope = project_scope(cwd, session_id=session_id)

        writes = await run_extraction(
            user_prompt,
            ExtractionContext(
                source=Source.USER_STATED,
                session_id=session_id,
                turn_seq=turn_seq,
            ),
            scope,
        )

        # Tool outputs from the latest assistant turn (last few messages).
        for message in conversation[-8:]:
            text = _text_from_message(message)
            if len(text) < 40:
                continue
            writes.extend(
                await run_extraction(
                    text,
                    ExtractionContext(
                        source=Source.TOOL_DERIVED,
                        session_id=session_id,
                        turn_seq=turn_seq,
                    ),
                    scope,
                )
            )

        if not writes:
            return 0

        existing = await router.recall(
            RecallQuery(scope=scope, limit=250)
        )
        existing_hashes = {hit.record.content_hash for hit in existing}
        existing_by_subject = {
            hit.record.subject_key: hit.record
            for hit in existing
            if hit.record.subject_key
        }

        deduped = await run_dedup_pipeline(
            writes,
            existing_hashes,
            existing_by_subject=existing_by_subject,
        )

        written = 0
        for intent in deduped.writes:
            receipts = await router.write(intent)
            written += len(receipts)

        for action in deduped.supersede_actions:
            receipts = await router.write(action.new_write)
            if receipts:
                await router.update(
                    MemoryEdit(
                        id=action.old_record_id,
                        superseded_by=receipts[0].record_id,
                    )
                )
                written += 1

        if written:
            logger.info(
                "turn memories captured",
                extra={"session_id": session_id, "count": written},
            )
        return written

    except Exception:
        logger.debug("turn memory capture skipped", exc_info=True)
        return 0
