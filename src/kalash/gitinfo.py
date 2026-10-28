"""Cheap, cached git facts for the status line (branch, dirty count, diff stat)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

CACHE_SECONDS = 5.0
_cache: dict[str, tuple[float, GitStatus | None]] = {}


@dataclass(frozen=True)
class GitStatus:
    branch: str
    dirty: int  # files with uncommitted changes
    detached: bool = False


async def _git(cwd: Path, *args: str, timeout: float = 2.0) -> str | None:
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(process.communicate(), timeout)
    except (OSError, TimeoutError):
        return None
    return out.decode("utf-8", "replace") if process.returncode == 0 else None


async def status(cwd: Path, *, force: bool = False) -> GitStatus | None:
    """Branch and dirty-file count, cached for a few seconds. None outside a git repository."""
    key = str(cwd)
    cached = _cache.get(key)
    if cached and not force and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    branch = await _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    result: GitStatus | None = None
    if branch is not None:
        name = branch.strip()
        detached = name == "HEAD"
        if detached:
            short = await _git(cwd, "rev-parse", "--short", "HEAD")
            name = (short or "HEAD").strip()
        porcelain = await _git(cwd, "status", "--porcelain")
        dirty = len([line for line in (porcelain or "").splitlines() if line.strip()])
        result = GitStatus(name, dirty, detached)
    _cache[key] = (time.monotonic(), result)
    return result


async def diff_summary(cwd: Path, limit: int = 60) -> str | None:
    """``git status --short`` plus ``git diff --stat`` for the working tree."""
    short = await _git(cwd, "status", "--short", timeout=5.0)
    if short is None:
        return None
    stat = await _git(cwd, "diff", "--stat", "HEAD", timeout=5.0) or ""
    lines = short.splitlines()
    text = "\n".join(lines[:limit])
    if len(lines) > limit:
        text += f"\n... {len(lines) - limit} more"
    return (text + ("\n\n" + stat.strip() if stat.strip() else "")).strip() or "(clean)"


async def tracked_files(cwd: Path, limit: int = 20000) -> list[str]:
    """Repository files (tracked + untracked, not ignored) for ``@`` completion."""
    out = await _git(cwd, "ls-files", "--cached", "--others", "--exclude-standard", timeout=5.0)
    if out is None:
        return []
    return out.splitlines()[:limit]


def shorten_path(path: Path, home: Path | None = None) -> str:
    home = home or Path.home()
    try:
        return "~/" + str(path.relative_to(home)) if path != home else "~"
    except ValueError:
        return str(path)
