"""Core Key-Value store engine with JSON persistence and TTL expiration."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class Entry:
    key: str
    value: Any
    created_at: float
    expires_at: float | None = None

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at


class KVStore:
    """Embedded persistent key-value store."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else Path.cwd() / ".kvstore.json"
        self._entries: dict[str, Entry] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                for k, v in data.get("entries", {}).items():
                    entry = Entry(
                        key=k,
                        value=v.get("value"),
                        created_at=v.get("created_at", time.time()),
                        expires_at=v.get("expires_at"),
                    )
                    if not entry.is_expired:
                        self._entries[k] = entry
            except (json.JSONDecodeError, OSError):
                self._entries = {}

    def _save(self) -> None:
        self.purge_expired()
        data = {
            "entries": {
                k: {
                    "value": e.value,
                    "created_at": e.created_at,
                    "expires_at": e.expires_at,
                }
                for k, e in self._entries.items()
            }
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def set(self, key: str, value: Any, ttl_seconds: float | None = None) -> None:
        now = time.time()
        expires_at = (now + ttl_seconds) if ttl_seconds else None
        self._entries[key] = Entry(
            key=key, value=value, created_at=now, expires_at=expires_at
        )
        self._save()

    def get(self, key: str, default: Any = None) -> Any:
        entry = self._entries.get(key)
        if entry is None:
            return default
        if entry.is_expired:
            del self._entries[key]
            self._save()
            return default
        return entry.value

    def delete(self, key: str) -> bool:
        if key in self._entries:
            del self._entries[key]
            self._save()
            return True
        return False

    def list_keys(self) -> list[str]:
        self.purge_expired()
        return sorted(self._entries.keys())

    def clear(self) -> None:
        self._entries.clear()
        self._save()

    def purge_expired(self) -> int:
        now = time.time()
        expired = [k for k, e in self._entries.items() if e.expires_at and now > e.expires_at]
        for k in expired:
            del self._entries[k]
        return len(expired)

    def stats(self) -> dict[str, Any]:
        self.purge_expired()
        return {
            "total_keys": len(self._entries),
            "file_path": str(self.path),
            "file_size_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }
