"""Hash and near-duplicate collapse for memory writes.

Deduplication runs BEFORE writes reach the router to prevent:
1. Exact duplicates (same content hash)
2. Near-duplicates (cosine similarity >= 0.94)
3. Contradictions (same subject_key, different content → supersede)

The pipeline:
- Hash dedup: O(1) lookup via content SHA-256
- Near-duplicate: cosine similarity on embeddings (when available)
- Contradiction: subject_key match with content divergence → supersede old
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

try:
    import structlog
    logger = structlog.get_logger()
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from kalash.memory.protocol import (
    MemoryEdit,
    MemoryHit,
    MemoryKind,
    MemoryRecord,
    MemoryWrite,
    RecallQuery,
    Scope,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NEAR_DUPLICATE_THRESHOLD: float = 0.94  # Cosine similarity threshold
CONTRADICTION_SAME_SUBJECT_THRESHOLD: float = 0.6  # Below this → contradiction


# ---------------------------------------------------------------------------
# Hash dedup
# ---------------------------------------------------------------------------


def content_hash(content: str) -> str:
    """Compute SHA-256 hash of content for exact dedup."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def hash_dedup(
    candidates: list[MemoryWrite],
    existing_hashes: set[str],
) -> list[MemoryWrite]:
    """Remove candidates whose content hash already exists.

    Args:
        candidates: Incoming write candidates.
        existing_hashes: Set of content hashes already in the store.

    Returns:
        Candidates that are NOT exact duplicates.
    """
    deduplicated: list[MemoryWrite] = []
    for candidate in candidates:
        h = content_hash(candidate.content)
        if h in existing_hashes:
            logger.debug("hash_dedup_dropped", content_preview=candidate.content[:50])
            continue
        existing_hashes.add(h)  # Prevent intra-batch duplicates
        deduplicated.append(candidate)

    dropped = len(candidates) - len(deduplicated)
    if dropped:
        logger.info("hash_dedup_complete", input=len(candidates), dropped=dropped)

    return deduplicated


