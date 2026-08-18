"""Session lifecycle management.

Creates, resumes, and manages agent sessions backed by SQLite.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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

    def rewind(self, steps: int = 1) -> None:
        """Rewind the session by N turns (placeholder)."""
        self.turn_seq = max(0, self.turn_seq - steps)


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
            # Run migrations synchronously
            conn = self._engine._get_connection()
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
            )
            if not cursor.fetchone():
                # Run migration SQL directly (synchronous, no async needed)
                from kalash.storage.migrations import MIGRATIONS
                for _version, _name, sql in MIGRATIONS:
                    conn.executescript(sql)
                import hashlib
                from datetime import datetime, timezone as tz
                now = datetime.now(tz.utc).isoformat()
                for version, name, sql in MIGRATIONS:
                    checksum = hashlib.sha256(sql.encode()).hexdigest()
                    conn.execute(
                        "INSERT OR IGNORE INTO schema_migrations (version, name, applied_at, checksum) VALUES (?, ?, ?, ?)",
                        (version, name, now, checksum),
                    )
        return self._engine

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

        # Persist to DB
        engine = self._ensure_engine()
        now = datetime.now(timezone.utc).isoformat()
        with engine.read() as conn:
            pass  # just ensure connection exists

        # Use synchronous write for session creation
        conn = engine._get_connection()
        conn.execute(
            """INSERT INTO sessions (id, project_dir, agent, state, created_at, updated_at)
               VALUES (?, ?, ?, 'ACTIVE', ?, ?)""",
            (session_id, proj, self._config.model.primary.split("/")[0],
             now, now),
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
        rows = engine.execute_read(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
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

    def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        """List recent sessions."""
        engine = self._ensure_engine()
        rows = engine.execute_read(
            "SELECT id, project_dir, state, created_at, updated_at FROM sessions ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def close_session(self, session_id: str) -> None:
        """Close a session."""
        engine = self._ensure_engine()
        now = datetime.now(timezone.utc).isoformat()
        conn = engine._get_connection()
        conn.execute(
            "UPDATE sessions SET state = 'CLOSED', closed_at = ?, updated_at = ? WHERE id = ?",
            (now, now, session_id),
        )
