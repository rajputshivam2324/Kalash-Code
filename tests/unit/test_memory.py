"""Tests for memory pipeline — hash dedup, near-duplicate, contradiction, cosine."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Any

import pytest

from kalash.memory.pipeline.dedupe import (
    content_hash,
    cosine_similarity,
    hash_dedup,
    near_duplicate_check,
    detect_contradictions,
    run_dedup_pipeline,
    NearDuplicateResult,
    SupersedeAction,
    DedupeResult,
    NEAR_DUPLICATE_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Minimal protocol stubs (only what dedupe needs)
# ---------------------------------------------------------------------------


@dataclass
class FakeMemoryWrite:
    content: str
    subject_key: str | None = None


@dataclass
class FakeMemoryRecord:
    id: str
    content: str
    content_hash: str = ""
    _embedding: list[float] | None = None

    def __post_init__(self):
        if not self.content_hash:
            self.content_hash = hashlib.sha256(self.content.encode()).hexdigest()


# Monkey-patch MemoryWrite/MemoryRecord to use these fakes in tests
import kalash.memory.pipeline.dedupe as dedupe_mod

_orig_MemoryWrite = getattr(dedupe_mod, "MemoryWrite", None)
_orig_MemoryRecord = getattr(dedupe_mod, "MemoryRecord", None)


# ---------------------------------------------------------------------------
# content_hash
# ---------------------------------------------------------------------------


class TestContentHash:
    def test_deterministic(self):
        assert content_hash("hello") == content_hash("hello")

    def test_different_content_different_hash(self):
        assert content_hash("hello") != content_hash("world")

    def test_sha256_format(self):
        h = content_hash("test")
        assert len(h) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# cosine_similarity
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 2.0, 3.0]
        assert abs(cosine_similarity(v, v) - 1.0) < 1e-6

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert abs(cosine_similarity(a, b)) < 1e-6

    def test_opposite_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert abs(cosine_similarity(a, b) + 1.0) < 1e-6

    def test_zero_vector_returns_zero(self):
        a = [0.0, 0.0, 0.0]
        b = [1.0, 2.0, 3.0]
        assert cosine_similarity(a, b) == 0.0

    def test_empty_vectors_returns_zero(self):
        assert cosine_similarity([], []) == 0.0

    def test_mismatched_lengths_returns_zero(self):
        assert cosine_similarity([1.0], [1.0, 2.0]) == 0.0


# ---------------------------------------------------------------------------
# hash_dedup
# ---------------------------------------------------------------------------


class TestHashDedup:
    @pytest.mark.asyncio
    async def test_drops_exact_duplicates(self):
        candidates = [
            FakeMemoryWrite(content="hello world"),
            FakeMemoryWrite(content="hello world"),  # duplicate
        ]
        existing = set()
        result = await hash_dedup(candidates, existing)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_keeps_unique(self):
        candidates = [
            FakeMemoryWrite(content="first"),
            FakeMemoryWrite(content="second"),
        ]
        existing = set()
        result = await hash_dedup(candidates, existing)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_drops_if_hash_already_exists(self):
        existing = {content_hash("already stored")}
        candidates = [FakeMemoryWrite(content="already stored")]
        result = await hash_dedup(candidates, existing)
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_empty_candidates(self):
        result = await hash_dedup([], set())
        assert result == []


# ---------------------------------------------------------------------------
# near_duplicate_check
# ---------------------------------------------------------------------------


class TestNearDuplicateCheck:
    @pytest.mark.asyncio
    async def test_no_embedding_skips_check(self):
        candidates = [FakeMemoryWrite(content="test")]
        results = await near_duplicate_check(candidates, get_embedding=None)
        assert len(results) == 1
        assert not results[0].is_duplicate

    @pytest.mark.asyncio
    async def test_no_records_skips_check(self):
        candidates = [FakeMemoryWrite(content="test")]

        async def embed(text):
            return [1.0, 0.0]

        results = await near_duplicate_check(candidates, get_embedding=embed, existing_records=[])
        assert not results[0].is_duplicate

    @pytest.mark.asyncio
    async def test_detects_near_duplicate(self):
        record = FakeMemoryRecord(id="rec_1", content="test data")
        record._embedding = [1.0, 0.0, 0.0]

        candidates = [FakeMemoryWrite(content="test data similar")]

        async def embed(text):
            return [1.0, 0.0, 0.0]  # identical embedding = cosine 1.0

        results = await near_duplicate_check(
            candidates,
            get_embedding=embed,
            existing_records=[record],
            threshold=0.9,
        )
        assert results[0].is_duplicate
        assert results[0].duplicate_of == "rec_1"


# ---------------------------------------------------------------------------
# detect_contradictions
# ---------------------------------------------------------------------------


class TestDetectContradictions:
    @pytest.mark.asyncio
    async def test_no_subject_key_passes_through(self):
        candidates = [FakeMemoryWrite(content="no key", subject_key=None)]
        clean, actions = await detect_contradictions(candidates, {})
        assert len(clean) == 1
        assert len(actions) == 0

    @pytest.mark.asyncio
    async def test_new_subject_passes_through(self):
        candidates = [FakeMemoryWrite(content="new data", subject_key="user.name")]
        clean, actions = await detect_contradictions(candidates, {})
        assert len(clean) == 1

    @pytest.mark.asyncio
    async def test_same_content_same_subject_skipped(self):
        content = "test content"
        existing = FakeMemoryRecord(id="rec_1", content=content)
        candidates = [FakeMemoryWrite(content=content, subject_key="key")]
        clean, actions = await detect_contradictions(candidates, {"key": existing})
        assert len(clean) == 0  # exact dup skipped
        assert len(actions) == 0

    @pytest.mark.asyncio
    async def test_different_content_same_subject_supersedes(self):
        existing = FakeMemoryRecord(id="rec_1", content="old value")
        candidates = [FakeMemoryWrite(content="new value", subject_key="key")]
        clean, actions = await detect_contradictions(candidates, {"key": existing})
        assert len(clean) == 0
        assert len(actions) == 1
        assert actions[0].old_record_id == "rec_1"


# ---------------------------------------------------------------------------
# run_dedup_pipeline (end-to-end)
# ---------------------------------------------------------------------------


class TestRunDedupPipeline:
    @pytest.mark.asyncio
    async def test_empty_candidates(self):
        result = await run_dedup_pipeline([], set())
        assert result.writes == []
        assert result.dropped_exact == 0

    @pytest.mark.asyncio
    async def test_full_pipeline_deduplicates(self):
        candidates = [
            FakeMemoryWrite(content="unique item"),
            FakeMemoryWrite(content="unique item"),  # exact dup
            FakeMemoryWrite(content="another item"),
        ]
        result = await run_dedup_pipeline(candidates, set())
        assert result.dropped_exact == 1
        assert len(result.writes) == 2

    @pytest.mark.asyncio
    async def test_existing_hashes_prevent_writes(self):
        existing = {content_hash("already in store")}
        candidates = [FakeMemoryWrite(content="already in store")]
        result = await run_dedup_pipeline(candidates, existing)
        assert len(result.writes) == 0
        assert result.dropped_exact == 1


# ---------------------------------------------------------------------------
# NearDuplicateResult / SupersedeAction / DedupeResult dataclasses
# ---------------------------------------------------------------------------


class TestDedupeDataclasses:
    def test_near_duplicate_result_defaults(self):
        r = NearDuplicateResult(candidate=FakeMemoryWrite(content="x"))
        assert not r.is_duplicate
        assert r.similarity == 0.0

    def test_supersede_action(self):
        a = SupersedeAction(
            old_record_id="rec_1",
            new_write=FakeMemoryWrite(content="new"),
        )
        assert a.reason == "contradiction"

    def test_dedupe_result_fields(self):
        r = DedupeResult(writes=[], supersede_actions=[], dropped_exact=5, dropped_near=2)
        assert r.dropped_exact == 5
        assert r.dropped_near == 2
