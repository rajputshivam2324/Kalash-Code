"""Bounding tool output: keep head and tail inline, save the full text to a spill file."""

from __future__ import annotations

import secrets
from pathlib import Path

INLINE_BYTES = 30_000
HEAD_BYTES = 8_000
TAIL_BYTES = 18_000
MAX_SPILL_BYTES = 64 * 1024 * 1024


def limit_output(text: str, spill_dir: Path, label: str = "output") -> str:
    """Return ``text`` unchanged when small; otherwise head + tail with the spill path.

    The tail is kept because build/test failures and summaries are at the end.
    """
    raw = text.encode("utf-8", "replace")
    if len(raw) <= INLINE_BYTES:
        return text
    path = _spill(raw, spill_dir, label)
    head = raw[:HEAD_BYTES].decode("utf-8", "ignore")
    tail = raw[-TAIL_BYTES:].decode("utf-8", "ignore")
    omitted = len(raw) - len(head.encode()) - len(tail.encode())
    note = (
        f"\n\n[... {omitted} bytes omitted. Full {label} ({len(raw)} bytes) saved to {path}"
        f" - use read with offset/limit or grep on that file ...]\n\n"
    )
    return head + note + tail


def _spill(raw: bytes, spill_dir: Path, label: str) -> Path:
    spill_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = spill_dir / f"{label}-{secrets.token_hex(4)}.txt"
    path.write_bytes(raw[:MAX_SPILL_BYTES])
    path.chmod(0o600)
    return path


def truncate_line(line: str, limit: int = 2000) -> str:
    return line if len(line) <= limit else line[:limit] + f"... [{len(line) - limit} more chars]"
