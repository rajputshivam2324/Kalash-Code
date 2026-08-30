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
import contextlib
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

# Background processes keyed by session → pid → process handle.
_BACKGROUND: dict[str, dict[int, asyncio.subprocess.Process]] = {}


async def terminate_session_backgrounds(session_id: str) -> list[int]:
    """Kill tracked background shell processes for a session."""
    procs = _BACKGROUND.pop(session_id, {})
    killed: list[int] = []
    for pid, process in procs.items():
        await _terminate_process(process)
        killed.append(pid)
    return killed


def list_background_pids(session_id: str) -> list[int]:
    """Return PIDs of background shells still running for a session."""
    return list(_BACKGROUND.get(session_id, {}).keys())

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
    # SEC-2: system-level destructive commands
    "mkfs": "shell.destructive",
    "dd": "shell.destructive",
    "reboot": "shell.destructive",
    "shutdown": "shell.destructive",
    "halt": "shell.destructive",
    "poweroff": "shell.destructive",
    "iptables": "shell.network",
    "nft": "shell.network",
    "systemctl": "shell.destructive",
    "crontab": "shell.destructive",
    "pkill": "shell.destructive",
    "killall": "shell.destructive",
    "mount": "shell.destructive",
    "umount": "shell.destructive",
    "fdisk": "shell.destructive",
}


# P-2: Pre-compile word-boundary regex for single-word patterns at module load
# so _classify_command doesn't recompile on every call.
import re as _re

_COMPILED_PATTERNS: list[tuple[_re.Pattern[str], str]] = []
_MULTI_WORD_PATTERNS: list[tuple[str, str]] = []

for _pat, _cap in _DANGEROUS_PATTERNS.items():
    if " " in _pat:
        _MULTI_WORD_PATTERNS.append((_pat, _cap))
    else:
        _COMPILED_PATTERNS.append((_re.compile(rf'\b{_re.escape(_pat)}\b'), _cap))


def _classify_command(command: str) -> frozenset[str]:
    """Identify capabilities needed based on command content.

    Uses pre-compiled word-boundary patterns to avoid false positives —
    e.g. 'curl' should not match inside 'uncurl' (S-8).
    """
    caps: set[str] = set()
    cmd_lower = command.lower().strip()
    for regex, cap in _COMPILED_PATTERNS:
        if regex.search(cmd_lower):
            caps.add(cap)
    for pattern, cap in _MULTI_WORD_PATTERNS:
        if pattern in cmd_lower:
            caps.add(cap)
    return frozenset(caps)


def _safe_env() -> dict[str, str]:
    """Create a sanitized environment, stripping dangerous variables."""
    env = dict(os.environ)
    for var in _STRIPPED_ENV_VARS:
        env.pop(var, None)
    return env


