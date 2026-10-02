"""Persisted permission decisions (grants).

CRUD operations against the permission_grants table.
Grants support pattern matching (path globs, host wildcards)
and have lifetimes: once, session, always.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from kalash.core.ids import generate_id
from kalash.storage.engine import StorageEngine

logger = logging.getLogger(__name__)

_GRANTS_TABLE = "kalash_grants"


# ---------------------------------------------------------------------------
# Grant lifetime
# ---------------------------------------------------------------------------


class GrantLifetime(StrEnum):
    """How long a permission grant persists."""

    ONCE = "once"  # Consumed on first use
    SESSION = "session"  # Valid for the current session only
    ALWAYS = "always"  # Persisted permanently until revoked


# ---------------------------------------------------------------------------
# Grant model
# ---------------------------------------------------------------------------


@dataclass
class Grant:
    """A persisted permission grant."""

    id: str
    tool_pattern: str  # Glob pattern matching tool names
    path_pattern: str | None = None  # Glob pattern for file paths
    host_pattern: str | None = None  # Wildcard pattern for hosts
    lifetime: GrantLifetime = GrantLifetime.SESSION
    session_id: str | None = None  # For session-scoped grants
    decision: str = "allow"  # allow or deny
    reason: str = ""
    created_at: str = ""
    consumed: bool = False  # True if a 'once' grant has been used
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Grant store
# ---------------------------------------------------------------------------


@dataclass
class GrantStore:
    """CRUD operations for the permission_grants table.

    Manages persisted permission decisions with pattern matching
    and lifetime enforcement.
    """

    engine: StorageEngine
    session_id: str = ""

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(
        self,
        *,
        tool_pattern: str,
        path_pattern: str | None = None,
        host_pattern: str | None = None,
        lifetime: GrantLifetime = GrantLifetime.SESSION,
        decision: str = "allow",
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Grant:
        """Create a new permission grant.

        Args:
            tool_pattern: Glob pattern matching tool names.
            path_pattern: Optional glob pattern for paths.
            host_pattern: Optional wildcard for hosts.
            lifetime: once, session, or always.
            decision: allow or deny.
            reason: Human-readable reason for the grant.
            metadata: Additional metadata.

        Returns:
            The created Grant.
        """
        grant_id = generate_id("grt_")
        now = datetime.now(UTC).isoformat()

        grant = Grant(
            id=grant_id,
            tool_pattern=tool_pattern,
            path_pattern=path_pattern,
            host_pattern=host_pattern,
            lifetime=lifetime,
            session_id=self.session_id if lifetime == GrantLifetime.SESSION else None,
            decision=decision,
            reason=reason,
            created_at=now,
            metadata=metadata or {},
        )

        await self.engine.execute_write(
            f"""INSERT INTO {_GRANTS_TABLE}
               (id, tool_pattern, path_pattern, host_pattern, lifetime,
                session_id, decision, reason, created_at, consumed, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                grant.id,
                grant.tool_pattern,
                grant.path_pattern,
                grant.host_pattern,
                grant.lifetime,
                grant.session_id,
                grant.decision,
                grant.reason,
                grant.created_at,
                False,
                _json_dumps(grant.metadata),
            ),
        )

        logger.info("Grant created: %s (%s, %s)", grant.id, tool_pattern, lifetime)
        return grant

    # ------------------------------------------------------------------
    # Read / Find
    # ------------------------------------------------------------------

    async def find_matching(
        self,
        *,
        tool_name: str,
        paths: list[str] | None = None,
        hosts: list[str] | None = None,
        grant_signature: str | None = None,
    ) -> dict[str, Any] | None:
        """Find a matching grant for the given action.

        Checks tool pattern, path pattern, and host pattern.
        Returns the first matching grant or None.
        """
        grants = await self._get_active_grants()

        for grant in grants:
            # Match tool pattern
            if not fnmatch.fnmatch(tool_name, grant["tool_pattern"]):
                continue

            # Match path pattern if specified
            if grant["path_pattern"] and paths:
                if not any(fnmatch.fnmatch(p, grant["path_pattern"]) for p in paths):
                    continue
            elif grant["path_pattern"] and not paths:
                continue

            # Match host pattern if specified
            if grant["host_pattern"] and hosts:
                if not any(fnmatch.fnmatch(h, grant["host_pattern"]) for h in hosts):
                    continue
            elif grant["host_pattern"] and not hosts:
                continue

            meta = _json_loads(grant.get("metadata"))
            stored_sig = meta.get("signature")
            if stored_sig and grant_signature and stored_sig != grant_signature:
                continue

            return grant

        return None

    async def get(self, grant_id: str) -> dict[str, Any] | None:
        """Get a grant by ID."""
        rows = await self.engine.execute_read_async(
            f"SELECT * FROM {_GRANTS_TABLE} WHERE id = ?", (grant_id,)
        )
        return dict(rows[0]) if rows else None

    async def list_grants(
        self,
        *,
        session_only: bool = False,
        include_consumed: bool = False,
    ) -> list[dict[str, Any]]:
        """List grants with optional filtering.

        Args:
            session_only: Only return session-scoped grants.
            include_consumed: Include consumed 'once' grants.
        """
        conditions: list[str] = []
        params: list[Any] = []

        if session_only:
            conditions.append("session_id = ?")
            params.append(self.session_id)

        if not include_consumed:
            conditions.append("consumed = 0")

        sql = f"SELECT * FROM {_GRANTS_TABLE}"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY created_at DESC"

        rows = await self.engine.execute_read_async(sql, tuple(params))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Consume (for 'once' grants)
    # ------------------------------------------------------------------

    async def consume(self, grant_id: str) -> None:
        """Mark a 'once' grant as consumed (used up)."""
        await self.engine.execute_write(
            f"UPDATE {_GRANTS_TABLE} SET consumed = 1 WHERE id = ? AND lifetime = 'once'",
            (grant_id,),
        )
        logger.debug("Grant consumed: %s", grant_id)

    # ------------------------------------------------------------------
    # Revoke
    # ------------------------------------------------------------------

    async def revoke(self, grant_id: str) -> bool:
        """Revoke (delete) a grant. Returns True if it existed."""
        rows = await self.engine.execute_read_async(
            f"SELECT id FROM {_GRANTS_TABLE} WHERE id = ?", (grant_id,)
        )
        if not rows:
            return False

        await self.engine.execute_write(f"DELETE FROM {_GRANTS_TABLE} WHERE id = ?", (grant_id,))
        logger.info("Grant revoked: %s", grant_id)
        return True

    async def revoke_session(self, session_id: str | None = None) -> int:
        """Revoke all session-scoped grants for a session.

        Returns the number of grants revoked.
        """
        sid = session_id or self.session_id
        rows = await self.engine.execute_read_async(
            f"SELECT COUNT(*) as cnt FROM {_GRANTS_TABLE} WHERE session_id = ?",
            (sid,),
        )
        count = rows[0]["cnt"] if rows else 0

        await self.engine.execute_write(f"DELETE FROM {_GRANTS_TABLE} WHERE session_id = ?", (sid,))
        logger.info("Revoked %d session grants for %s", count, sid)
        return count

    async def invalidate_expired(self) -> int:
        """Remove consumed 'once' grants and expired entries.

        Returns number of grants removed.
        """
        rows = await self.engine.execute_read_async(
            f"SELECT COUNT(*) as cnt FROM {_GRANTS_TABLE} WHERE consumed = 1",
            (),
        )
        count = rows[0]["cnt"] if rows else 0

        await self.engine.execute_write(f"DELETE FROM {_GRANTS_TABLE} WHERE consumed = 1", ())
        return count

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _get_active_grants(self) -> list[dict[str, Any]]:
        """Get all active (non-consumed) grants for the current context."""
        rows = await self.engine.execute_read_async(
            f"""SELECT * FROM {_GRANTS_TABLE}
               WHERE consumed = 0
               AND (lifetime = 'always'
                    OR (lifetime = 'session' AND session_id = ?)
                    OR (lifetime = 'once'))
               ORDER BY created_at DESC""",
            (self.session_id,),
        )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_dumps(data: Any) -> str | None:
    if data is None:
        return None
    import json

    return json.dumps(data)


def _json_loads(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    import json

    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