# ---------------------------------------------------------------------------
# Near-duplicate detection
# ---------------------------------------------------------------------------


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Returns 0.0 if either vector is zero-length.
    """
    if len(vec_a) != len(vec_b) or not vec_a:
        return 0.0

    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return dot / (norm_a * norm_b)


@dataclass
class NearDuplicateResult:
    """Result of near-duplicate detection."""

    candidate: MemoryWrite
    is_duplicate: bool = False
    duplicate_of: str | None = None  # Record ID of the near-duplicate
    similarity: float = 0.0


async def near_duplicate_check(
    candidates: list[MemoryWrite],
    get_embedding: callable | None = None,
    existing_records: Sequence[MemoryRecord] | None = None,
    threshold: float = NEAR_DUPLICATE_THRESHOLD,
) -> list[NearDuplicateResult]:
    """Check candidates against existing records for near-duplicates.

    Args:
        candidates: Incoming write candidates.
        get_embedding: Async function (text) -> list[float]. If None, skips check.
        existing_records: Records to compare against.
        threshold: Cosine similarity threshold for duplicate detection.

    Returns:
        Results for each candidate with duplicate status.
    """
    if get_embedding is None or not existing_records:
        # Cannot do near-duplicate check without embeddings
        return [
            NearDuplicateResult(candidate=c, is_duplicate=False)
            for c in candidates
        ]

    results: list[NearDuplicateResult] = []

    for candidate in candidates:
        try:
            candidate_embedding = await get_embedding(candidate.content)
        except Exception as e:
            logger.warning("Embedding failed for candidate in dedupe", exc_info=e)
            results.append(NearDuplicateResult(candidate=candidate, is_duplicate=False))
            continue

        best_similarity = 0.0
        best_match_id: str | None = None

        for record in existing_records:
            # We need embeddings from existing records
            # In practice, these would be stored alongside the record
            # For now, this is a comparison stub
            if not hasattr(record, "_embedding"):
                continue

            sim = cosine_similarity(candidate_embedding, record._embedding)  # type: ignore
            if sim > best_similarity:
                best_similarity = sim
                best_match_id = record.id

        is_dup = best_similarity >= threshold
        results.append(NearDuplicateResult(
            candidate=candidate,
            is_duplicate=is_dup,
            duplicate_of=best_match_id if is_dup else None,
            similarity=best_similarity,
        ))

        if is_dup:
            logger.debug(
                "near_duplicate_detected",
                content_preview=candidate.content[:50],
                similarity=round(best_similarity, 3),
                duplicate_of=best_match_id,
            )

    return results


# ---------------------------------------------------------------------------
# Contradiction / supersede logic
# ---------------------------------------------------------------------------


@dataclass
class SupersedeAction:
    """Action to supersede an existing record with a new one."""

    old_record_id: str
    new_write: MemoryWrite
    reason: str = "contradiction"


async def detect_contradictions(
    candidates: list[MemoryWrite],
    existing_by_subject: dict[str, MemoryRecord],
) -> tuple[list[MemoryWrite], list[SupersedeAction]]:
    """Detect contradictions between candidates and existing records.

    When a candidate has the same subject_key as an existing record but
    different content, the old record should be superseded.

    Args:
        candidates: Incoming write candidates.
        existing_by_subject: Existing records indexed by subject_key.

    Returns:
        Tuple of (clean_candidates, supersede_actions).
        Clean candidates can proceed directly; supersede_actions need
        the old record marked as superseded before the new one is written.
    """
    clean: list[MemoryWrite] = []
    supersede_actions: list[SupersedeAction] = []

    for candidate in candidates:
        if candidate.subject_key is None:
            clean.append(candidate)
            continue

        existing = existing_by_subject.get(candidate.subject_key)
        if existing is None:
            clean.append(candidate)
            continue

        # Same subject_key exists — check if content differs
        if content_hash(candidate.content) == existing.content_hash:
            # Same content, skip (exact duplicate handled by hash_dedup)
            logger.debug(
                "subject_key_same_content",
                subject_key=candidate.subject_key,
            )
            continue

        # Different content for same subject → supersede
        supersede_actions.append(SupersedeAction(
            old_record_id=existing.id,
            new_write=candidate,
            reason=f"content_changed for subject '{candidate.subject_key}'",
        ))

        logger.info(
            "contradiction_detected",
            subject_key=candidate.subject_key,
            old_id=existing.id,
            old_preview=existing.content[:40],
            new_preview=candidate.content[:40],
        )

    return clean, supersede_actions


# ---------------------------------------------------------------------------
# Full dedup pipeline
# ---------------------------------------------------------------------------


@dataclass
class DedupeResult:
    """Result of the full deduplication pipeline."""

    writes: list[MemoryWrite]  # Clean candidates to write
    supersede_actions: list[SupersedeAction]  # Records to supersede
    dropped_exact: int = 0  # Dropped by hash dedup
    dropped_near: int = 0  # Dropped by near-duplicate detection


async def run_dedup_pipeline(
    candidates: list[MemoryWrite],
    existing_hashes: set[str],
    existing_by_subject: dict[str, MemoryRecord] | None = None,
    get_embedding: callable | None = None,
    existing_records: Sequence[MemoryRecord] | None = None,
) -> DedupeResult:
    """Run the full deduplication pipeline.

    Steps:
    1. Hash dedup (exact content match)
    2. Near-duplicate detection (cosine >= 0.94)
    3. Contradiction detection (same subject_key, different content)

    Args:
        candidates: Raw extraction candidates.
        existing_hashes: Known content hashes.
        existing_by_subject: Existing records by subject_key.
        get_embedding: Optional embedding function for near-dup check.
        existing_records: Existing records for near-dup comparison.

    Returns:
        DedupeResult with clean writes and supersede actions.
    """
    if not candidates:
        return DedupeResult(writes=[], supersede_actions=[])

    original_count = len(candidates)

    # Step 1: Hash dedup
    after_hash = await hash_dedup(candidates, existing_hashes)
    dropped_exact = original_count - len(after_hash)

    # Step 2: Near-duplicate detection
    dropped_near = 0
    if get_embedding and existing_records:
        near_results = await near_duplicate_check(
            after_hash, get_embedding, existing_records
        )
        after_near = [r.candidate for r in near_results if not r.is_duplicate]
        dropped_near = len(after_hash) - len(after_near)
    else:
        after_near = after_hash

    # Step 3: Contradiction detection
    supersede_actions: list[SupersedeAction] = []
    if existing_by_subject:
        clean, supersede_actions = await detect_contradictions(after_near, existing_by_subject)
    else:
        clean = after_near

    logger.info(
        "dedup_pipeline_complete",
        input=original_count,
        output=len(clean),
        dropped_exact=dropped_exact,
        dropped_near=dropped_near,
        supersede_count=len(supersede_actions),
    )

    return DedupeResult(
        writes=clean,
        supersede_actions=supersede_actions,
        dropped_exact=dropped_exact,
        dropped_near=dropped_near,
    )
