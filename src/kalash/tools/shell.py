"""Sandboxed command execution.

Runs commands in platform shell (bash -c) with:
- New process group (setsid) for clean termination
- Timeout with SIGTERM -> 5s grace -> SIGKILL
- Environment allowlist (strips LD_PRELOAD, NODE_OPTIONS, etc.)
- stdin=/dev/null by default
- Output streaming and capping (128 KiB each for stdout/stderr)
- Background mode support
- Command classification for dynamic_capabilities
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from kalash.tools.base import (
    SideEffect,
    SideEffectRecord,
    ToolContext,
    ToolEnvelope,
    TruncationInfo,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_OUTPUT_BYTES = 131_072  # 128 KiB per stream
_DEFAULT_TIMEOUT_S = 120.0
_GRACE_PERIOD_S = 5.0

# Environment variables stripped for safety
_STRIPPED_ENV_VARS = frozenset({
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "NODE_OPTIONS",
    "PYTHONSTARTUP",
    "PYTHONPATH",
    "PERL5OPT",
    "RUBYOPT",
    "BASH_ENV",
    "ENV",
    "CDPATH",
})

# Command patterns that require elevated capabilities
_DANGEROUS_PATTERNS = {
    "sudo": "shell.sudo",
    "rm -rf": "shell.destructive",
    "rm -fr": "shell.destructive",
    "chmod": "shell.permissions",
    "chown": "shell.permissions",
    "git push": "git.push",
    "git push --force": "git.force_push",
    "git reset --hard": "git.destructive",
    "git clean": "git.destructive",
    "docker": "shell.docker",
    "kubectl": "shell.kubernetes",
    "curl": "shell.network",
    "wget": "shell.network",
    "ssh": "shell.network",
    "scp": "shell.network",
}


def _classify_command(command: str) -> frozenset[str]:
    """Identify capabilities needed based on command content."""
    caps: set[str] = set()
    cmd_lower = command.lower().strip()
    for pattern, cap in _DANGEROUS_PATTERNS.items():
        if pattern in cmd_lower:
            caps.add(cap)
    return frozenset(caps)


def _safe_env() -> dict[str, str]:
    """Create a sanitized environment, stripping dangerous variables."""
    env = dict(os.environ)
    for var in _STRIPPED_ENV_VARS:
        env.pop(var, None)
    return env


# ---------------------------------------------------------------------------
# Shell tool
# ---------------------------------------------------------------------------


class ShellParams(BaseModel):
    """Parameters for shell command execution."""

    command: str = Field(description="Shell command to execute (passed to bash -c)")
    cwd: str = Field(default="", description="Working directory (never use cd; use this instead)")
    timeout: float = Field(default=_DEFAULT_TIMEOUT_S, gt=0, le=1800, description="Timeout in seconds")
    background: bool = Field(default=False, description="Run in background (returns immediately)")


class ShellTool:
    """Sandboxed command execution with process isolation and output capping."""

    @property
    def name(self) -> str:
        return "shell"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return (
            "Execute a shell command. Uses bash -c with process isolation. "
            "Never use 'cd' — use the cwd parameter instead."
        )

    @property
    def params(self) -> type[BaseModel]:
        return ShellParams

    @property
    def side_effect(self) -> SideEffect:
        return SideEffect.EXEC

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"shell.exec"})

    @property
    def timeout_s(self) -> float:
        return _DEFAULT_TIMEOUT_S

    @property
    def max_output_bytes(self) -> int:
        return _MAX_OUTPUT_BYTES * 2  # stdout + stderr

    @property
    def idempotent(self) -> bool:
        return False

    @property
    def cancellable(self) -> bool:
        return True

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:
        """Classify command to determine additional required capabilities."""
        assert isinstance(args, ShellParams)
        return _classify_command(args.command)

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        assert isinstance(args, ShellParams)

        # Resolve working directory
        cwd = Path(args.cwd) if args.cwd else ctx.cwd
        if not cwd.is_absolute():
            cwd = ctx.cwd / cwd
        cwd = cwd.resolve()

        if not cwd.is_dir():
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"Working directory does not exist: {cwd}",
                recoverable=True,
            )

        # Determine shell
        shell = "/bin/bash"
        if sys.platform == "win32":
            shell = "cmd.exe"

        env = _safe_env()
        env["KALASH_TOOL"] = "1"  # Signal to child that it's inside Kalash

        try:
            # Use setsid for process group isolation on Unix
            kwargs: dict[str, Any] = {
                "cwd": str(cwd),
                "env": env,
                "stdin": asyncio.subprocess.DEVNULL,
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
            }
            if sys.platform != "win32":
                kwargs["preexec_fn"] = os.setsid

            if sys.platform == "win32":
                process = await asyncio.create_subprocess_shell(
                    args.command, **kwargs
                )
            else:
                process = await asyncio.create_subprocess_exec(
                    shell, "-c", args.command, **kwargs
                )

            # Background mode: return immediately
            if args.background:
                return ToolEnvelope.success(
                    content=f"Background process started (PID: {process.pid})",
                    metadata={
                        "pid": process.pid,
                        "command": args.command,
                        "background": True,
                    },
                    side_effects=(
                        SideEffectRecord(kind="executed", path=args.command),
                    ),
                )

            # Wait with timeout
            stdout_data, stderr_data = await asyncio.wait_for(
                process.communicate(),
                timeout=args.timeout,
            )

        except asyncio.TimeoutError:
            # SIGTERM -> grace period -> SIGKILL
            await _terminate_process(process)
            return ToolEnvelope.fail(
                code="KALASH_TOOL_TIMEOUT",
                message=f"Command timed out after {args.timeout}s: {args.command}",
                recoverable=True,
                remediation="Increase timeout or use background mode.",
            )
        except OSError as exc:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"Failed to start command: {exc}",
                recoverable=True,
            )

        # Cap output
        stdout_truncated = len(stdout_data) > _MAX_OUTPUT_BYTES
        stderr_truncated = len(stderr_data) > _MAX_OUTPUT_BYTES
        stdout_str = stdout_data[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        stderr_str = stderr_data[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")

        truncated = stdout_truncated or stderr_truncated
        truncation = (
            TruncationInfo(
                total_lines=0,
                shown_lines=0,
                total_bytes=len(stdout_data) + len(stderr_data),
                retrieval_hint="Output exceeded 128 KiB per stream and was capped.",
            )
            if truncated
            else None
        )

        # Build content
        parts: list[str] = []
        if stdout_str:
            parts.append(stdout_str)
        if stderr_str:
            parts.append(f"[stderr]\n{stderr_str}")

        content = "\n".join(parts)
        exit_code = process.returncode or 0

        return ToolEnvelope(
            ok=exit_code == 0,
            content=content,
            metadata={
                "exit_code": exit_code,
                "command": args.command,
                "cwd": str(cwd),
                "stdout_bytes": len(stdout_data),
                "stderr_bytes": len(stderr_data),
            },
            truncated=truncated,
            truncation=truncation,
            error=(
                ToolEnvelope.fail(
                    code="KALASH_TOOL_ERROR",
                    message=f"Command exited with code {exit_code}",
                    recoverable=True,
                ).error
                if exit_code != 0
                else None
            ),
            side_effects=(SideEffectRecord(kind="executed", path=args.command),),
        )


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Graceful termination: SIGTERM -> 5s -> SIGKILL."""
    if process.returncode is not None:
        return  # Already exited

    try:
        if sys.platform != "win32":
            # Kill entire process group
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGTERM)
        else:
            process.terminate()
    except (ProcessLookupError, OSError):
        return

    # Grace period
    try:
        await asyncio.wait_for(process.wait(), timeout=_GRACE_PERIOD_S)
    except asyncio.TimeoutError:
        try:
            if sys.platform != "win32":
                pgid = os.getpgid(process.pid)
                os.killpg(pgid, signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, OSError):
            pass
        await process.wait()
