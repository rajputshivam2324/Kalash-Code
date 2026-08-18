"""Summarization when budget is exceeded.

Triggered at 0.82 of context window. Preserves critical information
while compressing conversation history. Always appends new rows —
never UPDATEs (I-021).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from kalash.core.budget import BudgetState, estimate_tokens
from kalash.core.events import Event, EventBus, EventType
from kalash.core.ids import generate_id
from kalash.models.gateway import ModelGateway
from kalash.models.normalize import Message, Role, TextBlock
from kalash.storage.repositories.sessions import SessionRepository

logger = logging.getLogger(__name__)

# Compaction trigger threshold
COMPACTION_THRESHOLD: float = 0.82

# Maximum hierarchical summarization depth
MAX_SUMMARY_DEPTH: int = 3

# Prompt for summarization
COMPACTION_SYSTEM_PROMPT = """You are a summarization assistant. Compress the following conversation
while preserving ALL of the following (in order of priority):
1. Files touched (full paths)
2. Decisions made and their rationale
3. Open TODOs and next steps
4. Failed approaches and why they failed
5. User constraints (verbatim quotes)

Output a structured summary. Do not invent information not present in the conversation."""


# ---------------------------------------------------------------------------
# Preserved items for extraction
# ---------------------------------------------------------------------------


@dataclass
class PreservedContext:
    """Critical context extracted before compaction."""

    files_touched: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    open_todos: list[str] = field(default_factory=list)
    failed_approaches: list[str] = field(default_factory=list)
    user_constraints: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Compactor
# ---------------------------------------------------------------------------


@dataclass
class Compactor:
    """Manages context compaction when budget pressure is high.

    Compaction is triggered at 0.82 of the context window. It:
    1. Extracts critical preservable information
    2. Hierarchically summarizes conversation chunks
    3. Writes summaries as new rows (never UPDATE — I-021)
    4. Emits PreCompact/PostCompact hooks
    """

    gateway: ModelGateway
    session_repo: SessionRepository
    event_bus: EventBus
    budget: BudgetState

    # Configuration
    target_ratio: float = 0.50  # Target usage after compaction
    chunk_size: int = 20  # Messages per chunk for summarization

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_compact(self) -> bool:
        """Check if compaction should be triggered."""
        if self.budget.max_tokens == 0:
            return False
        ratio = self.budget.tokens_used / self.budget.max_tokens
        return ratio >= COMPACTION_THRESHOLD

    async def compact(
        self,
        session_id: str,
        messages: list[Message],
    ) -> str:
        """Compact a conversation into a summary.

        Args:
            session_id: Current session ID for persistence.
            messages: The conversation messages to compact.

        Returns:
            The compacted summary string.
        """
        # Emit PreCompact event
        await self.event_bus.emit(Event(
            type=EventType.COMPACT_START,
            session_id=session_id,
            data={"message_count": len(messages), "tokens_used": self.budget.tokens_used},
        ))

        try:
            # Extract preserved context first
            preserved = self._extract_preserved(messages)

            # Hierarchical chunked summarization
            summary = await self._hierarchical_summarize(messages, depth=0)

            # Prepend preserved items to summary
            final_summary = self._build_final_summary(preserved, summary)

            # Persist as new row (never UPDATE — I-021)
            await self._persist_summary(session_id, final_summary)

            # Emit PostCompact event
            await self.event_bus.emit(Event(
                type=EventType.COMPACT_COMPLETE,
                session_id=session_id,
                data={
                    "original_messages": len(messages),
                    "summary_tokens": estimate_tokens(final_summary),
                },
            ))

            return final_summary

        except Exception:
            # logger.exception already captures the active exception.
            logger.exception("Compaction failed")
            # On failure, fall back to a basic extractive summary.
            return self._fallback_summary(messages)

    # ------------------------------------------------------------------
    # Hierarchical summarization
    # ------------------------------------------------------------------

    async def _hierarchical_summarize(
        self,
        messages: list[Message],
        depth: int,
    ) -> str:
        """Recursively summarize message chunks up to MAX_SUMMARY_DEPTH.

        At each level, chunks of messages are summarized individually,
        then the summaries are combined. If still too large, recurse.
        """
        if depth >= MAX_SUMMARY_DEPTH:
            # Bottom out: just concatenate text
            return self._extract_text(messages)

        # Chunk messages
        chunks = self._chunk_messages(messages)

        if len(chunks) <= 1:
            # Single chunk: summarize directly
            return await self._summarize_chunk(messages)

        # Summarize each chunk
        chunk_summaries: list[str] = []
        for chunk in chunks:
            summary = await self._summarize_chunk(chunk)
            chunk_summaries.append(summary)

        # Check if combined summaries fit
        combined = "\n\n---\n\n".join(chunk_summaries)
        target_tokens = int(self.budget.max_tokens * self.target_ratio * 0.3)

        if estimate_tokens(combined) <= target_tokens:
            return combined

        # Still too large: recurse on the summaries
        summary_messages = [
            Message(role=Role.USER, content=[TextBlock(text=s)])
            for s in chunk_summaries
        ]
        return await self._hierarchical_summarize(summary_messages, depth + 1)

    async def _summarize_chunk(self, messages: list[Message]) -> str:
        """Summarize a single chunk of messages via the model."""
        # Build the summarization prompt
        conversation_text = self._extract_text(messages)

        summarize_messages = [
            Message(
                role=Role.SYSTEM,
                content=[TextBlock(text=COMPACTION_SYSTEM_PROMPT)],
            ),
            Message(
                role=Role.USER,
                content=[TextBlock(text=f"Summarize this conversation segment:\n\n{conversation_text}")],
            ),
        ]

        try:
            response = await self.gateway.complete(summarize_messages)
            # Extract text from response
            return "".join(
                block.text for block in response.content
                if hasattr(block, "text")
            )
        except Exception as exc:
            logger.warning("Summarization call failed: %s", exc)
            # Fallback: just truncate
            return conversation_text[:2000]

    # ------------------------------------------------------------------
    # Context extraction
    # ------------------------------------------------------------------

    def _extract_preserved(self, messages: list[Message]) -> PreservedContext:
        """Extract critical items that must survive compaction."""
        preserved = PreservedContext()

        for msg in messages:
            text = self._message_text(msg)
            if not text:
                continue

            # Extract file paths (simple heuristic: lines with / or \)
            for line in text.split("\n"):
                stripped = line.strip()
                # File paths
                if ("/" in stripped or "\\" in stripped) and len(stripped) < 200:
                    if any(stripped.startswith(p) for p in ("/", "~", ".", "src/")):
                        preserved.files_touched.append(stripped)
                # TODOs
                if stripped.upper().startswith("TODO"):
                    preserved.open_todos.append(stripped)

        # Deduplicate
        preserved.files_touched = list(dict.fromkeys(preserved.files_touched))
        preserved.open_todos = list(dict.fromkeys(preserved.open_todos))

        return preserved

    # ------------------------------------------------------------------
    # Summary building
    # ------------------------------------------------------------------

    def _build_final_summary(self, preserved: PreservedContext, summary: str) -> str:
        """Combine preserved context with the generated summary."""
        sections: list[str] = []

        if preserved.files_touched:
            sections.append("## Files Touched\n" + "\n".join(f"- {f}" for f in preserved.files_touched[:50]))

        if preserved.decisions:
            sections.append("## Decisions\n" + "\n".join(f"- {d}" for d in preserved.decisions))

        if preserved.open_todos:
            sections.append("## Open TODOs\n" + "\n".join(f"- {t}" for t in preserved.open_todos))

        if preserved.failed_approaches:
            sections.append("## Failed Approaches\n" + "\n".join(f"- {a}" for a in preserved.failed_approaches))

        if preserved.user_constraints:
            sections.append("## User Constraints (verbatim)\n" + "\n".join(f"> {c}" for c in preserved.user_constraints))

        sections.append("## Conversation Summary\n" + summary)

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Persistence (append-only — I-021)
    # ------------------------------------------------------------------

    async def _persist_summary(self, session_id: str, summary: str) -> None:
        """Persist the compaction summary as a new message row."""
        # Create a compaction turn
        turn_id = await self.session_repo.create_turn(
            session_id=session_id,
            seq=0,  # Compaction turns use seq=0
            role="system",
        )
        await self.session_repo.add_message(
            turn_id=turn_id,
            seq=0,
            role="system",
            content=summary,
            content_type="compaction_summary",
        )
        await self.session_repo.complete_turn(turn_id=turn_id, state="COMPLETED")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _chunk_messages(self, messages: list[Message]) -> list[list[Message]]:
        """Split messages into chunks of self.chunk_size."""
        chunks: list[list[Message]] = []
        for i in range(0, len(messages), self.chunk_size):
            chunks.append(messages[i : i + self.chunk_size])
        return chunks

    def _extract_text(self, messages: list[Message]) -> str:
        """Extract all text content from a list of messages."""
        parts: list[str] = []
        for msg in messages:
            text = self._message_text(msg)
            if text:
                parts.append(f"[{msg.role}]: {text}")
        return "\n\n".join(parts)

    @staticmethod
    def _message_text(msg: Message) -> str:
        """Get concatenated text content from a message."""
        return "".join(
            block.text for block in msg.content
            if hasattr(block, "text")
        )

    def _fallback_summary(self, messages: list[Message]) -> str:
        """Emergency fallback if LLM summarization fails."""
        # Keep first and last few messages
        if len(messages) <= 6:
            return self._extract_text(messages)
        head = self._extract_text(messages[:3])
        tail = self._extract_text(messages[-3:])
        return f"{head}\n\n[... {len(messages) - 6} messages compacted ...]\n\n{tail}"
