"""Server-side file observation: what the agent has seen of each file.

The model never has to echo a hash. ``read`` records the file's content fingerprint;
``edit``/``write`` of an existing file require a prior observation *and* that the file
on disk still matches it (otherwise another process changed it and the model must read
again). The agent's own successful writes update the observation, so consecutive edits
need no re-read. One tracker belongs to one session.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Fingerprint:
    size: int
    mtime_ns: int
    sha256: str


class StaleFile(Exception):
    """The file is unobserved, missing, or changed since it was last observed."""


def fingerprint_bytes(data: bytes, mtime_ns: int) -> Fingerprint:
    return Fingerprint(len(data), mtime_ns, hashlib.sha256(data).hexdigest())


class ObservationTracker:
    def __init__(self) -> None:
        self._seen: dict[str, Fingerprint | None] = {}  # None = observed absent

    def record(self, path: Path, data: bytes) -> None:
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            mtime = 0
        self._seen[str(path)] = fingerprint_bytes(data, mtime)

    def record_absent(self, path: Path) -> None:
        self._seen[str(path)] = None

    def forget(self, path: Path) -> None:
        self._seen.pop(str(path), None)

    def was_observed(self, path: Path) -> bool:
        return str(path) in self._seen

    def check_unchanged(self, path: Path, current: bytes) -> None:
        """Raise :class:`StaleFile` unless ``current`` is what the agent last saw."""
        key = str(path)
        if key not in self._seen:
            raise StaleFile(f"{path} has not been read in this session. Read it first, then retry.")
        seen = self._seen[key]
        if seen is None:
            raise StaleFile(f"{path} was observed as missing but now exists. Read it, then retry.")
        if hashlib.sha256(current).hexdigest() != seen.sha256:
            raise StaleFile(
                f"{path} changed since you last read it (modified outside this session or by "
                f"a command). Read it again, then retry."
            )


def read_bytes_nofollow_check(path: Path) -> bytes:
    """Read a regular file's bytes; refuse directories and special files with clear errors."""
    if path.is_dir():
        raise IsADirectoryError(f"{path} is a directory")
    if not path.is_file():
        raise FileNotFoundError(f"{path} does not exist or is not a regular file")
    if os.name == "posix" and path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError(f"{path} is larger than 50 MiB")
    return path.read_bytes()
