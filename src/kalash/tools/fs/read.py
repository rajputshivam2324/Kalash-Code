"""Filesystem read tools."""

from __future__ import annotations

from pydantic import BaseModel, Field

from kalash.tools.base import (
    SideEffect,
    ToolContext,
    ToolEnvelope,
    TruncationInfo,
)

from .common import (
    _MAX_READ_BYTES,
    _MAX_READ_LINES,
    _canonicalize,
    _check_readable,
    _content_digest,
    _detect_encoding,
    _is_binary,
)


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
