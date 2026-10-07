"""Durable attempt/effect facts and session accounting. Never replay effects."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from kalash.core.ids import generate_id

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from kalash.core.budget import BudgetState
    from kalash.storage.repositories.sessions import SessionRepository

COUNTERS = (
    "tokens_used",
    "cost_used",
    "wallclock_used_s",
    "turns_used",
    "tool_calls_used",
    "unknown_usage_attempts",
    "unknown_tokens_reserved",
    "unknown_cost_reserved",
)


class JournalError(RuntimeError):
    """A durable fact could not be committed; dependent effects must stop."""


def budget_snapshot(budget: BudgetState) -> dict[str, Any]:
    return {
        key: str(value) if isinstance(value := getattr(budget, key), Decimal) else value
        for key in COUNTERS
    }


@dataclass
class RuntimeJournal:
    repository: SessionRepository
    session_id: str
    run_id: str = ""
    summary: str | None = None

    async def _rows(
        self, kinds: tuple[str, ...], *, after: str = "", compact: bool = False
    ) -> AsyncIterator[dict[str, Any]]:
        """Keyset pages bound payload buffering and avoid old request snapshots."""
        cursor = (after, "\uffff" if after else "")
        projection = (
            "json_remove(payload, '$.messages', '$.system', '$.tools', '$.content', "
            "'$.text', '$.metadata', '$.conversation') AS payload"
            if compact
            else "payload"
        )
        placeholders = ",".join("?" for _ in kinds)
        while True:
            rows = await self.repository.engine.execute_read_async(
                f"SELECT id, created_at, kind, run_id, {projection} FROM runtime_journal "  # noqa: S608 -- only fixed SQL fragments; values use placeholders
                f"WHERE session_id=? AND kind IN ({placeholders}) "
                "AND (created_at, id) > (?, ?) ORDER BY created_at, id LIMIT 16",
                (self.session_id, *kinds, *cursor),
            )
            if not rows:
                return
            for row in rows:
                yield dict(row)
            cursor = (rows[-1]["created_at"], rows[-1]["id"])

    async def append(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        budget: BudgetState | None = None,
        summary: str | None = None,
    ) -> str:
        try:
            return await self._append(kind, payload, budget=budget, summary=summary)
        except Exception as error:
            raise JournalError(
                f"Could not persist execution state; stopped before dependent effects: {error}"
            ) from error

    async def _append(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        budget: BudgetState | None = None,
        summary: str | None = None,
    ) -> str:
        event_id = generate_id("evt_")
        now = datetime.now(UTC).isoformat()
        if summary is not None:
            self.summary = summary
        async with self.repository.engine.write() as connection:
            connection.execute(
                "INSERT INTO runtime_journal VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, self.session_id, self.run_id, kind, json.dumps(payload), now),
            )
            if budget is not None:
                connection.execute(
                    """INSERT INTO runtime_checkpoints VALUES (?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET budget=excluded.budget,
                    summary=excluded.summary, updated_at=excluded.updated_at""",
                    (self.session_id, json.dumps(budget_snapshot(budget)), self.summary, now),
                )
            connection.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?", (now, self.session_id)
            )
        return event_id

    async def recover(self, budget: BudgetState) -> str | None:
        rows = await self.repository.engine.execute_read_async(
            "SELECT * FROM runtime_checkpoints WHERE session_id=?", (self.session_id,)
        )
        if rows:
            values = json.loads(rows[0]["budget"])
            for key in COUNTERS:
                value = values.get(key, 0)
                setattr(
                    budget,
                    key,
                    Decimal(str(value)) if key in {"cost_used", "unknown_cost_reserved"} else value,
                )
            self.summary = rows[0]["summary"]
            return self.summary
        # Preserve legacy reported tokens. Legacy unknown costs are not fabricated.
        rows = await self.repository.engine.execute_read_async(
            "SELECT COALESCE(SUM(token_count), 0) AS tokens FROM turns WHERE session_id=?",
            (self.session_id,),
        )
        budget.tokens_used = int(rows[0]["tokens"])
        return None

    async def unsettled(self) -> list[dict[str, Any]]:
        pending: dict[str, dict[str, Any]] = {}
        async for row in self._rows(
            (
                "attempt_started",
                "effect_started",
                "provider_dispatch",
                "retry_uncertain",
                "attempt_settled",
                "effect_settled",
                "recovery_uncertain",
            ),
            compact=True,
        ):
            payload = json.loads(row["payload"])
            key = payload.get("effect_id") or payload.get("attempt_id")
            if not key:
                continue
            if row["kind"] in {"attempt_started", "effect_started"}:
                pending[key] = {"kind": row["kind"], "run_id": row["run_id"], **payload}
            elif row["kind"] == "provider_dispatch" and key in pending:
                pending[key].update(payload)
                pending[key]["reservation_held"] = False
            elif row["kind"] == "retry_uncertain" and key in pending:
                pending[key]["reservation_held"] = True
            elif row["kind"] in {"attempt_settled", "effect_settled", "recovery_uncertain"}:
                pending.pop(key, None)
        return list(pending.values())

    async def recover_messages(self) -> list[Any]:
        """Restore settled receipts across the journal-to-transcript crash gap.

        Transcript insertion and its source receipt share a transaction. A second
        recovery sees the receipt and cannot duplicate or execute the effect.
        """
        from kalash.models.normalize import Role, TextBlock, ToolResultBlock
        from kalash.runtime.serialize import (
            deserialize_blocks,
            rehydrate_messages,
            serialize_blocks,
        )

        persisted = {
            json.loads(row["payload"])["source_id"]
            async for row in self._rows(("message_persisted",), compact=True)
        }
        latest = await self.repository.engine.execute_read_async(
            "SELECT created_at, payload FROM runtime_journal WHERE session_id=? "
            "AND kind='compaction' AND json_type(payload, '$.conversation')='array' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (self.session_id,),
        )
        projection = (
            (latest[0]["created_at"], json.loads(latest[0]["payload"])["conversation"])
            if latest
            else None
        )
        settled = {
            json.loads(row["payload"])["attempt_id"]
            async for row in self._rows(("attempt_settled",), compact=True)
        }
        nonconversation = {
            json.loads(row["payload"])["attempt_id"]
            async for row in self._rows(("attempt_started",), compact=True)
            if not json.loads(row["payload"]).get("recoverable_response", True)
        }
        progress: dict[str, list[str]] = {}
        cutoff = projection[0] if projection else ""
        async for row in self._rows(("response_progress",), after=cutoff):
            payload = json.loads(row["payload"])
            if (
                payload["attempt_id"] not in settled
                and payload["attempt_id"] not in nonconversation
            ):
                progress.setdefault(payload["attempt_id"], []).append(payload["text"])
        async for row in self._rows(("attempt_settled", "effect_settled"), after=cutoff):
            payload = json.loads(row["payload"])
            source_id = None
            blocks = []
            role = Role.ASSISTANT
            if row["kind"] == "attempt_settled" and payload.get("recoverable_response", True):
                source_id = payload["attempt_id"]
                blocks = deserialize_blocks(payload.get("content"))
                if payload.get("error"):
                    blocks = [b for b in blocks if isinstance(b, TextBlock)]
            elif row["kind"] == "effect_settled" and payload.get("call_id"):
                source_id = payload["effect_id"]
                role = Role.USER
                evidence = {
                    k: v
                    for k, v in payload.get("metadata", {}).items()
                    if k in {"command", "cwd", "exit_code", "path", "content_digest", "operation"}
                }
                evidence.update(
                    {"effect_id": source_id, "tool": payload["tool"], "ok": payload["ok"]}
                )
                blocks = [
                    ToolResultBlock(
                        payload["call_id"],
                        payload["content"],
                        is_error=not payload["ok"],
                        evidence=evidence,
                    )
                ]
            if source_id and source_id not in persisted and blocks:
                await self.repository.append_turn(
                    session_id=self.session_id,
                    role=role.value,
                    content=serialize_blocks(blocks),
                    source_id=source_id,
                    run_id=row["run_id"],
                )
                persisted.add(source_id)
        for source_id, chunks in progress.items():
            if source_id not in persisted:
                await self.repository.append_turn(
                    session_id=self.session_id,
                    role=Role.ASSISTANT.value,
                    content=serialize_blocks([TextBlock("".join(chunks))]),
                    source_id=source_id,
                    run_id=self.run_id,
                )
        messages = await self.repository.get_session_messages(self.session_id, after=cutoff or None)
        if projection is not None:
            cutoff, retained = projection
            messages = [*retained, *(m for m in messages if m["created_at"] > cutoff)]
        return rehydrate_messages(messages)
