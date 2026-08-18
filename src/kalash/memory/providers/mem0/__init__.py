"""Optional mem0 cloud memory provider."""

from __future__ import annotations

from typing import Any

from kalash.memory.protocol import (
    ForgetReceipt,
    ForgetSelector,
    MemoryEdit,
    MemoryHit,
    MemoryKind,
    MemoryRecord,
    MemoryWrite,
    ProviderCapability,
    ProviderHealth,
    ProviderStats,
    RecallQuery,
    Scope,
    WriteReceipt,
)


class Mem0Provider:
    """Wrap mem0ai when installed. Falls back gracefully otherwise."""

    name = "mem0"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._client: Any = None

    @property
    def capabilities(self) -> frozenset[ProviderCapability]:
        return frozenset(
            {
                ProviderCapability.SEMANTIC_SEARCH,
                ProviderCapability.LLM_EXTRACTION,
                ProviderCapability.TIME_DECAY,
            }
        )

    async def initialize(self) -> None:
        try:
            from mem0 import Memory  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "mem0 provider requires `uv sync --extra mem0`"
            ) from exc
        self._client = Memory.from_config(self._config or {})

    async def health(self) -> ProviderHealth:
        if self._client is None:
            return ProviderHealth(provider=self.name, healthy=False, error="not initialized")
        return ProviderHealth(provider=self.name, healthy=True)

    async def stats(self) -> ProviderStats:
        return ProviderStats(provider=self.name, total_records=0)

    async def write(self, record: MemoryRecord) -> WriteReceipt:
        raise NotImplementedError("mem0 write mapping pending")

    async def update(self, edit: MemoryEdit) -> WriteReceipt:
        raise NotImplementedError("mem0 update mapping pending")

    async def recall(self, query: RecallQuery) -> list[MemoryHit]:
        if self._client is None or not query.text:
            return []
        # Placeholder until full mem0 protocol mapping lands.
        return []

    async def forget(self, selector: ForgetSelector) -> ForgetReceipt:
        return ForgetReceipt(provider=self.name, count=0)

    async def get(self, record_id: str) -> MemoryRecord | None:
        return None

    async def export(self, scope: Scope):
        if False:
            yield  # pragma: no cover
        return


def create_mem0_provider(config: dict[str, Any]) -> Mem0Provider:
    return Mem0Provider(config)
