"""Explicit, hash-pinned authorization for executable project configuration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from kalash.core.paths import kalash_home


def _store_path() -> Path:
    return kalash_home() / "trusted-files.json"


def _load() -> dict[str, str]:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        content = stream.read(2_000_001)
    if len(content) > 2_000_000:
        raise ValueError("Executable configuration exceeds 2 MB")
    return hashlib.sha256(content).hexdigest()


def trust_file(path: Path) -> str:
    """Called only by an explicit CLI authorization command."""
    path = path.resolve()
    digest = _digest(path)
    records = _load()
    records[str(path)] = digest
    target = _store_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(target)
    return digest


def is_trusted(path: Path) -> bool:
    path = path.resolve()
    try:
        return _load().get(str(path)) == _digest(path)
    except (OSError, ValueError):
        return False
