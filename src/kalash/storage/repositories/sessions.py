"""Session repository — CRUD for sessions, turns, messages."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kalash.core.ids import generate_id
from kalash.storage.engine import StorageEngine


class SessionRepository:
    """Data access for sessions and related tables."""

    def __init__(self, engine: StorageEngine) -> None:
        self._engine = engine

    async def create_session(
        self,
        project_dir: str,
        agent: str = "default",
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Create a new session. Returns the session ID."""
        session_id = generate_id("ses_")
        now = datetime.now(timezone.utc).isoformat()

        await self._engine.execute_write(
            """INSERT INTO sessions (id, title, project_dir, agent, state, created_at, updated_at, metadata)
               VALUES (?, ?, ?, ?, 'CREATED', ?, ?, ?)""",
            (session_id, title, project_dir, agent, now, now,
             _json_dumps(metadata) if metadata else None),
        )
        return session_id

    async def update_state(self, session_id: str, state: str) -> None:
        """Update session state."""
        now = datetime.now(timezone.utc).isoformat()
        await self._engine.execute_write(
            "UPDATE sessions SET state = ?, updated_at = ? WHERE id = ?",
            (state, now, session_id),
        )

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Get a session by ID."""
        rows = await self._engine.execute_read_async(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        return dict(rows[0]) if rows else None

    async def list_sessions(self, limit: int = 50, include_archived: bool = False) -> list[dict[str, Any]]:
        """List recent sessions."""
        sql = "SELECT * FROM sessions"
        if not include_archived:
            sql += " WHERE archived_at IS NULL"
        sql += " ORDER BY updated_at DESC LIMIT ?"
        rows = await self._engine.execute_read_async(sql, (limit,))
        return [dict(r) for r in rows]

    async def create_turn(
        self,
        session_id: str,
        seq: int,
        role: str,
        model: str | None = None,
    ) -> str:
        """Create a new turn. Returns the turn ID."""
        turn_id = generate_id("trn_")
        now = datetime.now(timezone.utc).isoformat()

        await self._engine.execute_write(
            """INSERT INTO turns (id, session_id, seq, role, state, started_at, model)
               VALUES (?, ?, ?, ?, 'PENDING', ?, ?)""",
            (turn_id, session_id, seq, role, now, model),
        )
        return turn_id

    async def complete_turn(
        self,
        turn_id: str,
        state: str = "COMPLETED",
        token_count: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        """Mark a turn as completed."""
        now = datetime.now(timezone.utc).isoformat()
        await self._engine.execute_write(
            """UPDATE turns SET state = ?, finished_at = ?, token_count = ?, cost_usd = ?
               WHERE id = ?""",
            (state, now, token_count, cost_usd, turn_id),
        )

    async def add_message(
        self,
        turn_id: str,
        seq: int,
        role: str,
        content: str | None = None,
        content_type: str = "text",
        blob_ref: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Add a message to a turn. Append-only (I-021)."""
        msg_id = generate_id("msg_")
        now = datetime.now(timezone.utc).isoformat()

        await self._engine.execute_write(
            """INSERT INTO messages (id, turn_id, seq, role, content_type, content, blob_ref, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, turn_id, seq, role, content_type, content, blob_ref,
             _json_dumps(metadata) if metadata else None, now),
        )
        return msg_id

    async def get_messages(self, turn_id: str) -> list[dict[str, Any]]:
        """Get all messages for a turn, ordered by seq."""
        rows = await self._engine.execute_read_async(
            "SELECT * FROM messages WHERE turn_id = ? ORDER BY seq", (turn_id,)
        )
        return [dict(r) for r in rows]

    async def get_session_messages(self, session_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Get all messages for a session, ordered by turn seq then message seq."""
        sql = """
            SELECT m.*, t.seq as turn_seq, t.role as turn_role
            FROM messages m
            JOIN turns t ON m.turn_id = t.id
            WHERE t.session_id = ?
            ORDER BY t.seq, m.seq
        """
        params: tuple[Any, ...] = (session_id,)
        if limit:
            sql += " LIMIT ?"
            params = (session_id, limit)
        rows = await self._engine.execute_read_async(sql, params)
        return [dict(r) for r in rows]

    async def record_tool_call(
        self,
        message_id: str,
        turn_id: str,
        tool_name: str,
        arguments: str | None = None,
    ) -> str:
        """Record a tool call."""
        call_id = generate_id("tc_")
        now = datetime.now(timezone.utc).isoformat()

        await self._engine.execute_write(
            """INSERT INTO tool_calls (id, message_id, turn_id, tool_name, arguments, state, started_at)
               VALUES (?, ?, ?, ?, ?, 'RECEIVED', ?)""",
            (call_id, message_id, turn_id, tool_name, arguments, now),
        )
        return call_id

    async def complete_tool_call(
        self,
        call_id: str,
        result: str | None = None,
        state: str = "RETURNED",
        error_code: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Complete a tool call with results."""
        now = datetime.now(timezone.utc).isoformat()
        await self._engine.execute_write(
            """UPDATE tool_calls SET result = ?, state = ?, status = ?,
               error_code = ?, duration_ms = ?, finished_at = ? WHERE id = ?""",
            (result, state, "completed" if state == "RETURNED" else "failed",
             error_code, duration_ms, now, call_id),
        )


def _json_dumps(data: Any) -> str | None:
    """Serialize to JSON, or None."""
    if data is None:
        return None
    import json
    return json.dumps(data)
