"""Canonical paths, permissions, digests and file metadata."""

from __future__ import annotations

import fnmatch
import hashlib
import unicodedata
from pathlib import Path

from kalash.sandbox.policy import DEFAULT_PROTECTED_PATHS
from kalash.tools.base import (
    ToolContext,
    ToolEnvelope,
)

_MAX_READ_BYTES = 1_048_576  # 1 MiB
_MAX_READ_LINES = 2000
_MAX_GLOB_RESULTS = 500

# Binary detection: check first 8192 bytes for null bytes
_BINARY_CHECK_SIZE = 8192

# Default ignore patterns
_DEFAULT_IGNORE_PATTERNS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
    }
)


def _canonicalize(path: str | Path, ctx: ToolContext) -> Path:
    """NFC-normalize, resolve relative to cwd, resolve symlinks, collapse '..'."""
    raw = unicodedata.normalize("NFC", str(path))
    p = Path(raw)
    if not p.is_absolute():
        p = ctx.cwd / p
    p = p.resolve()
    return p


def _check_readable(path: Path, ctx: ToolContext) -> ToolEnvelope | None:
    """Return an error envelope if path cannot be read, else None."""
    if not path.exists():
        return ToolEnvelope.fail(
            code="KALASH_TOOL_ERROR",
            message=f"Path does not exist: {path}",
            recoverable=True,
            remediation="Check the path and try again.",
        )
    if not path.is_file():
        return ToolEnvelope.fail(
            code="KALASH_TOOL_ERROR",
            message=f"Path is not a file: {path}",
            recoverable=True,
            remediation="Use 'list' for directories.",
        )
    return None


def _is_protected(path: Path) -> str:
    """Return the matching protected pattern, or empty string if none (I-010).

    The pattern list lives in ``sandbox/policy.py`` and was never consulted from
    the write path, which meant ``.env``, ``~/.ssh/*``, and ``.git/hooks/*`` were
    writable by any tool call.

    Patterns are relative by nature (``.git/config``, ``**/*.pem``) while the
    paths reaching here are absolute, so matching only the full path misses
    almost everything. Every path suffix is tested, which is what makes
    ``/home/u/proj/.git/config`` match the pattern ``.git/config``.
    """
    text = str(path)
    home = str(Path.home())
    parts = path.parts

    # Every trailing sub-path: "a/b/c.py" -> {"a/b/c.py", "b/c.py", "c.py"}
    suffixes = {"/".join(parts[index:]) for index in range(len(parts))}

    for pattern in DEFAULT_PROTECTED_PATHS:
        expanded = pattern.replace("~", home)
        if fnmatch.fnmatch(text, expanded):
            return pattern
        if any(fnmatch.fnmatch(suffix, pattern) for suffix in suffixes):
            return pattern
    return ""


def _check_writable(path: Path, ctx: ToolContext) -> ToolEnvelope | None:
    """Return an error envelope if the path may not be written, else None.

    Fails **closed**: an unconfigured context denies every write rather than
    permitting them. ``ToolContext.writable_roots`` defaults to ``()``, so the
    previous "no restrictions configured" short-circuit meant that any caller
    which forgot to populate roots silently got unrestricted filesystem write
    access — the opposite of the intended default.
    """
    if protected := _is_protected(path):
        return ToolEnvelope.fail(
            code="KALASH_SANDBOX_PATH_PROTECTED",
            message=f"Refusing to write a protected path: {path} (matches {protected!r})",
            recoverable=False,
            remediation="Protected paths hold credentials or repository trust config.",
        )

    if not ctx.writable_roots:
        return ToolEnvelope.fail(
            code="KALASH_SANDBOX_PATH_DENIED",
            message=(
                "No writable roots are configured for this session, so writing is "
                "denied (fail-closed)."
            ),
            recoverable=False,
            remediation="The session must declare writable roots before tools can write.",
        )

    for root in ctx.writable_roots:
        try:
            path.relative_to(root)
            return None
        except ValueError:
            continue

    roots = ", ".join(str(r) for r in ctx.writable_roots)
    return ToolEnvelope.fail(
        code="KALASH_SANDBOX_PATH_DENIED",
        message=f"Path outside writable roots: {path}",
        recoverable=False,
        remediation=f"The file must be within: {roots}",
    )


def _content_digest(data: bytes) -> str:
    """SHA-256 hex digest of content."""
    return hashlib.sha256(data).hexdigest()


def _is_binary(data: bytes) -> bool:
    """Check if data looks like binary content."""
    return b"\x00" in data[:_BINARY_CHECK_SIZE]


def _detect_encoding(data: bytes) -> str:
    """Simple encoding detection — tries UTF-8, then latin-1 fallback."""
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "latin-1"


def _should_ignore(path: Path, ignore_patterns: frozenset[str] = _DEFAULT_IGNORE_PATTERNS) -> bool:
    """Check if any path component matches ignore patterns."""
    parts = path.parts
    return any(part in ignore_patterns for part in parts)


def _human_size(size: int) -> str:
    """Convert bytes to human-readable size."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024  # type: ignore[assignment]
    return f"{size:.1f}TB"
