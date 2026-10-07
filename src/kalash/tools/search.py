"""Ripgrep-backed content search.

Uses subprocess to run `rg` (ripgrep) with:
- Context lines support
- Respect for ignore files (.gitignore, .kalashignore)
- Capped results (100 matches)
- Capped line length (4000 chars)
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from pathlib import Path

from pydantic import BaseModel, Field

from kalash.sandbox.policy import DEFAULT_PROTECTED_PATHS
from kalash.tools.base import (
    SideEffect,
    ToolContext,
    ToolEnvelope,
    TruncationInfo,
)
from kalash.tools.fs.common import _is_protected

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_MATCHES = 100
_MAX_LINE_LENGTH = 4000
_DEFAULT_CONTEXT_LINES = 2
_SEARCH_TIMEOUT_S = 30.0


async def _bounded_output(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    chunks = bytearray()
    while True:
        chunk = await stream.read(min(8192, limit + 1 - len(chunks)))
        if not chunk:
            return bytes(chunks), False
        chunks.extend(chunk)
        if len(chunks) > limit:
            return bytes(chunks[:limit]), True


def _kill(process: asyncio.subprocess.Process) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
    except ProcessLookupError:
        pass


async def _discard(stream: asyncio.StreamReader | None) -> None:
    if stream is not None:
        while await stream.read(8192):
            pass


async def _reap(process: asyncio.subprocess.Process) -> None:
    _kill(process)
    # asyncio's process.wait can wait on paused full pipes even after SIGKILL.
    # Drain without retaining data once the collectors have joined/cancelled.
    await asyncio.gather(_discard(process.stdout), _discard(process.stderr))
    await process.wait()


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

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:  # noqa: ARG002 - Preserve provider/tool callback signature.
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

        protected = _is_protected(search_path)
        if search_path.is_dir():
            protected = protected or _is_protected(search_path / "__kalash_probe__")
        if protected:
            return ToolEnvelope.fail(
                "KALASH_SANDBOX_PATH_PROTECTED",
                "Refusing to search protected content",
                recoverable=False,
            )

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
        for pattern in DEFAULT_PROTECTED_PATHS:
            if not pattern.startswith("~/"):
                cmd.extend(["--glob", f"!{pattern}"])
        from kalash.core.paths import kalash_home

        for secret_path in (kalash_home() / "auth.json", kalash_home() / "auth.key"):
            if secret_path.is_relative_to(search_path):
                cmd.extend(["--glob", "!" + str(secret_path.relative_to(search_path))])

        cmd.append("--")
        cmd.append(args.query)
        cmd.append(str(search_path))

        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(ctx.cwd),
                start_new_session=os.name == "posix",
            )
            assert process.stdout is not None
            assert process.stderr is not None
            async with asyncio.timeout(_SEARCH_TIMEOUT_S):
                async with asyncio.TaskGroup() as group:
                    out_task = group.create_task(
                        _bounded_output(process.stdout, self.max_output_bytes)
                    )
                    err_task = group.create_task(_bounded_output(process.stderr, 8192))
                    # Finish/stop the process as soon as either stream hits its
                    # bound. Otherwise a full pipe can deadlock the other drain.
                    pending = {out_task, err_task}
                    while pending:
                        done, pending = await asyncio.wait(
                            pending, return_when=asyncio.FIRST_COMPLETED
                        )
                        if any(task.result()[1] for task in done):
                            _kill(process)
                            break
                stdout, output_limited = out_task.result()
                stderr, error_limited = err_task.result()
                if output_limited or error_limited:
                    await _reap(process)
                else:
                    await process.wait()
        except TimeoutError:
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
        finally:
            if process is not None:
                await _reap(process)

        # rg exit codes: 0 = matches found, 1 = no matches, 2 = error
        if error_limited or (process.returncode not in {0, 1} and not output_limited):
            error_msg = stderr.decode("utf-8", errors="replace").strip()
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"ripgrep error: {error_msg}",
                recoverable=True,
            )

        output = stdout.decode("utf-8", errors="ignore")

        if process.returncode == 1 or not output.strip():
            return ToolEnvelope.success(
                content="No matches found.",
                metadata={"match_count": 0, "query": args.query},
            )

        # Count matches and check truncation
        lines = output.splitlines()
        match_lines = [line for line in lines if not line.startswith("--")]
        match_count = len(match_lines)
        truncated = output_limited or match_count >= _MAX_MATCHES

        truncation = (
            TruncationInfo(
                total_lines=match_count,
                shown_lines=match_count,
                total_bytes=len(stdout),
                retrieval_hint=(
                    "Results bounded; total matches unknown. Refine the pattern or path."
                ),
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
