"""Budget-aware context block construction for memory injection.

This module renders recalled memories into a structured <memory> block
that fits within the context budget. It performs:
1. Budget fitting: greedy selection by fused score, then diversity pass
2. Novelty filter: drops records whose content is already in context
3. Rendering: structured XML-like block with provenance metadata

The output is a MemoryBlock that the context assembler inserts into the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence

import structlog

from kalash.core.budget import estimate_tokens
from kalash.memory.protocol import MemoryHit, MemoryKind, Trust

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemoryBlockEntry:
    """A single entry in the rendered memory block."""

    record_id: str
    kind: MemoryKind
    content: str
    score: Decimal
    trust: Trust
    age_seconds: int
    source: str
    subject_key: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryBlock:
    """The rendered memory block ready for prompt injection."""

    entries: tuple[MemoryBlockEntry, ...]
    rendered: str  # Final rendered text
    token_estimate: int
    total_candidates: int  # How many hits were considered
    included_count: int  # How many made it into the block
    budget_used_fraction: float  # Fraction of budget consumed


# ---------------------------------------------------------------------------
# Novelty filter
# ---------------------------------------------------------------------------


def filter_novelty(
    hits: list[MemoryHit],
    existing_context: str,
    min_novelty_chars: int = 20,
) -> list[MemoryHit]:
    """Remove hits whose content is already present in the existing context.

    Args:
        hits: Scored memory hits.
        existing_context: Current prompt/context text.
        min_novelty_chars: Minimum unique characters for a hit to be novel.

    Returns:
        Hits that add new information.
    """
    if not existing_context:
        return hits

    context_lower = existing_context.lower()
    novel: list[MemoryHit] = []

    for hit in hits:
        # Check if the core content is already in context
        content_lower = hit.record.content.lower().strip()
        if len(content_lower) < min_novelty_chars:
            novel.append(hit)  # Too short to meaningfully check
            continue

        # Check for substring presence (the main content, not metadata)
        # Use a sliding window of the first 80 chars as a fingerprint
        fingerprint = content_lower[:80]
        if fingerprint in context_lower:
            logger.debug(
                "novelty_filter_dropped",
                record_id=hit.record.id,
                reason="content_in_context",
            )
            continue

        novel.append(hit)

    dropped = len(hits) - len(novel)
    if dropped:
        logger.debug("novelty_filter", input=len(hits), dropped=dropped)

    return novel


# ---------------------------------------------------------------------------
# Budget fitting
# ---------------------------------------------------------------------------


def fit_to_budget(
    hits: list[MemoryHit],
    budget_tokens: int,
    diversity_weight: float = 0.3,
) -> list[MemoryHit]:
    """Select hits that fit within the token budget.

    Strategy:
    1. Greedy pass: select by fused score until budget is ~70% consumed.
    2. Diversity pass: from remaining, prefer kinds not yet represented.

    Args:
        hits: Pre-sorted hits (highest score first).
        budget_tokens: Maximum tokens available for the memory block.
        diversity_weight: Fraction of budget reserved for diversity pass.

    Returns:
        Selected hits that fit within budget.
    """
    if not hits or budget_tokens <= 0:
        return []

    # Pass 1 is capped so that some budget survives for the diversity pass,
    # which then spends up to the full budget.
    greedy_budget = int(budget_tokens * (1 - diversity_weight))

    selected: list[MemoryHit] = []
    used_tokens = 0
    kinds_covered: set[MemoryKind] = set()
    remaining: list[MemoryHit] = []

    # Pass 1: Greedy by score
    for hit in hits:
        entry_tokens = _estimate_entry_tokens(hit)
        if used_tokens + entry_tokens <= greedy_budget:
            selected.append(hit)
            used_tokens += entry_tokens
            kinds_covered.add(hit.record.kind)
        else:
            remaining.append(hit)

    # Pass 2: Diversity — prefer uncovered kinds
    remaining_sorted = sorted(
        remaining,
        key=lambda h: (h.record.kind not in kinds_covered, h.score),
        reverse=True,
    )

    for hit in remaining_sorted:
        entry_tokens = _estimate_entry_tokens(hit)
        if used_tokens + entry_tokens <= budget_tokens:
            selected.append(hit)
            used_tokens += entry_tokens
            kinds_covered.add(hit.record.kind)

    return selected


def _estimate_entry_tokens(hit: MemoryHit) -> int:
    """Estimate tokens for a rendered memory entry."""
    # Content + metadata overhead (~30 tokens for tags and attributes)
    content_tokens = estimate_tokens(hit.record.content)
    overhead = 30
    return content_tokens + overhead


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_memory_block(
    hits: list[MemoryHit],
    now: datetime | None = None,
) -> str:
    """Render selected hits into the <memory> XML block.

    Format:
    <memory>
      <record id="mem_..." kind="semantic" score="0.87" trust="high" age="2h" source="tool_derived">
        content here
      </record>
      ...
    </memory>
    """
    if not hits:
        return ""

    now = now or datetime.now(timezone.utc)
    lines: list[str] = ["<memory>"]

    for hit in hits:
        record = hit.record
        age = _format_age(record.provenance.created_at, now)
        attrs = [
            f'id="{record.id}"',
            f'kind="{record.kind.value}"',
            f'score="{hit.score:.2f}"',
            f'trust="{record.provenance.trust.value}"',
            f'age="{age}"',
            f'source="{record.provenance.source.value}"',
        ]
        if record.subject_key:
            attrs.append(f'subject="{record.subject_key}"')

        attr_str = " ".join(attrs)
        lines.append(f"  <record {attr_str}>")
        # Indent content
        for content_line in record.content.split("\n"):
            lines.append(f"    {content_line}")
        lines.append("  </record>")

    lines.append("</memory>")
    return "\n".join(lines)


def _format_age(created_at: datetime, now: datetime) -> str:
    """Format age as human-readable string."""
    delta = now - created_at
    seconds = int(delta.total_seconds())

    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m"
    elif seconds < 86400:
        return f"{seconds // 3600}h"
    else:
        return f"{seconds // 86400}d"


# ---------------------------------------------------------------------------
# Main injection function
# ---------------------------------------------------------------------------


async def build_memory_block(
    hits: list[MemoryHit],
    budget_tokens: int,
    existing_context: str = "",
    diversity_weight: float = 0.3,
) -> MemoryBlock:
    """Build a budget-aware memory block from recall results.

    This is the main entry point for the injection pipeline.

    Args:
        hits: Raw recall hits (already scored and sorted by router).
        budget_tokens: Maximum tokens for the memory block.
        existing_context: Current context text (for novelty filtering).
        diversity_weight: Fraction of budget for diversity pass.

    Returns:
        MemoryBlock with rendered content and metadata.
    """
    total_candidates = len(hits)

    if not hits or budget_tokens <= 0:
        return MemoryBlock(
            entries=(),
            rendered="",
            token_estimate=0,
            total_candidates=total_candidates,
            included_count=0,
            budget_used_fraction=0.0,
        )

    # Step 1: Novelty filter
    novel_hits = filter_novelty(hits, existing_context)

    # Step 2: Sort by score (should already be sorted, but ensure)
    novel_hits.sort(key=lambda h: h.score, reverse=True)

    # Step 3: Budget fitting with diversity
    selected = fit_to_budget(novel_hits, budget_tokens, diversity_weight)

    # Step 4: Render
    now = datetime.now(timezone.utc)
    rendered = render_memory_block(selected, now)
    token_estimate = estimate_tokens(rendered)

    # Step 5: Build entries for metadata
    entries: list[MemoryBlockEntry] = []
    for hit in selected:
        record = hit.record
        age_seconds = int((now - record.provenance.created_at).total_seconds())
        entries.append(MemoryBlockEntry(
            record_id=record.id,
            kind=record.kind,
            content=record.content,
            score=hit.score,
            trust=record.provenance.trust,
            age_seconds=age_seconds,
            source=record.provenance.source.value,
            subject_key=record.subject_key,
        ))

    budget_fraction = token_estimate / budget_tokens if budget_tokens > 0 else 0.0

    logger.debug(
        "memory_block_built",
        candidates=total_candidates,
        novel=len(novel_hits),
        selected=len(selected),
        tokens=token_estimate,
        budget_fraction=round(budget_fraction, 3),
    )

    return MemoryBlock(
        entries=tuple(entries),
        rendered=rendered,
        token_estimate=token_estimate,
        total_candidates=total_candidates,
        included_count=len(selected),
        budget_used_fraction=budget_fraction,
    )
