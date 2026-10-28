"""bash, job_output and job_kill."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from pydantic import Field

from kalash.sandbox import SandboxPolicy, SandboxUnavailable, clean_env, wrap
from kalash.tools.base import Effects, Params, Tool, ToolContext, ToolResult
from kalash.tools.output import limit_output

DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 1_800_000


class BashParams(Params):
    description: str = Field(
        description="Clear, concise description of what this command does in active voice, "
        '5-10 words (shown in the UI). Example: "Run the unit tests".'
    )
    command: str = Field(description="The bash command to execute.")
    timeout_ms: int = Field(
        default=DEFAULT_TIMEOUT_MS,
        ge=1_000,
        le=MAX_TIMEOUT_MS,
        description="Timeout in milliseconds. On expiry the command keeps running as a "
        "background job instead of being killed.",
    )
    workdir: str | None = Field(
        default=None,
        description="Working directory for this command. Defaults to the session workspace; "
        "a relative path is resolved against it.",
    )
    run_in_background: bool = Field(
        default=False,
        description="Run in the background and return a job id immediately (collect with "
        "job_output, stop with job_kill). Use this for servers and watchers.",
    )
    require_escalated: bool = Field(
        default=False,
        description="Run this command OUTSIDE the sandbox. Requires the user's approval; set it "
        "only after a sandboxed attempt was blocked by policy and you can justify why.",
    )


class BashTool(Tool):
    name = "bash"
    description = (
        "Execute a bash command (`bash -c`) and return its combined stdout/stderr followed by "
        "`[exit code: N]`. Each call runs in a fresh shell; pass `workdir` instead of using "
        "`cd`. Commands run in an OS sandbox: they can read most of the filesystem but write only "
        "to the workspace (and temp), and a blocked write is reported as "
        "`[sandbox: ...]` - that is policy, do not retry another way. Processes started "
        "in the foreground end with the command; use run_in_background for servers. Long output "
        "keeps its head and tail and is saved to a file whose path is reported. Use `read`, "
        "`glob` and `grep` rather than cat/find/grep for inspecting files. Before any delete or "
        "move, verify the resolved absolute target path; guard variables in such paths with "
        "`${VAR:?}`."
    )
    params = BashParams

    def effects(self, params: Any, ctx: ToolContext) -> Effects:
        cwd = ctx.resolve(params.workdir) if params.workdir else ctx.cwd
        return Effects(reads=[cwd], execute=True, escalate=params.require_escalated)

    def permission_key(self, params: Any, ctx: ToolContext) -> str:
        return f"bash {params.command}"

    def describe(self, params: Any, ctx: ToolContext) -> str:
        where = ctx.resolve(params.workdir) if params.workdir else ctx.cwd
        where_text = f"in {where}"
        mode = "OUTSIDE the sandbox" if params.require_escalated else f"sandbox: {ctx.sandbox_mode}"
        return f"{params.description}\n$ {params.command}\n({where_text}; {mode})"

    async def run(self, params: BashParams, ctx: ToolContext) -> ToolResult:
        cwd = ctx.resolve(params.workdir) if params.workdir else ctx.cwd
        if not cwd.is_dir():
            return ToolResult(f"workdir {cwd} is not a directory.", is_error=True)
        shell = shutil.which("bash") or "/bin/sh"
        mode = "danger-full-access" if params.require_escalated else ctx.sandbox_mode
        policy = SandboxPolicy(
            mode, ctx.root, ctx.extras["session_tmp"], ctx.network, ctx.writable_roots
        )
        try:
            argv = wrap([shell, "-c", params.command], policy, cwd)
        except SandboxUnavailable as error:
            return ToolResult(str(error), is_error=True)
        env = clean_env(pass_env=ctx.extras.get("pass_env"), extra={"HOME": str(Path.home())})
        job = await ctx.jobs.start(argv, params.command, cwd, env)
        if params.run_in_background:
            return ToolResult(
                f"Started background job {job.id}. Collect output with job_output "
                f"and stop it with job_kill."
            )
        timeout = params.timeout_ms / 1000
        try:
            deadline = asyncio.get_running_loop().time() + timeout
            finished = False
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                finished = await ctx.jobs.wait(job, min(0.25, max(remaining, 0.0)))
                chunk = ctx.jobs.read_new(job)
                if chunk:
                    ctx.emit_output(chunk)
                if finished or remaining <= 0:
                    break
        except asyncio.CancelledError:
            await ctx.jobs.kill_hard(job)
            raise
        output = ctx.jobs.read_all(job)
        if not finished:
            body = limit_output(output, ctx.spill_dir, "bash")
            return ToolResult(
                f"{body}\n[still running after {timeout:.0f}s - moved to background as "
                f"{job.id}. Use job_output to read it or job_kill to stop it.]"
            )
        code = job.exit_code if job.exit_code is not None else -1
        body = limit_output(output, ctx.spill_dir, "bash")
        notes = ""
        if (
            code != 0
            and mode != "danger-full-access"
            and ("Read-only file system" in output or "Operation not permitted" in output)
        ):
            notes = (
                f"\n[sandbox: a file write was blocked under {mode} mode. This is policy; "
                f"do not retry another way. Write inside the workspace, or ask the user.]"
            )
        if code < 0:
            notes += f"\n[terminated by signal {-code}]"
        return ToolResult(
            f"{body}{notes}\n[exit code: {code}]", is_error=code != 0, metadata={"exit_code": code}
        )


class JobOutputParams(Params):
    job_id: str = Field(description="Job id returned by bash (e.g. job1).")
    wait_ms: int = Field(
        default=0, ge=0, le=300_000, description="Wait up to this long for the job to finish first."
    )


class JobOutputTool(Tool):
    name = "job_output"
    description = (
        "Read new output of a background bash job (since the last read) and report "
        "whether it is still running."
    )
    params = JobOutputParams
    read_only = True

    async def run(self, params: JobOutputParams, ctx: ToolContext) -> ToolResult:
        job = ctx.jobs.get(params.job_id)
        if job is None:
            known = ", ".join(j.id for j in ctx.jobs.all()) or "none"
            return ToolResult(f"No job {params.job_id!r}. Known jobs: {known}.", is_error=True)
        if params.wait_ms and job.running:
            await ctx.jobs.wait(job, params.wait_ms / 1000)
        text = limit_output(ctx.jobs.read_new(job), ctx.spill_dir, params.job_id)
        state = (
            "running"
            if job.running
            else f"finished (exit code {job.exit_code}{', killed' if job.killed else ''})"
        )
        return ToolResult(f"{text}\n[{job.id}: {state}]".lstrip("\n"))


class JobKillParams(Params):
    job_id: str = Field(description="Job id to stop.")


class JobKillTool(Tool):
    name = "job_kill"
    description = "Stop a background bash job and its process group."
    params = JobKillParams

    def permission_key(self, params: Any, ctx: ToolContext) -> str:
        return f"job_kill {params.job_id}"

    async def run(self, params: JobKillParams, ctx: ToolContext) -> ToolResult:
        job = ctx.jobs.get(params.job_id)
        if job is None:
            return ToolResult(f"No job {params.job_id!r}.", is_error=True)
        if not job.running:
            return ToolResult(f"{job.id} already finished (exit code {job.exit_code}).")
        await ctx.jobs.kill_hard(job)
        return ToolResult(f"Stopped {job.id}.")
