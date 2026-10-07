"""Canonical paths, permissions, digests and file metadata."""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import tempfile
import unicodedata
from pathlib import Path

from kalash.core.events import Event, EventType
from kalash.sandbox.policy import DEFAULT_PROTECTED_PATHS
from kalash.tools.base import (
    ToolContext,
    ToolEnvelope,
)

_MAX_READ_BYTES = 1_048_576  # 1 MiB
_MAX_READ_LINES = 2000
_MAX_GLOB_RESULTS = 500


async def post_file_edit(path: Path, operation: str, preview: str, ctx: ToolContext) -> list[str]:
    """Report post-effect hook failure without misreporting a committed write."""
    if ctx.hooks is None:
        return []
    from kalash.hooks.events import FileEditPayload, HookEvent

    warnings: list[str] = []
    try:
        results = await ctx.hooks.dispatch(
            FileEditPayload(
                event=HookEvent.POST_FILE_EDIT,
                session_id=ctx.session_id,
                run_id=ctx.run_id,
                path=str(path),
                operation=operation,
                content_preview=preview[:100],
            )
        )
        for result in results:
            if result.exit_code != 0 or result.error:
                warnings.append(f"PostFileEdit hook {result.hook_id} failed ({result.exit_code})")
    except Exception as error:
        warnings.append(f"PostFileEdit hook failed: {type(error).__name__}")
    if warnings:
        logging.getLogger(__name__).warning("%s; file effect completed", "; ".join(warnings))
        if ctx.event_bus is not None:
            await ctx.event_bus.emit(
                Event(
                    EventType.HOOK_RUN,
                    {
                        "phase": "PostFileEdit",
                        "status": "failed",
                        "path": str(path),
                        "effect_completed": True,
                        "warnings": warnings,
                    },
                    session_id=ctx.session_id,
                    run_id=ctx.run_id,
                )
            )
    return warnings


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
    return p.resolve()


def _check_readable(path: Path, ctx: ToolContext) -> ToolEnvelope | None:  # noqa: ARG001 - Preserve callback/keyword compatibility.
    """Return an error envelope if path cannot be read, else None."""
    if protected := _is_protected(path):
        return ToolEnvelope.fail(
            "KALASH_SANDBOX_PATH_PROTECTED",
            f"Refusing to expose protected content ({protected})",
            recoverable=False,
        )
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
    from kalash.core.paths import kalash_home

    if path in {kalash_home() / "auth.json", kalash_home() / "auth.key"}:
        return "credential storage"
    if path in {kalash_home() / "trusted-files.json", kalash_home() / "trusted-files.lock"}:
        return "user authorization storage"
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


def conditional_replace(
    path: Path, data: bytes, expected_digest: str, ctx: ToolContext, mode: int | None = None
) -> ToolEnvelope | None:
    """Revalidate after hooks and serialize cooperating writers without awaiting.

    External writers are not governed by this advisory lock. Revalidation narrows
    their race; portable replacement is not a universal filesystem CAS.
    """
    from kalash.core.paths import kalash_home

    temporary: Path | None = None
    lock_fd: int | None = None
    try:
        if path.resolve() != path:
            raise FileExistsError("Target ancestry changed")
        if error := _check_writable(path, ctx):
            return error
        locks = kalash_home() / "file-locks"
        locks.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = locks / hashlib.sha256(str(path).encode()).hexdigest()
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        if os.name == "posix":
            import fcntl

            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = (
            _content_digest(path.read_bytes()) if path.is_file() and not path.is_symlink() else ""
        )
        if current != expected_digest or (not expected_digest and path.exists()):
            raise FileExistsError("File changed since the validated read")
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".kalash_", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            if mode is not None:
                os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        # Check again immediately before committing, including ancestor changes.
        current = (
            _content_digest(path.read_bytes()) if path.is_file() and not path.is_symlink() else ""
        )
        if path.resolve() != path or current != expected_digest:
            raise FileExistsError("File changed before replacement")
        if expected_digest:
            Path(temporary).replace(path)
        else:
            os.link(temporary, path)  # Atomic no-clobber creation.
        if os.name == "posix":
            parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        return None
    except (FileExistsError, BlockingIOError) as error:
        return ToolEnvelope.fail(
            "KALASH_TOOL_STALE_READ",
            str(error),
            recoverable=True,
            remediation="Read the current file and review the intervening changes before retrying.",
        )
    except OSError as error:
        return ToolEnvelope.fail(
            "KALASH_TOOL_ERROR", f"Conditional write failed: {error}", recoverable=True
        )
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if lock_fd is not None:
            os.close(lock_fd)


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
