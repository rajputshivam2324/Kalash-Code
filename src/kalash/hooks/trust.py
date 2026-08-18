"""Hash-pinned project trust for hooks, skills, and plugins.

Trust state machine: UNTRUSTED → PROMPTED → TRUSTED → INVALIDATED.
Content hash computed on first load; invalidated on content change.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Coroutine

from kalash.core.errors import TrustInvalidatedError, KalashError
from kalash.core.ids import generate_id
from kalash.storage.engine import StorageEngine


class TrustState(StrEnum):
    """Trust state machine states."""

    UNTRUSTED = "UNTRUSTED"
    PROMPTED = "PROMPTED"  # User has been asked but hasn't responded
    TRUSTED = "TRUSTED"
    INVALIDATED = "INVALIDATED"  # Content changed since trust was granted


# Valid trust transitions
_TRUST_TRANSITIONS: dict[TrustState, set[TrustState]] = {
    TrustState.UNTRUSTED: {TrustState.PROMPTED, TrustState.TRUSTED},
    TrustState.PROMPTED: {TrustState.TRUSTED, TrustState.UNTRUSTED},
    TrustState.TRUSTED: {TrustState.INVALIDATED},
    TrustState.INVALIDATED: {TrustState.PROMPTED, TrustState.TRUSTED, TrustState.UNTRUSTED},
}


@dataclass
class TrustEntry:
    """A trust record for a piece of content."""

    id: str = ""
    content_path: str = ""
    content_type: str = ""  # "hook" | "skill" | "plugin"
    content_hash: str = ""
    state: TrustState = TrustState.UNTRUSTED
    trusted_at: datetime | None = None
    invalidated_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = generate_id("trust_")


# Type for the user prompt callback
PromptCallback = Callable[[str, str, str], Coroutine[Any, Any, bool]]


class TrustManager:
    """Manages content trust via hash-pinning.

    On first load, computes content hash and prompts the user.
    On subsequent loads, validates hash. If changed, invalidates trust.
    """

    def __init__(self, engine: StorageEngine) -> None:
        self._engine = engine
        self._prompt_callback: PromptCallback | None = None
        self._cache: dict[str, TrustEntry] = {}

    def set_prompt_callback(self, callback: PromptCallback) -> None:
        """Set the callback for prompting user about trust decisions.

        callback(content_path, content_type, description) -> approved
        """
        self._prompt_callback = callback

    async def check_trust(
        self,
        path: str,
        content_type: str,
        *,
        description: str = "",
    ) -> bool:
        """Check if content at path is trusted.

        Returns True if trusted, False if not.
        Handles the full state machine including first-load prompting.
        """
        current_hash = self._compute_hash(path)
        if current_hash is None:
            return False  # File doesn't exist

        # Look up existing trust record
        entry = await self._load_entry(path)

        if entry is None:
            # First load — create entry and prompt
            entry = TrustEntry(
                content_path=path,
                content_type=content_type,
                content_hash=current_hash,
                state=TrustState.UNTRUSTED,
            )
            await self._save_entry(entry)
            return await self._prompt_and_trust(entry, description)

        # Existing entry — check hash
        if entry.content_hash != current_hash:
            # Content changed — invalidate
            entry.state = TrustState.INVALIDATED
            entry.invalidated_at = datetime.now(timezone.utc)
            entry.content_hash = current_hash
            await self._save_entry(entry)
            return await self._prompt_and_trust(entry, description)

        # Hash matches — check state
        if entry.state == TrustState.TRUSTED:
            return True
        elif entry.state == TrustState.PROMPTED:
            # Still waiting for user decision
            return False
        elif entry.state == TrustState.UNTRUSTED:
            return await self._prompt_and_trust(entry, description)
        else:
            # INVALIDATED with matching hash shouldn't happen, but handle gracefully
            return await self._prompt_and_trust(entry, description)

    async def revoke_trust(self, path: str) -> None:
        """Revoke trust for a path."""
        entry = await self._load_entry(path)
        if entry and entry.state == TrustState.TRUSTED:
            entry.state = TrustState.INVALIDATED
            entry.invalidated_at = datetime.now(timezone.utc)
            await self._save_entry(entry)
            self._cache.pop(path, None)

    async def list_trusted(self, content_type: str | None = None) -> list[TrustEntry]:
        """List all trusted entries, optionally filtered by type."""
        sql = "SELECT * FROM trust_entries WHERE state = 'TRUSTED'"
        params: list[Any] = []
        if content_type:
            sql += " AND content_type = ?"
            params.append(content_type)

        rows = await self._engine.execute_read_async(sql, tuple(params))
        return [self._row_to_entry(dict(r)) for r in rows]

    async def verify_all(self) -> list[TrustEntry]:
        """Verify all trusted entries. Returns those that are now invalidated."""
        invalidated: list[TrustEntry] = []
        rows = await self._engine.execute_read_async(
            "SELECT * FROM trust_entries WHERE state = 'TRUSTED'"
        )
        for row in rows:
            entry = self._row_to_entry(dict(row))
            current_hash = self._compute_hash(entry.content_path)
            if current_hash is None or current_hash != entry.content_hash:
                entry.state = TrustState.INVALIDATED
                entry.invalidated_at = datetime.now(timezone.utc)
                if current_hash:
                    entry.content_hash = current_hash
                await self._save_entry(entry)
                invalidated.append(entry)
        return invalidated

    async def _prompt_and_trust(self, entry: TrustEntry, description: str) -> bool:
        """Prompt the user and update trust state."""
        if not self._prompt_callback:
            # No callback — cannot prompt, remain untrusted
            return False

        # Transition to PROMPTED
        entry.state = TrustState.PROMPTED
        await self._save_entry(entry)

        # Ask the user
        approved = await self._prompt_callback(
            entry.content_path,
            entry.content_type,
            description,
        )

        if approved:
            entry.state = TrustState.TRUSTED
            entry.trusted_at = datetime.now(timezone.utc)
        else:
            entry.state = TrustState.UNTRUSTED

        await self._save_entry(entry)
        self._cache[entry.content_path] = entry
        return approved

    def _compute_hash(self, path: str) -> str | None:
        """Compute SHA-256 hash of file content."""
        try:
            content = Path(path).read_bytes()
            return hashlib.sha256(content).hexdigest()
        except (OSError, IOError):
            return None

    async def _load_entry(self, path: str) -> TrustEntry | None:
        """Load a trust entry from cache or DB."""
        if path in self._cache:
            return self._cache[path]

        rows = await self._engine.execute_read_async(
            "SELECT * FROM trust_entries WHERE content_path = ?",
            (path,),
        )
        if not rows:
            return None

        entry = self._row_to_entry(dict(rows[0]))
        self._cache[path] = entry
        return entry

    async def _save_entry(self, entry: TrustEntry) -> None:
        """Persist a trust entry."""
        now = datetime.now(timezone.utc).isoformat()
        await self._engine.execute_write(
            """INSERT OR REPLACE INTO trust_entries
               (id, content_path, content_type, content_hash, state,
                trusted_at, invalidated_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.id,
                entry.content_path,
                entry.content_type,
                entry.content_hash,
                entry.state.value,
                entry.trusted_at.isoformat() if entry.trusted_at else None,
                entry.invalidated_at.isoformat() if entry.invalidated_at else None,
                now,
            ),
        )
        self._cache[entry.content_path] = entry

    def _row_to_entry(self, row: dict[str, Any]) -> TrustEntry:
        """Convert a database row to a TrustEntry."""
        return TrustEntry(
            id=row["id"],
            content_path=row["content_path"],
            content_type=row["content_type"],
            content_hash=row["content_hash"],
            state=TrustState(row["state"]),
            trusted_at=(
                datetime.fromisoformat(row["trusted_at"]) if row.get("trusted_at") else None
            ),
            invalidated_at=(
                datetime.fromisoformat(row["invalidated_at"]) if row.get("invalidated_at") else None
            ),
        )
