"""Filesystem read tools."""

from __future__ import annotations

import asyncio
import io
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from pathlib import Path

MAX_SOURCE_BYTES = 16 * 1024 * 1024


def _read_bounded(path: Path) -> bytes:
    # Bound acquisition even if the file grows after stat. Run file IO outside
    # the event loop; an output limit alone cannot bound a minified input.
    with path.open("rb") as stream:
        return stream.read(MAX_SOURCE_BYTES + 1)


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

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:  # noqa: ARG002 - Preserve provider/tool callback signature.
        return frozenset()

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        assert isinstance(args, ReadParams)
        path = _canonicalize(args.path, ctx)

        # Check readable
        if err := _check_readable(path, ctx):
            return err

        return await asyncio.to_thread(self._read, path, args)

    @staticmethod
    def _read(path: Path, args: ReadParams) -> ToolEnvelope:
        try:
            raw = _read_bounded(path)
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

        if len(raw) > MAX_SOURCE_BYTES:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_RESOURCE_LIMIT",
                message=f"Source exceeds the {MAX_SOURCE_BYTES}-byte read limit: {path}",
                recoverable=True,
                remediation="Use search to select relevant content or split the source file.",
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
        # Do not allocate a Python list for every line of a newline-heavy file.
        total_lines = 0
        shown_lines = 0
        selected = bytearray()
        bytes_truncated = False
        for line in io.StringIO(text, newline=""):
            if args.offset <= total_lines < args.offset + args.limit:
                encoded = line.encode("utf-8")
                room = _MAX_READ_BYTES - len(selected)
                if len(encoded) > room:
                    bytes_truncated = True
                if room:
                    selected.extend(encoded[:room])
                    shown_lines += 1
            total_lines += 1
        content = selected.decode("utf-8", errors="ignore")

        truncated = bytes_truncated or shown_lines < total_lines
        truncation = (
            TruncationInfo(
                total_lines=total_lines,
                shown_lines=shown_lines,
                total_bytes=len(raw),
                retrieval_hint=(
                    "Output byte limit reached; use search for long lines or a narrower line range."
                    if bytes_truncated
                    else f"Use offset={args.offset + shown_lines} to continue."
                ),
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
