"""Shared state blackboard for multi-agent coordination.

Put/get/delete operations keyed by (run_id, key), backed by
the blackboard table in SQLite with JSON-serialized typed values.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, TypeVar, overload

from kalash.core.errors import KalashError
from kalash.storage.engine import StorageEngine

T = TypeVar("T")


class BlackboardKeyError(KalashError):
    """Key not found on the blackboard."""

    code = "KALASH_BLACKBOARD_KEY_NOT_FOUND"


class Blackboard:
    """Shared state store for coordinating agents within a run.

    All values are JSON-serialized. Keys are scoped to a run_id
    so different runs cannot interfere.
    """

    def __init__(self, engine: StorageEngine) -> None:
        self._engine = engine

    async def put(
        self,
        run_id: str,
        key: str,
        value: Any,
        *,
        type_hint: str = "json",
        ttl_seconds: int | None = None,
    ) -> None:
        """Write a value to the blackboard.

        Args:
            run_id: Scope for the key.
            key: Unique key within the run.
            value: JSON-serializable value.
            type_hint: Type annotation for consumers (e.g. "str", "list", "dict").
            ttl_seconds: Optional TTL; entries older than this are treated as expired.
        """
        now = datetime.now(timezone.utc).isoformat()
        serialized = json.dumps(value, default=str)

        expires_at: str | None = None
        if ttl_seconds is not None:
            from datetime import timedelta

            expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
            ).isoformat()

        # Upsert: INSERT OR REPLACE
        await self._engine.execute_write(
            """INSERT OR REPLACE INTO blackboard
               (run_id, key, value, type_hint, expires_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, key, serialized, type_hint, expires_at, now),
        )

    async def get(self, run_id: str, key: str, *, default: Any = None) -> Any:
        """Read a value from the blackboard.

        Returns the deserialized value, or default if not found/expired.
        """
        rows = await self._engine.execute_read_async(
            "SELECT value, expires_at FROM blackboard WHERE run_id = ? AND key = ?",
            (run_id, key),
        )
        if not rows:
            return default

        row = rows[0]
        # Check TTL expiry
        if row["expires_at"]:
            expires = datetime.fromisoformat(row["expires_at"])
            if datetime.now(timezone.utc) > expires:
                # Expired — clean up and return default
                await self.delete(run_id, key)
                return default

        return json.loads(row["value"])

    async def get_typed(self, run_id: str, key: str, expected_type: type[T]) -> T:
        """Read a value with type validation.

        Raises BlackboardKeyError if not found.
        Raises TypeError if the value doesn't match expected_type.
        """
        value = await self.get(run_id, key)
        if value is None:
            raise BlackboardKeyError(
                f"Key '{key}' not found for run '{run_id}'",
                recoverable=True,
            )
        if not isinstance(value, expected_type):
            raise TypeError(
                f"Blackboard key '{key}': expected {expected_type.__name__}, "
                f"got {type(value).__name__}"
            )
        return value

    async def delete(self, run_id: str, key: str) -> bool:
        """Delete a key from the blackboard. Returns True if it existed."""
        rows = await self._engine.execute_read_async(
            "SELECT 1 FROM blackboard WHERE run_id = ? AND key = ?",
            (run_id, key),
        )
        if not rows:
            return False

        await self._engine.execute_write(
            "DELETE FROM blackboard WHERE run_id = ? AND key = ?",
            (run_id, key),
        )
        return True

    async def list_keys(self, run_id: str) -> list[str]:
        """List all non-expired keys for a run."""
        now = datetime.now(timezone.utc).isoformat()
        rows = await self._engine.execute_read_async(
            """SELECT key FROM blackboard
               WHERE run_id = ? AND (expires_at IS NULL OR expires_at > ?)""",
            (run_id, now),
        )
        return [row["key"] for row in rows]

    async def clear(self, run_id: str) -> int:
        """Delete all keys for a run. Returns count deleted."""
        rows = await self._engine.execute_read_async(
            "SELECT COUNT(*) as cnt FROM blackboard WHERE run_id = ?",
            (run_id,),
        )
        count = rows[0]["cnt"] if rows else 0

        await self._engine.execute_write(
            "DELETE FROM blackboard WHERE run_id = ?",
            (run_id,),
        )
        return count

    async def cleanup_expired(self) -> int:
        """Remove all expired entries across all runs. Returns count deleted."""
        now = datetime.now(timezone.utc).isoformat()
        rows = await self._engine.execute_read_async(
            "SELECT COUNT(*) as cnt FROM blackboard WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        )
        count = rows[0]["cnt"] if rows else 0

        await self._engine.execute_write(
            "DELETE FROM blackboard WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        )
        return count
