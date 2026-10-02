"""Filesystem edit tools."""

from __future__ import annotations

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
    _check_readable,
    _check_writable,
    _content_digest,
    _detect_encoding,
)


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
            from kalash.core.errors import HookDeniedError
            from kalash.hooks.events import FileEditPayload, HookEvent

            try:
                await ctx.hooks.dispatch(
                    FileEditPayload(
                        event=HookEvent.PRE_FILE_EDIT,
                        session_id=ctx.session_id,
                        run_id=ctx.run_id,
                        path=str(path),
                        operation="edit",
                        content_preview=args.new_str[:100],
                    )
                )
            except HookDeniedError as e:
                return ToolEnvelope.fail(
                    code="KALASH_TOOL_DENIED", message=str(e), recoverable=True
                )

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
            from kalash.hooks.events import FileEditPayload, HookEvent

            try:
                await ctx.hooks.dispatch(
                    FileEditPayload(
                        event=HookEvent.POST_FILE_EDIT,
                        session_id=ctx.session_id,
                        run_id=ctx.run_id,
                        path=str(path),
                        operation="edit",
                        content_preview=args.new_str[:100],
                    )
                )
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
            from kalash.core.errors import HookDeniedError
            from kalash.hooks.events import FileEditPayload, HookEvent

            try:
                await ctx.hooks.dispatch(
                    FileEditPayload(
                        event=HookEvent.PRE_FILE_EDIT,
                        session_id=ctx.session_id,
                        run_id=ctx.run_id,
                        path=str(path),
                        operation="edit",
                        content_preview=text[:100],
                    )
                )
            except HookDeniedError as e:
                return ToolEnvelope.fail(
                    code="KALASH_TOOL_DENIED", message=str(e), recoverable=True
                )

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
            from kalash.hooks.events import FileEditPayload, HookEvent

            try:
                await ctx.hooks.dispatch(
                    FileEditPayload(
                        event=HookEvent.POST_FILE_EDIT,
                        session_id=ctx.session_id,
                        run_id=ctx.run_id,
                        path=str(path),
                        operation="edit",
                        content_preview=text[:100],
                    )
                )
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
