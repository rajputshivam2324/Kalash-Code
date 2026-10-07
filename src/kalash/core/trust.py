"""Explicit, hash-pinned authorization for executable project configuration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from kalash.core.paths import kalash_home
from kalash.core.secure_io import atomic_private_write, private_lock


def _store_path() -> Path:
    return kalash_home() / "trusted-files.json"


def _load() -> dict[str, str]:
    try:
        with _store_path().open("rb") as stream:
            raw = stream.read(2_000_001)
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise ValueError("Cannot read user authorization registry") from error
    if len(raw) > 2_000_000:
        raise ValueError("User authorization registry exceeds 2 MB")
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise ValueError("User authorization registry is not valid JSON") from error
    if not isinstance(data, dict) or any(
        not isinstance(path, str)
        or not Path(path).is_absolute()
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
        for path, digest in data.items()
    ):
        raise ValueError("User authorization registry has an invalid schema")
    return data


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
    target = _store_path()
    with private_lock(target.with_suffix(".lock")):
        records = _load()
        records[str(path)] = digest
        atomic_private_write(target, (json.dumps(records, indent=2) + "\n").encode())
    return digest


def is_trusted(path: Path) -> bool:
    path = path.resolve()
    try:
        return _load().get(str(path)) == _digest(path)
    except (OSError, ValueError):
        return False


def has_trust_record(path: Path) -> bool:
    """Distinguish changed authorized configuration from never-authorized files."""
    return str(path.resolve()) in _load()


def trusted_paths() -> tuple[Path, ...]:
    """Retain authorized paths even when their executable files are removed."""
    return tuple(Path(path) for path in _load())


def untrust_file(path: Path) -> bool:
    """Explicit operator revocation; malformed records are never reset."""
    target = _store_path()
    with private_lock(target.with_suffix(".lock")):
        records = _load()
        removed = records.pop(str(path.resolve()), None) is not None
        if removed:
            atomic_private_write(target, (json.dumps(records, indent=2) + "\n").encode())
    return removed
