"""Multi-provider read/write orchestration.

The MemoryRouter is the ONLY interface the runtime uses for memory operations.
It handles:
- Write dispatch (primary, mirror_all, by_kind, sticky)
- Read fanout with Reciprocal Rank Fusion merge
- Scope re-verification (client-side, I-017)
- Per-provider timeouts and circuit breakers
- Tombstone suppression
- Graceful degradation (recall returns [] on total failure, I-005)
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Sequence

import structlog

from kalash.core.events import Event, EventType, get_event_bus
from kalash.core.ids import generate_id
from kalash.memory.protocol import (
    ForgetReceipt,
    ForgetSelector,
    MemoryEdit,
    MemoryHit,
    MemoryKind,
    MemoryProvider,
    MemoryRecord,
    MemoryWrite,
    Provenance,
    RecallQuery,
    Scope,
    WriteReceipt,
)
from kalash.memory.registry import MemoryRegistry

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------


class WritePolicy(StrEnum):
    """How writes are dispatched across providers."""

    PRIMARY = "primary"  # Write only to primary provider
    MIRROR_ALL = "mirror_all"  # Write to all healthy providers
    BY_KIND = "by_kind"  # Route by MemoryKind → provider mapping
    STICKY = "sticky"  # Write to origin provider (for edits)


class ReadPolicy(StrEnum):
    """How reads are dispatched and merged."""

    PRIMARY_ONLY = "primary_only"  # Query primary only
    FANOUT_MERGE = "fanout_merge"  # Query all, RRF merge
    CASCADE = "cascade"  # Query primary, fallback if insufficient


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


@dataclass
class CircuitState:
    """Per-provider circuit breaker state."""

    failures: int = 0
    last_failure: float = 0.0
    open_until: float = 0.0  # Monotonic time when circuit can half-open

    FAILURE_THRESHOLD: int = 3
    RECOVERY_TIME_S: float = 30.0

    @property
    def is_open(self) -> bool:
        """True if circuit is open (provider should be skipped)."""
        if self.failures < self.FAILURE_THRESHOLD:
            return False
        return time.monotonic() < self.open_until

    def record_failure(self) -> None:
        """Record a failure and potentially open the circuit."""
        self.failures += 1
        self.last_failure = time.monotonic()
        if self.failures >= self.FAILURE_THRESHOLD:
            self.open_until = time.monotonic() + self.RECOVERY_TIME_S

    def record_success(self) -> None:
        """Record a success, resetting the failure counter."""
        self.failures = 0
        self.open_until = 0.0


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class MemoryRouter:
    """Orchestrates memory operations across multiple providers.

    This is the single entry point for all memory operations from the runtime.
    It enforces scope isolation, merges results via RRF, and gracefully
    degrades when providers fail.
    """

    # Timeouts (milliseconds)
    RECALL_TIMEOUT_MS: int = 1500
    WRITE_TIMEOUT_MS: int = 5000

    # RRF parameters
    RRF_K: int = 60  # Smoothing constant

    def __init__(
        self,
        registry: MemoryRegistry,
        write_policy: WritePolicy = WritePolicy.PRIMARY,
        read_policy: ReadPolicy = ReadPolicy.FANOUT_MERGE,
        provider_weights: dict[str, float] | None = None,
        kind_routing: dict[MemoryKind, str] | None = None,
    ) -> None:
        self._registry = registry
        self._write_policy = write_policy
        self._read_policy = read_policy
        self._provider_weights = provider_weights or {}
        self._kind_routing = kind_routing or {}
        self._circuits: dict[str, CircuitState] = {}
        self._bus = get_event_bus()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def write(self, intent: MemoryWrite) -> list[WriteReceipt]:
        """Write a memory record according to the write policy.

        Returns receipts from all providers that successfully persisted.
        """
        # Build the full record
        content_hash = hashlib.sha256(intent.content.encode()).hexdigest()
        record = MemoryRecord(
            id=generate_id("mem_"),
            kind=intent.kind,
            content=intent.content,
            content_hash=content_hash,
            scope=intent.scope,
            provenance=intent.provenance,
            confidence=intent.confidence,
            salience=intent.salience,
            sensitive_class=intent.sensitive_class,
            subject_key=intent.subject_key,
            expires_at=intent.expires_at,
            metadata=intent.metadata,
        )

        targets = self._resolve_write_targets(record)
        if not targets:
            logger.warning("memory_write_no_targets", kind=record.kind)
            return []

        receipts = await self._dispatch_write(record, targets)

        # Emit event
        await self._bus.emit(Event(
            type=EventType.MEMORY_WRITE,
            data={
                "record_id": record.id,
                "kind": record.kind.value,
                "providers": [r.provider for r in receipts],
            },
        ))

        return receipts

    async def update(self, edit: MemoryEdit) -> list[WriteReceipt]:
        """Update an existing record across providers."""
        # For sticky policy, route to origin; otherwise treat like write
        targets: list[MemoryProvider]
        if self._write_policy == WritePolicy.STICKY:
            # Find which provider has this record
            targets = []
            for provider in self._registry.get_healthy():
                existing = await provider.get(edit.id)
                if existing is not None:
                    targets.append(provider)
                    break
            if not targets:
                targets = self._get_primary_list()
        else:
            targets = self._get_primary_list()

        receipts: list[WriteReceipt] = []
        for provider in targets:
            if self._is_circuit_open(provider.name):
                continue
            try:
                receipt = await asyncio.wait_for(
                    provider.update(edit),
                    timeout=self.WRITE_TIMEOUT_MS / 1000,
                )
                receipts.append(receipt)
                self._record_success(provider.name)
            except Exception as exc:  # includes TimeoutError
                self._record_failure(provider.name, exc)

        return receipts

    # ------------------------------------------------------------------
    # Recall
    # ------------------------------------------------------------------

    async def recall(self, query: RecallQuery) -> list[MemoryHit]:
        """Recall memories according to the read policy.

        Returns merged, deduplicated, scope-verified results.
        Returns [] on total failure (I-005: never crash on recall).
        """
        try:
            if self._read_policy == ReadPolicy.PRIMARY_ONLY:
                hits = await self._recall_primary(query)
            elif self._read_policy == ReadPolicy.CASCADE:
                hits = await self._recall_cascade(query)
            else:  # FANOUT_MERGE
                hits = await self._recall_fanout(query)

            # Post-processing
            hits = self._suppress_tombstones(hits)
            hits = self._verify_scope(hits, query.scope)
            hits = self._deduplicate_hits(hits)

            # Sort by fused score descending
            hits.sort(key=lambda h: h.score, reverse=True)

            # Apply limit
            if query.limit:
                hits = hits[: query.limit]

            # Emit event
            await self._bus.emit(Event(
                type=EventType.MEMORY_RECALL,
                data={
                    "query_text": query.text[:50] if query.text else None,
                    "results": len(hits),
                    "kinds": [k.value for k in query.kinds] if query.kinds else None,
                },
            ))

            return hits

        except Exception as exc:
            # I-005: recall never raises to the runtime
            logger.error("memory_recall_total_failure", error=str(exc), exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Forget
    # ------------------------------------------------------------------

    async def forget(self, selector: ForgetSelector) -> list[ForgetReceipt]:
        """Forget (tombstone) records across all providers."""
        targets = self._registry.get_healthy()
        receipts: list[ForgetReceipt] = []

        for provider in targets:
            if self._is_circuit_open(provider.name):
                continue
            try:
                receipt = await asyncio.wait_for(
                    provider.forget(selector),
                    timeout=self.WRITE_TIMEOUT_MS / 1000,
                )
                receipts.append(receipt)
                self._record_success(provider.name)
            except Exception as exc:  # includes TimeoutError
                self._record_failure(provider.name, exc)

        # Emit event
        await self._bus.emit(Event(
            type=EventType.MEMORY_FORGET,
            data={
                "selector_ids": list(selector.ids) if selector.ids else None,
                "providers": [r.provider for r in receipts],
                "total_forgotten": sum(r.count for r in receipts),
            },
        ))

        return receipts

    # ------------------------------------------------------------------
    # Get
    # ------------------------------------------------------------------

    async def get(self, record_id: str) -> MemoryRecord | None:
        """Retrieve a single record by ID from any provider."""
        for provider in self._registry.get_healthy():
            if self._is_circuit_open(provider.name):
                continue
            try:
                record = await asyncio.wait_for(
                    provider.get(record_id),
                    timeout=self.RECALL_TIMEOUT_MS / 1000,
                )
                if record is not None:
                    self._record_success(provider.name)
                    return record
            except Exception as exc:  # includes TimeoutError
                self._record_failure(provider.name, exc)

        return None

    # ------------------------------------------------------------------
    # Internal: write dispatch
    # ------------------------------------------------------------------

    def _resolve_write_targets(self, record: MemoryRecord) -> list[MemoryProvider]:
        """Determine which providers should receive a write."""
        match self._write_policy:
            case WritePolicy.PRIMARY:
                return self._get_primary_list()
            case WritePolicy.MIRROR_ALL:
                return self._registry.get_healthy()
            case WritePolicy.BY_KIND:
                target_name = self._kind_routing.get(record.kind)
                if target_name:
                    provider = self._registry.get(target_name)
                    if provider:
                        return [provider]
                # Fallback to primary
                return self._get_primary_list()
            case WritePolicy.STICKY:
                return self._get_primary_list()
            case _:
                return self._get_primary_list()

    async def _dispatch_write(
        self, record: MemoryRecord, targets: list[MemoryProvider]
    ) -> list[WriteReceipt]:
        """Dispatch write to targets with timeout and circuit breaker."""
        receipts: list[WriteReceipt] = []

        async def _write_one(provider: MemoryProvider) -> WriteReceipt | None:
            if self._is_circuit_open(provider.name):
                return None
            try:
                receipt = await asyncio.wait_for(
                    provider.write(record),
                    timeout=self.WRITE_TIMEOUT_MS / 1000,
                )
                self._record_success(provider.name)
                return receipt
            except Exception as exc:  # includes TimeoutError
                self._record_failure(provider.name, exc)
                return None

        # Use TaskGroup so one provider failing doesn't cancel others
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(_write_one(p)) for p in targets]

        for task in tasks:
            result = task.result()
            if result is not None:
                receipts.append(result)

        return receipts

    # ------------------------------------------------------------------
    # Internal: read strategies
    # ------------------------------------------------------------------

    async def _recall_primary(self, query: RecallQuery) -> list[MemoryHit]:
        """Recall from primary provider only."""
        primary = self._registry.get_primary()
        if primary is None or self._is_circuit_open(primary.name):
            return []

        try:
            hits = await asyncio.wait_for(
                primary.recall(query),
                timeout=self.RECALL_TIMEOUT_MS / 1000,
            )
            self._record_success(primary.name)
            return list(hits)
        except Exception as exc:  # includes TimeoutError
            self._record_failure(primary.name, exc)
            return []

    async def _recall_cascade(self, query: RecallQuery) -> list[MemoryHit]:
        """Recall from primary, fallback to others if results are sparse."""
        hits = await self._recall_primary(query)
        if len(hits) >= query.limit:
            return hits

        # Cascade to the remaining providers.
        #
        # The primary is identified by name. Comparing against the primary
        # instance's class name never matched (`LocalProvider` vs `local`), so
        # the primary was silently re-queried on every cascade.
        primary = self._registry.get_primary()
        primary_name = primary.name if primary is not None else None

        for provider in self._registry.get_healthy():
            if provider.name == primary_name:
                continue
            if self._is_circuit_open(provider.name):
                continue

            remaining = query.limit - len(hits)
            if remaining <= 0:
                break

            try:
                extra = await asyncio.wait_for(
                    provider.recall(query),
                    timeout=self.RECALL_TIMEOUT_MS / 1000,
                )
                # Honour the caller's limit rather than overshooting it.
                hits.extend(extra[:remaining])
                self._record_success(provider.name)
            except Exception as exc:  # includes TimeoutError
                self._record_failure(provider.name, exc)

        return hits

    async def _recall_fanout(self, query: RecallQuery) -> list[MemoryHit]:
        """Fan out query to all healthy providers, merge via RRF.

        Uses asyncio.TaskGroup — one provider failing doesn't cancel others.
        """
        providers = self._registry.get_healthy()
        if not providers:
            return []

        provider_results: dict[str, list[MemoryHit]] = {}

        async def _query_one(provider: MemoryProvider) -> tuple[str, list[MemoryHit]]:
            if self._is_circuit_open(provider.name):
                return provider.name, []
            try:
                hits = await asyncio.wait_for(
                    provider.recall(query),
                    timeout=self.RECALL_TIMEOUT_MS / 1000,
                )
                self._record_success(provider.name)
                return provider.name, list(hits)
            except Exception as exc:  # includes TimeoutError
                self._record_failure(provider.name, exc)
                return provider.name, []

        # TaskGroup ensures one failure doesn't cancel the rest
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(_query_one(p)) for p in providers]

        for task in tasks:
            name, hits = task.result()
            if hits:
                provider_results[name] = hits

        # Merge via Reciprocal Rank Fusion
        return self._rrf_merge(provider_results)

    # ------------------------------------------------------------------
    # Reciprocal Rank Fusion
    # ------------------------------------------------------------------

    def _rrf_merge(self, provider_results: dict[str, list[MemoryHit]]) -> list[MemoryHit]:
        """Merge results from multiple providers using weighted RRF.

        Score = sum over providers: weight_p / (k + rank_p)
        where k=60 (smoothing), and weight defaults to 1.0 per provider.
        """
        # Map record_id → {fused_score, best_hit}
        fused: dict[str, tuple[Decimal, MemoryHit]] = {}

        for provider_name, hits in provider_results.items():
            weight = Decimal(str(self._provider_weights.get(provider_name, 1.0)))

            for rank, hit in enumerate(hits, start=1):
                rrf_score = weight / (Decimal(self.RRF_K) + Decimal(rank))
                record_id = hit.record.id

                if record_id in fused:
                    existing_score, existing_hit = fused[record_id]
                    fused[record_id] = (existing_score + rrf_score, existing_hit)
                else:
                    fused[record_id] = (rrf_score, hit)

        # Rebuild hits with fused scores
        merged: list[MemoryHit] = []
        for fused_score, hit in fused.values():
            merged.append(MemoryHit(
                record=hit.record,
                score=fused_score,
                provider=hit.provider,
                match_type=hit.match_type,
            ))

        return merged

    # ------------------------------------------------------------------
    # Post-processing
    # ------------------------------------------------------------------

    def _suppress_tombstones(self, hits: list[MemoryHit]) -> list[MemoryHit]:
        """Remove hits that have been superseded (tombstoned)."""
        # Collect IDs that are superseded
        superseded_ids: set[str] = set()
        for hit in hits:
            if hit.record.superseded_by:
                superseded_ids.add(hit.record.id)

        return [h for h in hits if h.record.id not in superseded_ids]

    def _verify_scope(self, hits: list[MemoryHit], query_scope: Scope | None) -> list[MemoryHit]:
        """Client-side scope re-verification (I-017).

        Even though providers filter by scope in their queries,
        we re-verify here as a defense-in-depth measure.
        """
        if query_scope is None:
            return hits

        verified: list[MemoryHit] = []
        for hit in hits:
            record_scope = hit.record.scope
            # User must match
            if record_scope.user_id != query_scope.user_id:
                continue
            # Visibility-based filtering
            match record_scope.visibility:
                case "global":
                    verified.append(hit)
                case "project":
                    if record_scope.project_id == query_scope.project_id:
                        verified.append(hit)
                case "session":
                    if record_scope.session_id == query_scope.session_id:
                        verified.append(hit)
                case "agent":
                    if record_scope.agent_id == query_scope.agent_id:
                        verified.append(hit)
                case _:
                    verified.append(hit)

        return verified

    def _deduplicate_hits(self, hits: list[MemoryHit]) -> list[MemoryHit]:
        """Deduplicate by record ID, keeping highest-scored instance."""
        seen: dict[str, MemoryHit] = {}
        for hit in hits:
            existing = seen.get(hit.record.id)
            if existing is None or hit.score > existing.score:
                seen[hit.record.id] = hit
        return list(seen.values())

    # ------------------------------------------------------------------
    # Circuit breaker helpers
    # ------------------------------------------------------------------

    def _get_circuit(self, provider_name: str) -> CircuitState:
        """Get or create circuit state for a provider."""
        if provider_name not in self._circuits:
            self._circuits[provider_name] = CircuitState()
        return self._circuits[provider_name]

    def _is_circuit_open(self, provider_name: str) -> bool:
        """Check if a provider's circuit is open."""
        return self._get_circuit(provider_name).is_open

    def _record_failure(self, provider_name: str, exc: BaseException) -> None:
        """Record a provider failure."""
        circuit = self._get_circuit(provider_name)
        circuit.record_failure()
        logger.debug(
            "memory_provider_failure",
            provider=provider_name,
            failures=circuit.failures,
            error=str(exc),
        )

    def _record_success(self, provider_name: str) -> None:
        """Record a provider success."""
        self._get_circuit(provider_name).record_success()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_primary_list(self) -> list[MemoryProvider]:
        """Get the primary provider as a single-element list."""
        primary = self._registry.get_primary()
        return [primary] if primary else []
