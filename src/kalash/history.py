"""Prompt history shared by every session (``~/.kalash/history.jsonl``)."""

from __future__ import annotations

import json
from pathlib import Path

from kalash.paths import kalash_home
from kalash.secure_io import atomic_private_write

MAX_ENTRIES = 500
MAX_LENGTH = 20_000


def history_path() -> Path:
    return kalash_home() / "history.jsonl"


def load() -> list[str]:
    """Oldest first. A damaged file yields what could be read, never an exception."""
    entries: list[str] = []
    try:
        for line in history_path().read_text("utf-8").splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, str) and value.strip():
                entries.append(value)
    except OSError:
        return []
    return entries[-MAX_ENTRIES:]


def add(entries: list[str], text: str) -> list[str]:
    """Return ``entries`` plus ``text`` (deduplicated against the previous entry) and persist."""
    text = text.strip()
    if not text or len(text) > MAX_LENGTH:
        return entries
    updated = entries if entries and entries[-1] == text else [*entries, text]
    updated = updated[-MAX_ENTRIES:]
    try:
        payload = "\n".join(json.dumps(item) for item in updated) + "\n"
        atomic_private_write(history_path(), payload.encode("utf-8"))
    except OSError:
        pass  # history is a convenience; never let it break sending a prompt
    return updated
