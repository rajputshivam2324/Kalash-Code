"""Ripgrep-backed content search.

Uses subprocess to run `rg` (ripgrep) with:
- Context lines support
- Respect for ignore files (.gitignore, .kalashignore)
- Capped results (100 matches)
- Capped line length (4000 chars)
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from kalash.tools.base import (
    SideEffect,
    ToolContext,
    ToolEnvelope,
    TruncationInfo,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_MATCHES = 100
_MAX_LINE_LENGTH = 4000
_DEFAULT_CONTEXT_LINES = 2
_SEARCH_TIMEOUT_S = 30.0


# ---------------------------------------------------------------------------
# SearchTool
# ---------------------------------------------------------------------------


class SearchParams(BaseModel):
    """Parameters for content search."""

    query: str = Field(description="Regex pattern to search for (Rust regex syntax)")
    path: str = Field(default=".", description="Directory or file to search in")
    include: str = Field(default="", description="Glob pattern to include (e.g. '**/*.py')")
    exclude: str = Field(default="", description="Glob pattern to exclude")
    context_lines: int = Field(
        default=_DEFAULT_CONTEXT_LINES, ge=0, le=10, description="Lines of context around matches"
    )
    case_sensitive: bool = Field(default=False, description="Case-sensitive search")
    fixed_strings: bool = Field(default=False, description="Treat query as literal (not regex)")


class SearchTool:
    """Ripgrep-backed content search with capped output."""

    @property
    def name(self) -> str:
        return "search"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return (
            "Search file contents using ripgrep regex. "
            "Returns matching lines with context. Respects ignore files."
        )

    @property
    def params(self) -> type[BaseModel]:
        return SearchParams

    @property
    def side_effect(self) -> SideEffect:
        return SideEffect.READ

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"fs.read"})

    @property
    def timeout_s(self) -> float:
        return _SEARCH_TIMEOUT_S

    @property
    def max_output_bytes(self) -> int:
        return 262_144  # 256 KiB

    @property
    def idempotent(self) -> bool:
        return True

    @property
    def cancellable(self) -> bool:
        return True

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:
        return frozenset()

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        assert isinstance(args, SearchParams)

        # Check ripgrep availability
        rg_path = shutil.which("rg")
        if not rg_path:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message="ripgrep (rg) not found in PATH.",
                recoverable=False,
                remediation="Install ripgrep: https://github.com/BurntSushi/ripgrep#installation",
            )

        # Resolve search path
        search_path = Path(args.path)
        if not search_path.is_absolute():
            search_path = ctx.cwd / search_path
        search_path = search_path.resolve()

        if not search_path.exists():
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"Search path does not exist: {search_path}",
                recoverable=True,
            )

        # Build rg command
        cmd = [
            rg_path,
            "--color=never",
            "--line-number",
            "--no-heading",
            f"--max-count={_MAX_MATCHES}",
            f"--max-columns={_MAX_LINE_LENGTH}",
            "--max-columns-preview",
            f"--context={args.context_lines}",
        ]

        if not args.case_sensitive:
            cmd.append("--ignore-case")

        if args.fixed_strings:
            cmd.append("--fixed-strings")

        if args.include:
            cmd.extend(["--glob", args.include])

        if args.exclude:
            cmd.extend(["--glob", f"!{args.exclude}"])

        cmd.append("--")
        cmd.append(args.query)
        cmd.append(str(search_path))

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(ctx.cwd),
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=_SEARCH_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_TIMEOUT",
                message=f"Search timed out after {_SEARCH_TIMEOUT_S}s",
                recoverable=True,
                remediation="Narrow the search scope or simplify the pattern.",
            )
        except OSError as exc:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"Failed to run ripgrep: {exc}",
                recoverable=True,
            )

        # rg exit codes: 0 = matches found, 1 = no matches, 2 = error
        if process.returncode == 2:
            error_msg = stderr.decode("utf-8", errors="replace").strip()
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"ripgrep error: {error_msg}",
                recoverable=True,
            )

        output = stdout.decode("utf-8", errors="replace")

        if process.returncode == 1 or not output.strip():
            return ToolEnvelope.success(
                content="No matches found.",
                metadata={"match_count": 0, "query": args.query},
            )

        # Count matches and check truncation
        lines = output.splitlines()
        match_lines = [l for l in lines if not l.startswith("--")]  # Exclude context separators
        match_count = len(match_lines)
        truncated = match_count >= _MAX_MATCHES

        truncation = (
            TruncationInfo(
                total_lines=match_count,
                shown_lines=match_count,
                total_bytes=len(stdout),
                retrieval_hint=f"Results capped at {_MAX_MATCHES} matches. Refine the pattern.",
            )
            if truncated
            else None
        )

        return ToolEnvelope.success(
            content=output,
            metadata={
                "match_count": match_count,
                "query": args.query,
                "search_path": str(search_path),
            },
            truncated=truncated,
            truncation=truncation,
        )
