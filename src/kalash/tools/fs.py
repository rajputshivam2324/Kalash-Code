"""Filesystem tools: read, write, edit, multi_edit, glob, list.

All tools implement the Tool protocol with proper SideEffect declarations.
Path canonicalization: NFC normalize, resolve symlinks, collapse '..'.
Writes are atomic (temp + fsync + os.replace) and require digest for overwrites (I-012).
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import os
import stat
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from kalash.core.diff import make_diff
from kalash.sandbox.policy import DEFAULT_PROTECTED_PATHS

from kalash.tools.base import (
    SideEffect,
    SideEffectRecord,
    ToolContext,
    ToolEnvelope,
    TruncationInfo,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MAX_READ_BYTES = 1_048_576  # 1 MiB
_MAX_READ_LINES = 2000
_MAX_GLOB_RESULTS = 500

# Binary detection: check first 8192 bytes for null bytes
_BINARY_CHECK_SIZE = 8192

# Default ignore patterns
_DEFAULT_IGNORE_PATTERNS = frozenset({
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
})


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


# ---------------------------------------------------------------------------
# ReadTool
# ---------------------------------------------------------------------------


class ReadParams(BaseModel):
    """Parameters for reading a file."""

    path: str = Field(description="Path to file to read")
    offset: int = Field(default=0, ge=0, description="Starting line (0-indexed)")
    limit: int = Field(default=_MAX_READ_LINES, ge=1, description="Max lines to return")


class ReadTool:
    """Reads files with line-range support, encoding detection, and binary detection."""

    @property
    def name(self) -> str:
        return "read"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Read a file's content with optional line-range selection."

    @property
    def params(self) -> type[BaseModel]:
        return ReadParams

    @property
    def side_effect(self) -> SideEffect:
        return SideEffect.READ

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"fs.read"})

    @property
    def timeout_s(self) -> float:
        return 30.0

    @property
    def max_output_bytes(self) -> int:
        return _MAX_READ_BYTES

    @property
    def idempotent(self) -> bool:
        return True

    @property
    def cancellable(self) -> bool:
        return False

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:
        return frozenset()

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        assert isinstance(args, ReadParams)
        path = _canonicalize(args.path, ctx)

        # Check readable
        if err := _check_readable(path, ctx):
            return err

        try:
            raw = path.read_bytes()
        except PermissionError:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"Permission denied reading: {path}",
                recoverable=False,
            )
        except OSError as exc:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"OS error reading {path}: {exc}",
                recoverable=True,
            )

        # Binary detection
        if _is_binary(raw):
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"File appears to be binary: {path}",
                recoverable=True,
                remediation="Use a specialized tool for binary files.",
            )

        # Decode
        encoding = _detect_encoding(raw)
        text = raw.decode(encoding)
        lines = text.splitlines(keepends=True)
        total_lines = len(lines)

        # Apply line range
        selected = lines[args.offset : args.offset + args.limit]
        shown_lines = len(selected)
        content = "".join(selected)

        truncated = shown_lines < total_lines
        truncation = (
            TruncationInfo(
                total_lines=total_lines,
                shown_lines=shown_lines,
                total_bytes=len(raw),
                retrieval_hint=f"Use offset={args.offset + shown_lines} to continue.",
            )
            if truncated
            else None
        )

        digest = _content_digest(raw)

        return ToolEnvelope.success(
            content=content,
            metadata={
                "path": str(path),
                "content_digest": digest,
                "encoding": encoding,
                "total_lines": total_lines,
                "shown_lines": shown_lines,
            },
            truncated=truncated,
            truncation=truncation,
        )


# ---------------------------------------------------------------------------
# WriteTool
# ---------------------------------------------------------------------------


class WriteParams(BaseModel):
    """Parameters for writing a file."""

    path: str = Field(description="Path to file to create/overwrite")
    content: str = Field(description="Content to write")
    digest: str = Field(
        default="",
        description="Expected SHA-256 digest of current content (required for overwrites, I-012)",
    )
    create_dirs: bool = Field(default=True, description="Create parent directories if needed")


class WriteTool:
    """Creates or overwrites files atomically (temp + fsync + os.replace).

    Requires digest for overwrites (I-012 invariant).
    Preserves metadata (mode, executable bit) on overwrite.
    """

    @property
    def name(self) -> str:
        return "write"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Create or overwrite a file atomically. Requires digest for existing files."

    @property
    def params(self) -> type[BaseModel]:
        return WriteParams

    @property
    def side_effect(self) -> SideEffect:
        return SideEffect.WRITE

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"fs.write"})

    @property
    def timeout_s(self) -> float:
        return 30.0

    @property
    def max_output_bytes(self) -> int:
        return 4096

    @property
    def idempotent(self) -> bool:
        return False

    @property
    def cancellable(self) -> bool:
        return False

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:
        return frozenset()

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        assert isinstance(args, WriteParams)
        path = _canonicalize(args.path, ctx)

        # Check writable
        if err := _check_writable(path, ctx):
            return err

        existing_mode: int | None = None
        previous_text = ""
        if path.exists():
            # Captured before the write so a diff can be shown. Read failures are
            # non-fatal: the diff is a display nicety, the write is the contract.
            with contextlib.suppress(OSError, UnicodeDecodeError):
                previous_text = path.read_text(encoding="utf-8", errors="replace")
            current_digest = _content_digest(path.read_bytes())
            if not args.digest:
                return ToolEnvelope.fail(
                    code="KALASH_TOOL_STALE_READ",
                    message=f"File exists; provide digest='{current_digest}' to confirm overwrite (I-012).",
                    recoverable=True,
                    remediation=f"Pass digest='{current_digest}' in the write call to overwrite.",
                )
            if args.digest != current_digest:
                return ToolEnvelope.fail(
                    code="KALASH_TOOL_STALE_READ",
                    message=f"Digest mismatch — file changed since last read. Current digest is '{current_digest}'.",
                    recoverable=True,
                    remediation=f"Pass digest='{current_digest}' in the write call.",
                )
            # Preserve metadata
            st = path.stat()
            existing_mode = stat.S_IMODE(st.st_mode)

        # Create parent dirs
        if args.create_dirs:
            path.parent.mkdir(parents=True, exist_ok=True)

        if ctx.hooks:
            from kalash.hooks.events import HookEvent, FileEditPayload
            from kalash.core.errors import HookDeniedError
            try:
                await ctx.hooks.dispatch(FileEditPayload(
                    event=HookEvent.PRE_FILE_EDIT,
                    session_id=ctx.session_id,
                    run_id=ctx.run_id,
                    path=str(path),
                    operation="edit" if existing_mode is not None else "create",
                    content_preview=args.content[:100],
                ))
            except HookDeniedError as e:
                return ToolEnvelope.fail(code="KALASH_TOOL_DENIED", message=str(e), recoverable=True)
        
        # Atomic write: temp -> fsync -> replace
        content_bytes = args.content.encode("utf-8")
        tmp_path: Path | None = None
        try:
            fd = tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=".kalash_",
                suffix=".tmp",
                delete=False,
            )
            tmp_path = Path(fd.name)
            try:
                fd.write(content_bytes)
                fd.flush()
                os.fsync(fd.fileno())
            finally:
                fd.close()

            # Restore permissions if overwriting
            if existing_mode is not None:
                os.chmod(tmp_path, existing_mode)

            os.replace(tmp_path, path)
        except OSError as exc:
            # Clean up temp file on failure
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"Write failed: {exc}",
                recoverable=True,
            )

        new_digest = _content_digest(content_bytes)
        kind = "modified" if existing_mode is not None else "created"

        if ctx.hooks:
            from kalash.hooks.events import HookEvent, FileEditPayload
            try:
                await ctx.hooks.dispatch(FileEditPayload(
                    event=HookEvent.POST_FILE_EDIT,
                    session_id=ctx.session_id,
                    run_id=ctx.run_id,
                    path=str(path),
                    operation="edit" if existing_mode is not None else "create",
                    content_preview=args.content[:100],
                ))
            except Exception:
                pass

        diff = make_diff(str(path), previous_text, args.content)

        return ToolEnvelope.success(
            content=f"Wrote {len(content_bytes)} bytes to {path} ({diff.stat.render()})",
            metadata={
                "path": str(path),
                "content_digest": new_digest,
                "bytes_written": len(content_bytes),
                # Consumed by the UI to render the change inline. Reporting only a
                # byte count gave the user no way to review what happened.
                "diff": diff.text,
                "diff_stat": diff.stat.render(),
                "lines_added": diff.stat.added,
                "lines_removed": diff.stat.removed,
                "operation": kind,
            },
            side_effects=(SideEffectRecord(kind=kind, path=str(path), digest=new_digest),),
        )


# ---------------------------------------------------------------------------
# EditTool
# ---------------------------------------------------------------------------


class EditParams(BaseModel):
    """Parameters for exact string replacement."""

    path: str = Field(description="Path to file to edit")
    old_str: str = Field(description="Exact string to find (must match exactly once)")
    new_str: str = Field(description="Replacement string")
    digest: str = Field(description="SHA-256 digest from prior read (required)")


class EditTool:
    """Exact string replacement — fails on 0 or >1 matches. Requires digest."""

    @property
    def name(self) -> str:
        return "edit"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Replace an exact string in a file. Fails if 0 or >1 matches found."

    @property
    def params(self) -> type[BaseModel]:
        return EditParams

    @property
    def side_effect(self) -> SideEffect:
        return SideEffect.WRITE

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"fs.write"})

    @property
    def timeout_s(self) -> float:
        return 30.0

    @property
    def max_output_bytes(self) -> int:
        return 4096

    @property
    def idempotent(self) -> bool:
        return False

    @property
    def cancellable(self) -> bool:
        return False

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:
        return frozenset()

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        assert isinstance(args, EditParams)
        path = _canonicalize(args.path, ctx)

        if err := _check_readable(path, ctx):
            return err
        if err := _check_writable(path, ctx):
            return err

        raw = path.read_bytes()
        current_digest = _content_digest(raw)
        if args.digest != current_digest:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_STALE_READ",
                message="Digest mismatch — file changed since last read.",
                recoverable=True,
                remediation="Re-read the file to get the current digest.",
            )

        encoding = _detect_encoding(raw)
        text = raw.decode(encoding)

        # Count occurrences
        count = text.count(args.old_str)
        if count == 0:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message="old_str not found in file.",
                recoverable=True,
                remediation="Verify the exact string including whitespace and line endings.",
            )
        if count > 1:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"old_str found {count} times; must match exactly once.",
                recoverable=True,
                remediation="Include more surrounding context to make the match unique.",
            )

        # Perform replacement
        new_text = text.replace(args.old_str, args.new_str, 1)
        new_bytes = new_text.encode(encoding)

        if ctx.hooks:
            from kalash.hooks.events import HookEvent, FileEditPayload
            from kalash.core.errors import HookDeniedError
            try:
                await ctx.hooks.dispatch(FileEditPayload(
                    event=HookEvent.PRE_FILE_EDIT,
                    session_id=ctx.session_id,
                    run_id=ctx.run_id,
                    path=str(path),
                    operation="edit",
                    content_preview=args.new_str[:100],
                ))
            except HookDeniedError as e:
                return ToolEnvelope.fail(code="KALASH_TOOL_DENIED", message=str(e), recoverable=True)

        # Atomic write
        existing_mode = stat.S_IMODE(path.stat().st_mode)
        fd = tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=".kalash_", suffix=".tmp", delete=False
        )
        tmp_path = Path(fd.name)
        try:
            fd.write(new_bytes)
            fd.flush()
            os.fsync(fd.fileno())
            fd.close()
            os.chmod(tmp_path, existing_mode)
            os.replace(tmp_path, path)
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"Edit write failed: {exc}",
                recoverable=True,
            )

        new_digest = _content_digest(new_bytes)

        if ctx.hooks:
            from kalash.hooks.events import HookEvent, FileEditPayload
            try:
                await ctx.hooks.dispatch(FileEditPayload(
                    event=HookEvent.POST_FILE_EDIT,
                    session_id=ctx.session_id,
                    run_id=ctx.run_id,
                    path=str(path),
                    operation="edit",
                    content_preview=args.new_str[:100],
                ))
            except Exception:
                pass

        diff = make_diff(str(path), text, new_text)
        return ToolEnvelope.success(
            content=f"Edited {path} ({diff.stat.render()})",
            metadata={
                "path": str(path),
                "content_digest": new_digest,
                "diff": diff.text,
                "diff_stat": diff.stat.render(),
                "lines_added": diff.stat.added,
                "lines_removed": diff.stat.removed,
                "operation": "modified",
            },
            side_effects=(SideEffectRecord(kind="modified", path=str(path), digest=new_digest),),
        )


# ---------------------------------------------------------------------------
# MultiEditTool
# ---------------------------------------------------------------------------


class EditOperation(BaseModel):
    """A single edit within a multi-edit batch."""

    old_str: str = Field(description="Exact string to find")
    new_str: str = Field(description="Replacement string")


class MultiEditParams(BaseModel):
    """Parameters for batched edits (all-or-nothing)."""

    path: str = Field(description="Path to file to edit")
    edits: list[EditOperation] = Field(description="Ordered list of edits to apply")
    digest: str = Field(description="SHA-256 digest from prior read")


class MultiEditTool:
    """Batched edits — all-or-nothing. Each edit must match exactly once."""

    @property
    def name(self) -> str:
        return "multi_edit"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Apply multiple edits to a file atomically. All must match exactly once."

    @property
    def params(self) -> type[BaseModel]:
        return MultiEditParams

    @property
    def side_effect(self) -> SideEffect:
        return SideEffect.WRITE

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"fs.write"})

    @property
    def timeout_s(self) -> float:
        return 30.0

    @property
    def max_output_bytes(self) -> int:
        return 4096

    @property
    def idempotent(self) -> bool:
        return False

    @property
    def cancellable(self) -> bool:
        return False

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:
        return frozenset()

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        assert isinstance(args, MultiEditParams)
        path = _canonicalize(args.path, ctx)

        if err := _check_readable(path, ctx):
            return err
        if err := _check_writable(path, ctx):
            return err

        raw = path.read_bytes()
        current_digest = _content_digest(raw)
        if args.digest != current_digest:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_STALE_READ",
                message="Digest mismatch — file changed since last read.",
                recoverable=True,
            )

        encoding = _detect_encoding(raw)
        text = raw.decode(encoding)

        # Validate all edits before applying (all-or-nothing)
        for i, edit in enumerate(args.edits):
            count = text.count(edit.old_str)
            if count == 0:
                return ToolEnvelope.fail(
                    code="KALASH_TOOL_ERROR",
                    message=f"Edit #{i}: old_str not found.",
                    recoverable=True,
                )
            if count > 1:
                return ToolEnvelope.fail(
                    code="KALASH_TOOL_ERROR",
                    message=f"Edit #{i}: old_str found {count} times; must match exactly once.",
                    recoverable=True,
                )

        # Apply edits in order
        for edit in args.edits:
            text = text.replace(edit.old_str, edit.new_str, 1)

        new_bytes = text.encode(encoding)

        if ctx.hooks:
            from kalash.hooks.events import HookEvent, FileEditPayload
            from kalash.core.errors import HookDeniedError
            try:
                await ctx.hooks.dispatch(FileEditPayload(
                    event=HookEvent.PRE_FILE_EDIT,
                    session_id=ctx.session_id,
                    run_id=ctx.run_id,
                    path=str(path),
                    operation="edit",
                    content_preview=text[:100],
                ))
            except HookDeniedError as e:
                return ToolEnvelope.fail(code="KALASH_TOOL_DENIED", message=str(e), recoverable=True)

        # Atomic write
        existing_mode = stat.S_IMODE(path.stat().st_mode)
        fd = tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=".kalash_", suffix=".tmp", delete=False
        )
        tmp_path = Path(fd.name)
        try:
            fd.write(new_bytes)
            fd.flush()
            os.fsync(fd.fileno())
            fd.close()
            os.chmod(tmp_path, existing_mode)
            os.replace(tmp_path, path)
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"Multi-edit write failed: {exc}",
                recoverable=True,
            )

        new_digest = _content_digest(new_bytes)

        if ctx.hooks:
            from kalash.hooks.events import HookEvent, FileEditPayload
            try:
                await ctx.hooks.dispatch(FileEditPayload(
                    event=HookEvent.POST_FILE_EDIT,
                    session_id=ctx.session_id,
                    run_id=ctx.run_id,
                    path=str(path),
                    operation="edit",
                    content_preview=text[:100],
                ))
            except Exception:
                pass
        return ToolEnvelope.success(
            content=f"Applied {len(args.edits)} edits to {path}",
            metadata={
                "path": str(path),
                "content_digest": new_digest,
                "edits_applied": len(args.edits),
            },
            side_effects=(SideEffectRecord(kind="modified", path=str(path), digest=new_digest),),
        )


# ---------------------------------------------------------------------------
# GlobTool
# ---------------------------------------------------------------------------


class GlobParams(BaseModel):
    """Parameters for glob-based file discovery."""

    pattern: str = Field(description="Glob pattern (e.g. '**/*.py')")
    path: str = Field(default=".", description="Base directory for the search")
    respect_ignore: bool = Field(default=True, description="Respect .gitignore/.kalashignore")


class GlobTool:
    """Path-pattern file discovery respecting .gitignore/.kalashignore."""

    @property
    def name(self) -> str:
        return "glob"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Find files matching a glob pattern. Respects ignore files by default."

    @property
    def params(self) -> type[BaseModel]:
        return GlobParams

    @property
    def side_effect(self) -> SideEffect:
        return SideEffect.READ

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"fs.read"})

    @property
    def timeout_s(self) -> float:
        return 30.0

    @property
    def max_output_bytes(self) -> int:
        return 65536

    @property
    def idempotent(self) -> bool:
        return True

    @property
    def cancellable(self) -> bool:
        return False

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:
        return frozenset()

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        assert isinstance(args, GlobParams)
        base = _canonicalize(args.path, ctx)

        if not base.is_dir():
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"Base path is not a directory: {base}",
                recoverable=True,
            )

        results: list[str] = []
        truncated = False

        try:
            for match in sorted(base.glob(args.pattern)):
                if args.respect_ignore and _should_ignore(match):
                    continue
                results.append(str(match))
                if len(results) >= _MAX_GLOB_RESULTS:
                    truncated = True
                    break
        except OSError as exc:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"Glob error: {exc}",
                recoverable=True,
            )

        content = "\n".join(results)
        truncation = (
            TruncationInfo(
                total_lines=len(results),
                shown_lines=len(results),
                total_bytes=len(content.encode()),
                retrieval_hint=f"Capped at {_MAX_GLOB_RESULTS} results; refine pattern.",
            )
            if truncated
            else None
        )

        return ToolEnvelope.success(
            content=content,
            metadata={"count": len(results), "pattern": args.pattern, "base": str(base)},
            truncated=truncated,
            truncation=truncation,
        )


# ---------------------------------------------------------------------------
# ListTool
# ---------------------------------------------------------------------------


class ListParams(BaseModel):
    """Parameters for directory listing."""

    path: str = Field(default=".", description="Directory path to list")
    depth: int = Field(default=1, ge=1, le=5, description="Recursion depth (1 = immediate children)")


class ListTool:
    """Directory listing with file types."""

    @property
    def name(self) -> str:
        return "list"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "List directory contents with file types and sizes."

    @property
    def params(self) -> type[BaseModel]:
        return ListParams

    @property
    def side_effect(self) -> SideEffect:
        return SideEffect.READ

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"fs.read"})

    @property
    def timeout_s(self) -> float:
        return 15.0

    @property
    def max_output_bytes(self) -> int:
        return 65536

    @property
    def idempotent(self) -> bool:
        return True

    @property
    def cancellable(self) -> bool:
        return False

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:
        return frozenset()

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        assert isinstance(args, ListParams)
        base = _canonicalize(args.path, ctx)

        if not base.exists():
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"Path does not exist: {base}",
                recoverable=True,
            )
        if not base.is_dir():
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"Path is not a directory: {base}",
                recoverable=True,
                remediation="Use 'read' for files.",
            )

        entries: list[str] = []

        def _list_recursive(dir_path: Path, current_depth: int, prefix: str = "") -> None:
            if current_depth > args.depth:
                return
            try:
                items = sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
            except PermissionError:
                entries.append(f"{prefix}[permission denied]")
                return

            for item in items:
                if _should_ignore(item):
                    continue
                if item.is_dir():
                    entries.append(f"{prefix}{item.name}/")
                    if current_depth < args.depth:
                        _list_recursive(item, current_depth + 1, prefix + "  ")
                elif item.is_symlink():
                    entries.append(f"{prefix}{item.name} -> {os.readlink(item)}")
                else:
                    try:
                        size = item.stat().st_size
                    except OSError:
                        size = 0
                    entries.append(f"{prefix}{item.name}  ({_human_size(size)})")

        _list_recursive(base, 1)
        content = "\n".join(entries)

        return ToolEnvelope.success(
            content=content,
            metadata={"path": str(base), "entry_count": len(entries)},
        )


def _human_size(size: int) -> str:
    """Convert bytes to human-readable size."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024  # type: ignore[assignment]
    return f"{size:.1f}TB"
