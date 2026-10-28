"""A real ToolContext for exercising tools directly."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kalash.paths import kalash_home
from kalash.tools.base import Tool, ToolContext, ToolResult
from kalash.tools.jobs import JobManager
from kalash.tools.observe import ObservationTracker


def make_ctx(
    work: Path, *, mode: str = "workspace-write", network: bool = True, **extras: Any
) -> ToolContext:
    home = kalash_home()
    return ToolContext(
        cwd=work,
        root=work,
        session_id="s",
        observations=ObservationTracker(),
        jobs=JobManager(home / "spill" / "s" / "jobs"),
        sandbox_mode=mode,
        network=network,
        writable_roots=[],
        spill_dir=home / "spill" / "s",
        extras={"session_tmp": home / "tmp" / "s", **extras},
    )


async def run_tool(tool: Tool, ctx: ToolContext, **arguments: Any) -> ToolResult:
    return await tool.run(tool.parse(arguments), ctx)
