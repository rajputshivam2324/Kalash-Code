"""Session-scoped memory access for tools and turn-start recall."""

from __future__ import annotations

import getpass
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalash.core.config import KalashConfig, load_config
from kalash.core.ids import generate_id
from kalash.memory.pipeline.inject import build_memory_block
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
from kalash.memory.router import MemoryRouter

logger = logging.getLogger(__name__)


def project_scope(
    cwd: Path,
    *,
    session_id: str | None = None,
    scope_name: str = "project",
) -> Scope:
    """Build a scope for the current workspace."""
    user_id = getpass.getuser()
    visibility = {
        "global": Visibility.GLOBAL,
        "project": Visibility.PROJECT,
        "session": Visibility.SESSION,
        "user": Visibility.GLOBAL,
        "all": Visibility.PROJECT,
    }.get(scope_name, Visibility.PROJECT)
    return Scope(
        user_id=user_id,
        project_id=str(cwd.resolve()),
        session_id=session_id if scope_name == "session" else None,
        visibility=visibility,
    )


class SessionMemory:
    """Memory operations scoped to one agent session."""

    def __init__(self, session_id: str, cwd: Path, config: KalashConfig | None = None) -> None:
        self.session_id = session_id
        self.cwd = cwd.resolve()
        self.config = config

    async def _router(self) -> MemoryRouter:
        from kalash.memory.service import get_memory_service

        return (await get_memory_service(config=self.config)).router

    async def recall_blocks(
        self,
        query: str,
        *,
        limit: int = 8,
        existing_context: str = "",
        context_window: int = 32_000,
    ) -> list[str]:
        """Return formatted memory blocks for context injection."""
        config = self.config or load_config(self.cwd)
        router = await self._router()

        budget_tokens = max(0, min(2_000, int(context_window * config.memory.recall_budget)))

        scope = project_scope(self.cwd, session_id=self.session_id)
        hits = await router.recall(
            RecallQuery(
                text=query or None,
                scope=scope,
                limit=limit,
                min_confidence=Decimal("0.3"),
            )
        )

        block = await build_memory_block(
            list(hits),
            budget_tokens=budget_tokens,
            existing_context=existing_context,
        )
        if not block.rendered:
            return []
        return [block.rendered]

    async def recall_tool(
        self,
        query: str,
        *,
        limit: int = 10,
        scope: str = "all",
    ) -> list[dict[str, Any]]:
        router = await self._router()
        scope_name = scope if scope != "all" else "project"
        scope_obj = project_scope(
            self.cwd,
            session_id=self.session_id,
            scope_name=scope_name,
        )
        hits = await router.recall(RecallQuery(text=query, scope=scope_obj, limit=limit))
        return [
            {
                "id": hit.record.id,
                "content": hit.record.content,
                "score": float(hit.score),
                "kind": hit.record.kind.value,
            }
            for hit in hits
        ]

    async def remember(
        self,
        content: str,
        *,
        scope: str = "project",
        tags: list[str] | None = None,
    ) -> str:
        router = await self._router()
        mem_id = generate_id("mem_")
        intent = MemoryWrite(
            kind=MemoryKind.SEMANTIC,
            content=content,
            scope=project_scope(
                self.cwd,
                session_id=self.session_id,
                scope_name=scope,
            ),
            provenance=Provenance(
                source=Source.USER_STATED,
                trust=Trust.HIGH,
                session_id=self.session_id,
            ),
            metadata={"tags": tags or [], "requested_id": mem_id},
        )
        receipts = await router.write(intent)
        if not receipts:
            raise RuntimeError("Memory was not saved: no provider accepted the write")
        return receipts[0].record_id

    async def forget(
        self,
        *,
        memory_id: str = "",
        content_match: str = "",
    ) -> int:
        router = await self._router()
        if memory_id:
            record = await router.get(memory_id)
            scope = project_scope(self.cwd, session_id=self.session_id)
            if record is None or not router.visible(record.scope, scope):
                return 0
            receipts = await router.forget(ForgetSelector(ids=[memory_id]))
            return sum(r.count for r in receipts)
        if content_match:
            hits = await router.recall(
                RecallQuery(
                    text=content_match,
                    scope=project_scope(self.cwd),
                    limit=20,
                )
            )
            ids = [h.record.id for h in hits if content_match in h.record.content]
            if not ids:
                return 0
            receipts = await router.forget(ForgetSelector(ids=ids))
            return sum(r.count for r in receipts)
        return 0


def get_session_memory(
    session_id: str, cwd: Path, config: KalashConfig | None = None
) -> SessionMemory:
    return SessionMemory(session_id, cwd, config)