def _wrap_sandboxed(
    argv: list[str], ctx: ToolContext, cwd: Path
) -> tuple[list[str], bool, str | None]:
    """Wrap argv in the platform sandbox if one is usable.

    Returns ``(argv, wrapped, warning)``. Never raises.
    """
    if not ctx.writable_roots:
        return argv, False, None
    try:
        from kalash.sandbox.manager import get_sandbox_manager

        manager = get_sandbox_manager(
            workspace_root=ctx.cwd,
            writable_roots=tuple({*ctx.writable_roots, cwd}),
            allow_network=ctx.allow_network,
        )
        argv, wrapped = manager.wrap(argv, cwd=str(cwd))
        if wrapped:
            return argv, True, None
        status = manager.status()
        if status.available:
            return argv, False, "sandbox preflight failed — command runs unwrapped"
        return argv, False, f"OS sandbox unavailable ({status.detail}) — command runs unwrapped"
    except Exception:
        return argv, False, "sandbox backend error — command runs unwrapped"


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

        if ctx.writable_roots and not _cwd_allowed(cwd, ctx):
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"Working directory outside allowed roots: {cwd}",
                recoverable=True,
                remediation="Use a path inside the workspace.",
            )

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
        sandboxed = False
        sandbox_warning: str | None = None

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
                # Wrap in the platform sandbox when one is available. This is
                # defence in depth behind the permission gate, not a substitute
                # for it: an unavailable backend returns the argv unchanged and
                # the degradation shows up in `kalash doctor` rather than being
                # silently assumed.
                argv, wrapped, sandbox_warning = _wrap_sandboxed(
                    [shell, "-c", args.command], ctx, cwd
                )
                sandboxed = wrapped
                process = await asyncio.create_subprocess_exec(*argv, **kwargs)

            # Background mode: return immediately
            if args.background:
                meta: dict[str, Any] = {
                    "pid": process.pid,
                    "command": args.command,
                    "background": True,
                }
                if sandbox_warning:
                    meta["sandbox_warning"] = sandbox_warning
                if process.pid is not None:
                    _BACKGROUND.setdefault(ctx.session_id, {})[process.pid] = process
                return ToolEnvelope.success(
                    content=(
                        f"Background process started (PID: {process.pid}). "
                        "Use `shell` foreground commands to check output; "
                        "background jobs stop when the session is interrupted."
                    ),
                    metadata=meta,
                    side_effects=(
                        SideEffectRecord(kind="executed", path=args.command),
                    ),
                )

            # Wait with timeout — stream output live to the TUI / headless sink
            try:
                stdout_data, stderr_data, stdout_truncated, stderr_truncated = (
                    await _run_with_timeout(process, ctx=ctx, timeout=args.timeout)
                )
            except _ShellCancelled:
                return ToolEnvelope.fail(
                    code="KALASH_CANCELLED",
                    message=f"Command cancelled: {args.command}",
                    recoverable=True,
                )
            except asyncio.TimeoutError:
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

        # Cap output (live stream already truncated at source)
        stdout_str = stdout_data.decode("utf-8", errors="replace")
        stderr_str = stderr_data.decode("utf-8", errors="replace")

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

        metadata: dict[str, Any] = {
            "exit_code": exit_code,
            "command": args.command,
            "cwd": str(cwd),
            "stdout_bytes": len(stdout_data),
            "stderr_bytes": len(stderr_data),
            "sandboxed": sandboxed,
        }
        if sandbox_warning:
            metadata["sandbox_warning"] = sandbox_warning

        return ToolEnvelope(
            ok=exit_code == 0,
            content=content,
            metadata=metadata,
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


class _ShellCancelled(Exception):
    """Raised when a shell command is aborted via cancel_event."""


def _cwd_allowed(cwd: Path, ctx: ToolContext) -> bool:
    """True when cwd is under one of the writable roots."""
    resolved_cwd = cwd.resolve()
    for root in ctx.writable_roots:
        try:
            # Resolve roots once per check rather than inside a loop
            # that calls this on every command execution (S-9).
            resolved_cwd.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


async def _emit_stream_chunk(ctx: ToolContext, stream: str, chunk: str) -> None:
    if not chunk or ctx.event_bus is None:
        return
    from kalash.core.events import Event, EventType

    await ctx.event_bus.emit(
        Event(
            type=EventType.TOOL_OUTPUT,
            session_id=ctx.session_id,
            data={
                "tool_use_id": ctx.tool_use_id,
                "stream": stream,
                "chunk": chunk,
            },
        )
    )


async def _collect_stream(
    reader: asyncio.StreamReader | None,
    *,
    stream: str,
    ctx: ToolContext,
    limit: int,
) -> tuple[bytes, bool]:
    """Read a subprocess stream, emitting live chunks to the event bus."""
    if reader is None:
        return b"", False
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        if ctx.cancel_event is not None and ctx.cancel_event.is_set():
            truncated = True
            break
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=0.25)
        except asyncio.TimeoutError:
            continue
        if not data:
            break
        total += len(data)
        if total <= limit:
            chunks.append(data)
            await _emit_stream_chunk(
                ctx, stream, data.decode("utf-8", errors="replace")
            )
        else:
            truncated = True
    return b"".join(chunks), truncated


async def _run_with_timeout(
    process: asyncio.subprocess.Process,
    *,
    ctx: ToolContext,
    timeout: float,
) -> tuple[bytes, bytes, bool, bool]:
    """Wait for a process, streaming stdout/stderr as they arrive."""
    stdout_task = asyncio.create_task(
        _collect_stream(process.stdout, stream="stdout", ctx=ctx, limit=_MAX_OUTPUT_BYTES)
    )
    stderr_task = asyncio.create_task(
        _collect_stream(process.stderr, stream="stderr", ctx=ctx, limit=_MAX_OUTPUT_BYTES)
    )
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while process.returncode is None:
            if ctx.cancel_event is not None and ctx.cancel_event.is_set():
                await _terminate_process(process)
                raise _ShellCancelled()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await _terminate_process(process)
                raise asyncio.TimeoutError()
            try:
                await asyncio.wait_for(process.wait(), timeout=min(0.2, remaining))
            except asyncio.TimeoutError:
                continue
    except (asyncio.TimeoutError, _ShellCancelled):
        stdout_task.cancel()
        stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stdout_task
            await stderr_task
        raise
    stdout_data, stdout_trunc = await stdout_task
    stderr_data, stderr_trunc = await stderr_task
    return stdout_data, stderr_data, stdout_trunc, stderr_trunc


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Graceful termination: SIGTERM -> 5s -> SIGKILL."""
    if process.returncode is not None:
        return

    try:
        if sys.platform != "win32":
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGTERM)
        else:
            process.terminate()
    except (ProcessLookupError, OSError):
        return

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
