"""Filesystem locations: the Kalash home, project roots and per-project session folders."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path


def kalash_home() -> Path:
    """User state directory (``$KALASH_HOME`` or ``~/.kalash``), resolved once per call."""
    raw = os.environ.get("KALASH_HOME")
    home = Path(raw).expanduser() if raw else Path.home() / ".kalash"
    return home.resolve()


def project_root(cwd: Path) -> Path:
    """The nearest ancestor of ``cwd`` containing ``.git``; ``cwd`` itself otherwise."""
    cwd = cwd.resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return candidate
    return cwd


def project_slug(root: Path) -> str:
    """Stable, filesystem-safe folder name identifying one project root."""
    text = str(root.resolve())
    digest = hashlib.sha256(text.encode()).hexdigest()[:10]
    tail = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[-48:] or "root"
    return f"{tail}-{digest}"


def sessions_dir(root: Path) -> Path:
    return kalash_home() / "sessions" / project_slug(root)


def spill_dir() -> Path:
    return kalash_home() / "spill"


def log_file() -> Path:
    return kalash_home() / "logs" / "kalash.log"
