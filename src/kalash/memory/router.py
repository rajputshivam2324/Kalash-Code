"""Bounded provider dispatch and scope verification for saved facts."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

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
    RecallQuery,
    Scope,
    WriteReceipt,
)
from kalash.memory.registry import MemoryRegistry

logger = logging.getLogger(__name__)


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


class MemoryRouter:
    """Use one dispatch path for every operation; filter scope before ranking."""

    RECALL_TIMEOUT_MS = 1500
    WRITE_TIMEOUT_MS = 5000
    RRF_K = 60

    def __init__(
        self,
        registry: MemoryRegistry,
        write_policy: WritePolicy = WritePolicy.PRIMARY,
        read_policy: ReadPolicy = ReadPolicy.PRIMARY_ONLY,
        provider_weights: dict[str, float] | None = None,
        kind_routing: dict[MemoryKind, str] | None = None,
    ) -> None:
        self._registry = registry
        self._write_policy = write_policy
        self._read_policy = read_policy
        self._provider_weights = provider_weights or {}
        self._kind_routing = kind_routing or {}
        self._circuits: dict[str, CircuitState] = {}

    async def _call(self, provider: MemoryProvider, operation: str, argument: Any) -> Any:
        circuit = self._circuits.setdefault(provider.name, CircuitState())
        if circuit.is_open:
            return None
        timeout = (
            self.RECALL_TIMEOUT_MS if operation in {"recall", "get"} else self.WRITE_TIMEOUT_MS
        )
        try:
            result = await asyncio.wait_for(
                getattr(provider, operation)(argument),
                timeout=timeout / 1000,
            )
        except Exception:
            circuit.record_failure()
            logger.warning("Memory %s failed on %s", operation, provider.name, exc_info=True)
            return None
        circuit.record_success()
        return result

    def _primary(self) -> list[MemoryProvider]:
        primary = self._registry.get_primary()
        return [primary] if primary is not None else []

    async def _dispatch(
        self,
        providers: list[MemoryProvider],
        operation: str,
        argument: Any,
    ) -> list[Any]:
        results = await asyncio.gather(*(self._call(p, operation, argument) for p in providers))
        return [result for result in results if result is not None]

    async def write(self, intent: MemoryWrite) -> list[WriteReceipt]:
        record = MemoryRecord(
            id=generate_id("mem_"),
            content_hash=hashlib.sha256(intent.content.encode()).hexdigest(),
            **{name: getattr(intent, name) for name in (f.name for f in fields(intent))},
        )
        targets = self._primary()
        if self._write_policy == WritePolicy.MIRROR_ALL:
            targets = self._registry.get_healthy()
        elif self._write_policy == WritePolicy.BY_KIND:
            target = self._registry.get(self._kind_routing.get(intent.kind, ""))
            if target is not None:
                targets = [target]
        return await self._dispatch(targets, "write", record)

    async def update(self, edit: MemoryEdit) -> list[WriteReceipt]:
        targets = self._primary()
        if self._write_policy == WritePolicy.STICKY:
            for provider in self._registry.get_healthy():
                if await self._call(provider, "get", edit.id) is not None:
                    targets = [provider]
                    break
        elif self._write_policy == WritePolicy.MIRROR_ALL:
            targets = self._registry.get_healthy()
        return await self._dispatch(targets, "update", edit)

    async def recall(self, query: RecallQuery) -> list[MemoryHit]:
        if query.limit <= 0:
            return []
        providers = self._registry.get_healthy()
        if self._read_policy == ReadPolicy.PRIMARY_ONLY:
            providers = self._primary()
        elif self._read_policy == ReadPolicy.CASCADE:
            primary = self._registry.get_primary()
            providers.sort(key=lambda p: p is not primary)
        ranked: dict[str, list[MemoryHit]] = {}
        if self._read_policy == ReadPolicy.CASCADE:
            for provider in providers:
                hits = await self._call(provider, "recall", query) or []
                ranked[provider.name] = self._filter(hits, query)
                if sum(len(hits) for hits in ranked.values()) >= query.limit:
                    break
        else:
            results = await asyncio.gather(*(self._call(p, "recall", query) for p in providers))
            ranked = {
                p.name: self._filter(hits or [], query) for p, hits in zip(providers, results)
            }
        # A single provider's relevance score needs no fusion.
        hits = self._rrf_merge(ranked) if len(ranked) > 1 else next(iter(ranked.values()), [])
        unique: dict[str, MemoryHit] = {}
        for hit in sorted(hits, key=lambda h: h.score, reverse=True):
            unique.setdefault(hit.record.id, hit)
        return list(unique.values())[: query.limit]

    def _filter(self, hits: list[MemoryHit], query: RecallQuery) -> list[MemoryHit]:
        now = datetime.now(UTC)
        return [
            hit
            for hit in hits
            if (query.include_superseded or hit.record.superseded_by is None)
            and (query.scope is None or self.visible(hit.record.scope, query.scope))
            and (
                query.include_expired
                or hit.record.expires_at is None
                or hit.record.expires_at > now
            )
            and hit.record.confidence >= query.min_confidence
            and hit.record.salience >= query.min_salience
        ]

    def _rrf_merge(self, ranked: dict[str, list[MemoryHit]]) -> list[MemoryHit]:
        scores: dict[str, Decimal] = {}
        records: dict[str, MemoryHit] = {}
        for name, hits in ranked.items():
            weight = Decimal(str(self._provider_weights.get(name, 1)))
            for rank, hit in enumerate(hits, start=1):
                records[hit.record.id] = hit
                scores[hit.record.id] = scores.get(hit.record.id, Decimal(0)) + weight / (
                    self.RRF_K + rank
                )
        return [
            MemoryHit(
                record=hit.record,
                provider=hit.provider,
                score=scores[key],
                match_type=hit.match_type,
            )
            for key, hit in records.items()
        ]

    async def get(self, record_id: str) -> MemoryRecord | None:
        for provider in self._registry.get_healthy():
            record = await self._call(provider, "get", record_id)
            if isinstance(record, MemoryRecord):
                return record
        return None

    async def forget(self, selector: ForgetSelector) -> list[ForgetReceipt]:
        if not any(
            (selector.ids, selector.kinds, selector.scope, selector.subject_key, selector.before)
        ):
            raise ValueError("Forgetting memory requires an explicit selector")
        return await self._dispatch(self._registry.get_healthy(), "forget", selector)

    @staticmethod
    def visible(record: Scope, query: Scope) -> bool:
        """Require concrete ownership IDs for every non-global scope."""
        if record.user_id != query.user_id:
            return False
        if record.visibility == "global":
            return True
        if not query.project_id or record.project_id != query.project_id:
            return False
        if record.visibility == "project":
            return True
        if record.visibility == "session":
            return bool(query.session_id) and record.session_id == query.session_id
        if record.visibility == "agent":
            return bool(query.agent_id) and record.agent_id == query.agent_id
        return False
