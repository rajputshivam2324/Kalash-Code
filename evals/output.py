"""Persistent defaults for evaluation evidence, independent of the caller's cwd."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def default_output(kind: str) -> Path:
    """Allocate a unique name without creating or overwriting an existing run."""
    if not kind or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in kind):
        raise ValueError("Evaluation kind must contain lowercase letters, digits or hyphens")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    root = Path(__file__).resolve().parents[1] / ".kalash" / "eval_runs"
    return root / f"{kind}-{stamp}-{uuid4().hex[:12]}"
