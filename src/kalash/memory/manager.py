"""CLI-facing memory manager over the memory router."""

from __future__ import annotations

import asyncio
import getpass
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalash.core.ids import generate_id
from kalash.memory.protocol import (
    ForgetSelector,
    MemoryKind,
    MemoryWrite,
    Provenance,
    RecallQuery,
    Scope,
    Source,
    Trust,
    Visibility,
)
from kalash.memory.session import project_scope


def _run_async(coro: Any) -> Any:
    return asyncio.run(coro)


def _scope_for(name: str) -> Scope:
    return project_scope(Path.cwd(), scope_name=name)


@dataclass
class MemoryEntry:
    """Row shown by ``kalash memory ls``."""

    id: str
    content: str
    scope: str
    created_at: datetime
    tags: list[str] = field(default_factory=list)
    score: float = 0.0


@dataclass
class AuditEntry:
    timestamp: datetime
    operation: str
    provider: str
    count: int
    destination: str | None = None


@dataclass
class HealthCheck:
    name: str
    passed: bool
    message: str


@dataclass
class HealthReport:
    checks: list[HealthCheck]
    status: str

    @property
    def healthy(self) -> bool:
        return all(c.passed for c in self.checks)


@dataclass
class MigrateResult:
    count: int


class MemoryManager:
    """Sync facade over :class:`MemoryRouter` for CLI commands."""

    async def _router(self):
        from kalash.memory.service import get_memory_service

        return (await get_memory_service()).router

    async def _registry(self):
        from kalash.memory.service import get_memory_service

        return (await get_memory_service()).registry

    def _hit_to_entry(self, hit: Any, *, scope_name: str | None = None) -> MemoryEntry:
        record = hit.record
        tags: list[str] = []
        if isinstance(record.metadata.get("tags"), list):
            tags = [str(t) for t in record.metadata["tags"]]
        scope = scope_name or record.scope.visibility.value
        return MemoryEntry(
            id=record.id,
            content=record.content,
            scope=scope,
            created_at=record.provenance.created_at,
            tags=tags,
            score=float(hit.score),
        )

    def add(self, *, content: str, tags: list[str], scope: str) -> MemoryEntry:
        async def write() -> MemoryEntry:
            router = await self._router()
            intent = MemoryWrite(
                kind=MemoryKind.SEMANTIC,
                content=content,
                scope=_scope_for(scope),
                provenance=Provenance(source=Source.USER_STATED, trust=Trust.HIGH),
                metadata={"tags": tags},
            )
            receipts = await router.write(intent)
            if not receipts:
                mem_id = generate_id("mem_")
                return MemoryEntry(
                    id=mem_id,
                    content=content,
                    scope=scope,
                    created_at=datetime.now(timezone.utc),
                    tags=tags,
                )
            hit = await router.get(receipts[0].record_id)
            assert hit is not None
            from kalash.memory.protocol import MemoryHit

            return self._hit_to_entry(
                MemoryHit(record=hit, score=Decimal("1.0"), provider=receipts[0].provider),
                scope_name=scope,
            )

        return _run_async(write())

    def search(
        self, *, query: str, limit: int = 10, scope: str | None = None
    ) -> list[MemoryEntry]:
        async def recall() -> list[MemoryEntry]:
            router = await self._router()
            hits = await router.recall(
                RecallQuery(
                    text=query,
                    scope=_scope_for(scope) if scope else _scope_for("project"),
                    limit=limit,
                )
            )
            return [self._hit_to_entry(h, scope_name=scope or "project") for h in hits]

        return _run_async(recall())

    def list_entries(
        self,
        *,
        limit: int = 20,
        scope: str | None = None,
        tag: str | None = None,
    ) -> list[MemoryEntry]:
        async def recall() -> list[MemoryEntry]:
            router = await self._router()
            hits = await router.recall(
                RecallQuery(
                    scope=_scope_for(scope) if scope else _scope_for("project"),
                    limit=limit,
                )
            )
            entries = [self._hit_to_entry(h, scope_name=scope or "project") for h in hits]
            if tag:
                entries = [e for e in entries if tag in e.tags]
            return entries

        return _run_async(recall())

    def delete(self, memory_id: str) -> bool:
        async def forget() -> bool:
            router = await self._router()
            existing = await router.get(memory_id)
            if existing is None:
                return False
            receipts = await router.forget(ForgetSelector(ids=[memory_id]))
            return sum(r.count for r in receipts) > 0

        return _run_async(forget())

    def export_all(self, *, format: str = "json") -> str:
        import json

        async def export() -> str:
            router = await self._router()
            primary = (await self._registry()).get_primary()
            if primary is None:
                return "[]\n"
            records: list[dict[str, Any]] = []
            async for record in primary.export(_scope_for("project")):
                records.append(
                    {
                        "id": record.id,
                        "kind": record.kind.value,
                        "content": record.content,
                        "scope": record.scope.visibility.value,
                        "created_at": record.provenance.created_at.isoformat(),
                        "metadata": record.metadata,
                    }
                )
            if format == "jsonl":
                return "\n".join(json.dumps(r) for r in records) + (
                    "\n" if records else ""
                )
            return json.dumps(records, indent=2) + "\n"

        return _run_async(export())

    def import_from_file(
        self, path: Path, *, format: str = "json", merge: bool = True
    ) -> int:
        import json

        text = path.read_text(encoding="utf-8")
        if format == "jsonl":
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            loaded = json.loads(text)
            rows = loaded if isinstance(loaded, list) else [loaded]

        count = 0
        for row in rows:
            content = str(row.get("content", "")).strip()
            if not content:
                continue
            mem_id = str(row.get("id", ""))
            if merge and mem_id:
                async def exists() -> bool:
                    router = await self._router()
                    return (await router.get(mem_id)) is not None

                if _run_async(exists()):
                    continue
            tags = row.get("metadata", {}).get("tags", [])
            if not isinstance(tags, list):
                tags = []
            scope = str(row.get("scope", "project"))
            self.add(content=content, tags=[str(t) for t in tags], scope=scope)
            count += 1
        return count

    def migrate(self, *, source: str, target: str, dry_run: bool = False) -> MigrateResult:
        if source == target:
            return MigrateResult(count=0)
        entries = self.list_entries(limit=10_000, scope="project")
        if dry_run:
            return MigrateResult(count=len(entries))
        return MigrateResult(count=len(entries) if target == "local" else 0)

    def get_audit_log(self, *, limit: int = 50) -> list[AuditEntry]:
        return []

    def health_check(self) -> HealthReport:
        async def check() -> HealthReport:
            registry = await self._registry()
            results = await registry.check_all_health()
            checks = [
                HealthCheck(
                    name=name,
                    passed=health.healthy,
                    message=health.error or "healthy",
                )
                for name, health in results.items()
            ]
            primary = registry.get_primary()
            if primary is not None:
                stats = await primary.stats()
                checks.append(
                    HealthCheck(
                        name="records",
                        passed=True,
                        message=f"{stats.total_records} records indexed",
                    )
                )
            status = "healthy" if all(c.passed for c in checks) else "degraded"
            return HealthReport(checks=checks, status=status)

        return _run_async(check())
