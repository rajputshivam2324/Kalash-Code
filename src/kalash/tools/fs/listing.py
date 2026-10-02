"""Filesystem listing tools."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field

from kalash.tools.base import (
    SideEffect,
    ToolContext,
    ToolEnvelope,
    TruncationInfo,
)

from .common import (
    _MAX_GLOB_RESULTS,
    _canonicalize,
    _human_size,
    _should_ignore,
)


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


class ListParams(BaseModel):
    """Parameters for directory listing."""

    path: str = Field(default=".", description="Directory path to list")
    depth: int = Field(
        default=1, ge=1, le=5, description="Recursion depth (1 = immediate children)"
    )


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
