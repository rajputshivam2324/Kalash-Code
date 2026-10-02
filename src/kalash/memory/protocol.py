"""Memory provider protocol and data types.

This module defines the contract every memory provider must fulfill,
plus all value types that flow through the memory subsystem.

Design invariants:
- All data types are frozen dataclasses (immutable after creation).
- Numeric fields use Decimal to avoid float drift across serialization.
- MemoryRecord.id is always a ULID with 'mem_' prefix.
- Scope isolation is the responsibility of the router, not the provider.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class MemoryKind(StrEnum):
    """Classification of memory records."""

    SESSION = "session"  # Short-lived turn context
    WORKING = "working"  # Active task scratchpad
    SEMANTIC = "semantic"  # General knowledge facts
    EPISODIC = "episodic"  # Timestamped events / past actions
    PROCEDURAL = "procedural"  # How-to knowledge, workflows
    ENTITY = "entity"  # Named entity graph nodes
    ARTIFACT = "artifact"  # File references, code snippets, links


class ProviderCapability(StrEnum):
    """Capabilities a provider can declare."""

    SEMANTIC_SEARCH = "semantic_search"
    KEYWORD_SEARCH = "keyword_search"
    HYBRID_SEARCH = "hybrid_search"
    GRAPH = "graph"
    LLM_EXTRACTION = "llm_extraction"
    TIME_DECAY = "time_decay"
    NAMESPACES = "namespaces"
    HISTORY = "history"
    TTL = "ttl"
    BULK_EXPORT = "bulk_export"
    RERANK = "rerank"
    ATTACHMENTS = "attachments"


class Visibility(StrEnum):
    """Scope visibility levels (broadest → narrowest)."""

    GLOBAL = "global"
    PROJECT = "project"
    SESSION = "session"
    AGENT = "agent"


class Source(StrEnum):
    """How a memory was acquired."""

    USER_STATED = "user_stated"
    TOOL_DERIVED = "tool_derived"
    AGENT_INFERRED = "agent_inferred"
    WEB_DERIVED = "web_derived"
    IMPORTED = "imported"


class Trust(StrEnum):
    """Trust level assigned to a memory."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class Scope:
    """Defines who can see a memory record."""

    user_id: str
    project_id: str | None = None
    org_id: str | None = None
    repo_id: str | None = None
    branch: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    visibility: Visibility = Visibility.PROJECT


@dataclass(frozen=True, slots=True)
class Provenance:
    """Lineage metadata for a memory record."""

    source: Source
    trust: Trust = Trust.MEDIUM
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    origin_provider: str | None = None
    session_id: str | None = None
    turn_seq: int | None = None
    tool_call_id: str | None = None
    file_path: str | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """A single memory record stored across providers."""

    id: str  # ULID with mem_ prefix
    kind: MemoryKind
    content: str
    content_hash: str  # SHA-256 hex of content
    scope: Scope
    provenance: Provenance
    confidence: Decimal = field(default_factory=lambda: Decimal("1.0"))
    salience: Decimal = field(default_factory=lambda: Decimal("0.5"))
    sensitive_class: str | None = None  # e.g. "secret", "pii", "credential"
    subject_key: str | None = None  # Entity or topic key for graph lookups
    expires_at: datetime | None = None
    superseded_by: str | None = None  # ID of the record that replaces this one
    revision: int = 1
    embedding_model_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryWrite:
    """Intent to write a new memory record."""

    kind: MemoryKind
    content: str
    scope: Scope
    provenance: Provenance
    confidence: Decimal = field(default_factory=lambda: Decimal("1.0"))
    salience: Decimal = field(default_factory=lambda: Decimal("0.5"))
    sensitive_class: str | None = None
    subject_key: str | None = None
    expires_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryEdit:
    """Intent to update an existing memory record."""

    id: str
    content: str | None = None
    confidence: Decimal | None = None
    salience: Decimal | None = None
    sensitive_class: str | None = None
    subject_key: str | None = None
    expires_at: datetime | None = None
    superseded_by: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RecallQuery:
    """Parameters for a memory recall operation."""

    text: str | None = None
    kinds: Sequence[MemoryKind] | None = None
    scope: Scope | None = None
    subject_key: str | None = None
    min_confidence: Decimal = field(default_factory=lambda: Decimal("0.3"))
    min_salience: Decimal = field(default_factory=lambda: Decimal("0.0"))
    limit: int = 25
    include_superseded: bool = False
    include_expired: bool = False
    after: datetime | None = None
    before: datetime | None = None
    metadata_filter: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ForgetSelector:
    """Selects which records to forget (tombstone)."""

    ids: Sequence[str] | None = None
    kinds: Sequence[MemoryKind] | None = None
    scope: Scope | None = None
    subject_key: str | None = None
    before: datetime | None = None


@dataclass(frozen=True, slots=True)
class MemoryHit:
    """A single result from a recall query, scored."""

    record: MemoryRecord
    score: Decimal  # Fused relevance score [0, 1]
    provider: str  # Which provider returned this
    match_type: str = "semantic"  # semantic, keyword, hybrid, graph


@dataclass(frozen=True, slots=True)
class WriteReceipt:
    """Confirmation of a successful write to a provider."""

    record_id: str
    provider: str
    written_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    revision: int = 1


@dataclass(frozen=True, slots=True)
class ForgetReceipt:
    """Confirmation of a forget (tombstone) operation."""

    count: int
    provider: str
    forgotten_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """Health status of a memory provider."""

    provider: str
    healthy: bool
    latency_ms: float | None = None
    last_check: datetime = field(default_factory=lambda: datetime.now(UTC))
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderStats:
    """Statistics from a memory provider."""

    provider: str
    total_records: int = 0
    total_tombstones: int = 0
    kinds_breakdown: dict[str, int] = field(default_factory=dict)
    storage_bytes: int = 0
    oldest_record: datetime | None = None
    newest_record: datetime | None = None


@runtime_checkable
class MemoryProvider(Protocol):
    """Protocol that all memory providers must satisfy.

    Providers are responsible for storage and retrieval only.
    Scope filtering, deduplication, and federation are handled by the router.
    """

    @property
    def name(self) -> str:
        """Unique provider identifier (e.g. 'local', 'mem0', 'zep')."""
        ...

    @property
    def capabilities(self) -> frozenset[ProviderCapability]:
        """Declared capabilities of this provider."""
        ...

    async def health(self) -> ProviderHealth:
        """Check provider health and connectivity."""
        ...

    async def write(self, record: MemoryRecord) -> WriteReceipt:
        """Persist a memory record."""
        ...

    async def recall(self, query: RecallQuery) -> Sequence[MemoryHit]:
        """Retrieve memory records matching the query."""
        ...

    async def get(self, record_id: str) -> MemoryRecord | None:
        """Retrieve a single record by ID."""
        ...

    async def update(self, edit: MemoryEdit) -> WriteReceipt:
        """Update an existing record (creates new revision)."""
        ...

    async def forget(self, selector: ForgetSelector) -> ForgetReceipt:
        """Tombstone records matching the selector."""
        ...

    async def history(self, record_id: str) -> Sequence[MemoryRecord]:
        """Get all revisions of a record (newest first)."""
        ...

    def export(self, scope: Scope) -> AsyncIterator[MemoryRecord]:
        """Bulk export all records within a scope."""
        ...

    async def stats(self) -> ProviderStats:
        """Get provider statistics."""
        ...
