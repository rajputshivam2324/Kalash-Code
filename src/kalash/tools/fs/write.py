"""Filesystem write tools."""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from kalash.core.diff import make_diff
from kalash.tools.base import (
    SideEffect,
    SideEffectRecord,
    ToolContext,
    ToolEnvelope,
)

from .common import (
    _canonicalize,
    _check_writable,
    _content_digest,
)


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
            from kalash.core.errors import HookDeniedError
            from kalash.hooks.events import FileEditPayload, HookEvent

            try:
                await ctx.hooks.dispatch(
                    FileEditPayload(
                        event=HookEvent.PRE_FILE_EDIT,
                        session_id=ctx.session_id,
                        run_id=ctx.run_id,
                        path=str(path),
                        operation="edit" if existing_mode is not None else "create",
                        content_preview=args.content[:100],
                    )
                )
            except HookDeniedError as e:
                return ToolEnvelope.fail(
                    code="KALASH_TOOL_DENIED", message=str(e), recoverable=True
                )

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
            from kalash.hooks.events import FileEditPayload, HookEvent

            try:
                await ctx.hooks.dispatch(
                    FileEditPayload(
                        event=HookEvent.POST_FILE_EDIT,
                        session_id=ctx.session_id,
                        run_id=ctx.run_id,
                        path=str(path),
                        operation="edit" if existing_mode is not None else "create",
                        content_preview=args.content[:100],
                    )
                )
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
