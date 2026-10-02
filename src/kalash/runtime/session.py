"""Session lifecycle management.

Creates, resumes, and manages agent sessions backed by SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kalash.core.config import KalashConfig, load_config
from kalash.core.ids import generate_id
from kalash.storage.engine import StorageEngine, get_engine
from kalash.storage.repositories.sessions import SessionRepository


@dataclass
class Session:
    """An active agent session."""

    id: str
    project_dir: str
    agent: str = "default"
    model: str = "anthropic/claude-sonnet-4-5"
    sandbox_mode: str = "workspace-write"
    state: str = "CREATED"
    turn_seq: int = 0
    config: KalashConfig | None = None


class SessionManager:
    """Manages session lifecycle: create, resume, list, close."""

    def __init__(self, config: KalashConfig | None = None) -> None:
        self._config = config or load_config()
        self._engine: StorageEngine | None = None
        self._repo: SessionRepository | None = None

    def _ensure_engine(self) -> StorageEngine:
        """Get or initialize the storage engine."""
        if self._engine is None:
            self._engine = get_engine()
            self._run_migrations_sync(self._engine)
        return self._engine

    @staticmethod
    def _run_migrations_sync(engine: StorageEngine) -> None:
        """Apply pending schema migrations synchronously."""
        import hashlib
        from datetime import datetime

        from kalash.storage.migrations import MIGRATIONS

        conn = engine._get_connection()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                checksum TEXT NOT NULL
            )
            """
        )
        row = conn.execute("SELECT MAX(version) as v FROM schema_migrations").fetchone()
        current_version = int(row["v"]) if row and row["v"] is not None else 0
        now = datetime.now(UTC).isoformat()
        for version, name, sql in MIGRATIONS:
            if version <= current_version:
                continue
            conn.executescript(sql)
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at, checksum) VALUES (?, ?, ?, ?)",
                (version, name, now, checksum),
            )

    def _get_repo(self) -> SessionRepository:
        """Get the session repository."""
        if self._repo is None:
            engine = self._ensure_engine()
            self._repo = SessionRepository(engine)
        return self._repo

    def create(self, project_dir: str | None = None) -> Session:
        """Create a new session."""
        proj = project_dir or str(self._config.project_dir)
        session_id = generate_id("ses_")

        engine = self._ensure_engine()
        now = datetime.now(UTC).isoformat()

        # Use the engine's synchronous write path (A-1/R-3).
        engine.execute_write_sync(
            """INSERT INTO sessions (id, project_dir, agent, state, created_at, updated_at)
               VALUES (?, ?, ?, 'ACTIVE', ?, ?)""",
            (session_id, proj, self._config.model.primary.split("/")[0], now, now),
        )

        return Session(
            id=session_id,
            project_dir=proj,
            agent="default",
            model=self._config.model.primary,
            sandbox_mode=self._config.permissions.sandbox,
            state="ACTIVE",
            config=self._config,
        )

    def resume(self, session_id: str) -> Session:
        """Resume an existing session."""
        engine = self._ensure_engine()
        rows = engine.execute_read("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if not rows:
            raise ValueError(f"Session not found: {session_id}")

        row = dict(rows[0])
        return Session(
            id=row["id"],
            project_dir=row["project_dir"],
            agent=row.get("agent", "default"),
            model=self._config.model.primary,
            sandbox_mode=self._config.permissions.sandbox,
            state=row.get("state", "ACTIVE"),
            config=self._config,
        )

    def list_sessions(self, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
        """List recent sessions, newest first.

        Each row carries a ``turn_count`` so callers can show session size
        without a second query per row.
        """
        engine = self._ensure_engine()
        sql = """
            SELECT s.id, s.project_dir, s.agent, s.state, s.created_at, s.updated_at,
                   (SELECT COUNT(*) FROM turns t WHERE t.session_id = s.id) AS turn_count
            FROM sessions s
        """
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE UPPER(s.state) = UPPER(?)"
            params = (status,)
        sql += " ORDER BY s.updated_at DESC LIMIT ?"
        params = (*params, limit)

        return [dict(r) for r in engine.execute_read(sql, params)]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Fetch one session row with its turn count, or None."""
        engine = self._ensure_engine()
        rows = engine.execute_read(
            """
            SELECT s.*,
                   (SELECT COUNT(*) FROM turns t WHERE t.session_id = s.id) AS turn_count,
                   (SELECT COALESCE(SUM(t.token_count), 0) FROM turns t
                     WHERE t.session_id = s.id) AS total_tokens
            FROM sessions s WHERE s.id = ?
            """,
            (session_id,),
        )
        return dict(rows[0]) if rows else None

    def get_transcript(self, session_id: str) -> list[dict[str, Any]]:
        """Ordered messages for a session: the read path a replay needs."""
        engine = self._ensure_engine()
        rows = engine.execute_read(
            """
            SELECT m.role, m.content, m.content_type, m.created_at,
                   t.seq AS turn_seq, m.seq AS msg_seq
            FROM messages m JOIN turns t ON m.turn_id = t.id
            WHERE t.session_id = ?
            ORDER BY t.seq, m.seq
            """,
            (session_id,),
        )
        return [dict(r) for r in rows]

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and everything under it. Returns False if unknown."""
        engine = self._ensure_engine()
        if self.get_session(session_id) is None:
            return False

        # Ordered child-first because the schema declares foreign keys and
        # SQLite enforces them when foreign_keys=ON.  All three DELETEs go
        # through the engine's serialized write path (A-1).
        engine.execute_write_sync(
            """DELETE FROM messages WHERE turn_id IN
               (SELECT id FROM turns WHERE session_id = ?)""",
            (session_id,),
        )
        engine.execute_write_sync(
            "DELETE FROM turns WHERE session_id = ?",
            (session_id,),
        )
        engine.execute_write_sync(
            "DELETE FROM sessions WHERE id = ?",
            (session_id,),
        )
        return True

    def export_session(self, session_id: str, format: str = "json") -> str | None:
        """Serialize a session as JSON or Markdown, or None if unknown."""
        session = self.get_session(session_id)
        if session is None:
            return None

        transcript = self.get_transcript(session_id)

        if format.lower() == "markdown":
            from kalash.models.normalize import TextBlock
            from kalash.runtime.serialize import deserialize_blocks

            lines = [
                f"# Session {session_id}",
                "",
                f"- project: {session.get('project_dir', '')}",
                f"- created: {session.get('created_at', '')}",
                f"- turns: {session.get('turn_count', 0)}",
                "",
            ]
            for row in transcript:
                blocks = deserialize_blocks(row.get("content"))
                text = "\n".join(b.text for b in blocks if isinstance(b, TextBlock))
                if not text.strip():
                    continue
                lines.append(f"## {row.get('role', '?')}")
                lines.append("")
                lines.append(text.strip())
                lines.append("")
            return "\n".join(lines)

        import json

        return json.dumps({"session": session, "messages": transcript}, indent=2, default=str)

    def close_session(self, session_id: str) -> None:
        """Close a session."""
        engine = self._ensure_engine()
        now = datetime.now(UTC).isoformat()
        engine.execute_write_sync(
            "UPDATE sessions SET state = 'CLOSED', closed_at = ?, updated_at = ? WHERE id = ?",
            (now, now, session_id),
        )
