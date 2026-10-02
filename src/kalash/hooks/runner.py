"""Hook dispatch engine.

Handler types: command (JSON on stdin), http (JSON POST), python (in-process).
Exit-code semantics: 0=proceed, 2=block, other=non-blocking error.
Loop protection: track depth per event chain, refuse re-entry beyond depth 1.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from kalash.core.errors import HookDeniedError
from kalash.core.ids import generate_id
from kalash.hooks.events import HookEvent, HookPayload
from kalash.storage.engine import StorageEngine


class HandlerType(StrEnum):
    """Types of hook handlers."""

    COMMAND = "command"  # shell command, JSON on stdin
    HTTP = "http"  # JSON POST to URL
    PYTHON = "python"  # in-process callable


class HookExitCode(StrEnum):
    """Semantics of exit codes from hook handlers."""

    PROCEED = "0"  # Allow the action to proceed
    BLOCK = "2"  # Block the action (PreToolUse, UserPromptSubmit, etc.)
    # Any other code = non-blocking error


@dataclass
class HookConfig:
    """Configuration for a single hook handler."""

    id: str = ""
    name: str = ""
    event: HookEvent = HookEvent.SESSION_START
    handler_type: HandlerType = HandlerType.COMMAND
    matcher: str | None = None  # Regex for filtering (e.g. tool name)
    timeout_s: float = 30.0
    enabled: bool = True

    # Handler-specific config
    command: str = ""  # For COMMAND type
    url: str = ""  # For HTTP type
    source_path: str = ""
    python_callable: str = ""  # For PYTHON type (dotted path)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = generate_id("hook_")


@dataclass
class HookResult:
    """Result from executing a hook handler."""

    hook_id: str
    event: HookEvent
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    blocked: bool = False
    error: str | None = None


class HookRunner:
    """Dispatches hook events to registered handlers.

    Enforces loop protection: tracks re-entry depth per event chain
    and refuses to fire hooks that would exceed depth 1.
    """

    MAX_CHAIN_DEPTH = 1

    def __init__(self, engine: StorageEngine) -> None:
        self._engine = engine
        self._hooks: dict[HookEvent, list[HookConfig]] = {}
        self._chain_depth: dict[str, int] = {}  # event_chain_id → depth
        self._python_handlers: dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {}

    def register(self, config: HookConfig) -> None:
        """Register a hook handler."""
        if config.event not in self._hooks:
            self._hooks[config.event] = []
        self._hooks[config.event].append(config)

    def register_python(
        self,
        name: str,
        event: HookEvent,
        handler: Callable[[HookPayload], Coroutine[Any, Any, Any]],
        *,
        matcher: str | None = None,
    ) -> None:
        """Register an in-process Python hook handler."""
        handler_id = generate_id("hook_")
        self._python_handlers[handler_id] = handler
        config = HookConfig(
            id=handler_id,
            name=name,
            event=event,
            handler_type=HandlerType.PYTHON,
            matcher=matcher,
            python_callable=handler_id,
        )
        self.register(config)

    async def dispatch(
        self,
        payload: HookPayload,
        *,
        chain_id: str | None = None,
    ) -> list[HookResult]:
        """Dispatch an event to all matching hooks.

        Returns results from all handlers. If any handler returns
        exit code 2 (block), raises HookDeniedError.
        """
        event = payload.event
        hooks = self._hooks.get(event, [])
        if not hooks:
            return []

        # Loop protection: check chain depth
        effective_chain = chain_id or generate_id("chain_")
        current_depth = self._chain_depth.get(effective_chain, 0)
        if current_depth > self.MAX_CHAIN_DEPTH:
            import logging as _log

            _log.getLogger(__name__).warning(
                "hook_loop_protection: event=%s chain_id=%s depth=%d",
                event,
                effective_chain,
                current_depth,
            )
            return []

        self._chain_depth[effective_chain] = current_depth + 1
        try:
            results: list[HookResult] = []
            for hook_config in hooks:
                if not hook_config.enabled:
                    continue
                if not self._matches(hook_config, payload):
                    continue

                result = await self._execute_hook(hook_config, payload)
                results.append(result)

                # Record in hook_runs table
                await self._record_result(result, payload)

                # Check for block
                if result.exit_code == 2:
                    result.blocked = True
                    raise HookDeniedError(
                        f"Hook '{hook_config.name}' blocked {event}: {result.stderr or result.stdout}",
                        recoverable=True,
                    )

            return results
        finally:
            if current_depth:
                self._chain_depth[effective_chain] = current_depth
            else:
                self._chain_depth.pop(effective_chain, None)

    async def _execute_hook(
        self,
        config: HookConfig,
        payload: HookPayload,
    ) -> HookResult:
        """Execute a single hook handler based on its type."""
        start = time.time()
        if config.source_path:
            from pathlib import Path

            from kalash.core.trust import is_trusted

            if not is_trusted(Path(config.source_path)):
                raise HookDeniedError(
                    "Project hook configuration is not trusted or changed", recoverable=True
                )
        try:
            if config.handler_type == HandlerType.COMMAND:
                return await self._execute_command(config, payload, start)
            if config.handler_type == HandlerType.HTTP:
                return await self._execute_http(config, payload, start)
            if config.handler_type == HandlerType.PYTHON:
                return await self._execute_python(config, payload, start)
            return HookResult(
                hook_id=config.id,
                event=payload.event,
                exit_code=1,
                error=f"Unknown handler type: {config.handler_type}",
                duration_ms=int((time.time() - start) * 1000),
            )
        except TimeoutError:
            return HookResult(
                hook_id=config.id,
                event=payload.event,
                exit_code=1,
                error=f"Hook timed out after {config.timeout_s}s",
                duration_ms=int((time.time() - start) * 1000),
            )
        except HookDeniedError:
            raise  # propagate block signals
        except Exception as e:
            return HookResult(
                hook_id=config.id,
                event=payload.event,
                exit_code=1,
                error=str(e),
                duration_ms=int((time.time() - start) * 1000),
            )

    async def _execute_command(
        self,
        config: HookConfig,
        payload: HookPayload,
        start: float,
    ) -> HookResult:
        """Execute a command hook: JSON payload on stdin, exit code semantics."""
        stdin_data = json.dumps(
            {
                "event": payload.event.value,
                "session_id": payload.session_id,
                "run_id": payload.run_id,
                "timestamp": payload.timestamp,
                "data": payload.data,
            }
        ).encode()

        from kalash.sandbox.environment import safe_environment

        # Command comes from the user's hook config (trusted), not from the
        # payload. Pass it as a single `-c` argument so runtime data never
        # undergoes shell interpolation.
        if sys.platform == "win32":
            proc = await asyncio.create_subprocess_exec(
                "cmd.exe",
                "/c",
                config.command,
                env=safe_environment(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                "/bin/sh",
                "-c",
                config.command,
                env=safe_environment(),
                start_new_session=True,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        async def drain(reader: asyncio.StreamReader | None) -> bytes:
            retained = bytearray()
            if reader is not None:
                while chunk := await reader.read(8192):
                    retained.extend(chunk[: max(0, 32_000 - len(retained))])
            return bytes(retained)

        async def communicate() -> tuple[bytes, bytes]:
            readers = [
                asyncio.create_task(drain(proc.stdout)),
                asyncio.create_task(drain(proc.stderr)),
            ]
            try:
                if proc.stdin is not None:
                    try:
                        proc.stdin.write(stdin_data)
                        await proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    finally:
                        proc.stdin.close()
                stdout, stderr = await asyncio.gather(*readers)
                await proc.wait()
                return stdout, stderr
            finally:
                for reader in readers:
                    reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)

        try:
            stdout, stderr = await asyncio.wait_for(
                communicate(),
                timeout=config.timeout_s,
            )
        except (TimeoutError, asyncio.CancelledError):
            from kalash.tools.shell import _terminate_process

            await _terminate_process(proc)
            raise

        return HookResult(
            hook_id=config.id,
            event=payload.event,
            exit_code=proc.returncode or 0,
            stdout=stdout[:32_000].decode(errors="replace"),
            stderr=stderr[:32_000].decode(errors="replace"),
            duration_ms=int((time.time() - start) * 1000),
        )

    async def _execute_http(
        self,
        config: HookConfig,
        payload: HookPayload,
        start: float,
    ) -> HookResult:
        """Execute an HTTP hook: JSON POST to configured URL."""
        import urllib.error
        import urllib.request

        body = json.dumps(
            {
                "event": payload.event.value,
                "session_id": payload.session_id,
                "run_id": payload.run_id,
                "timestamp": payload.timestamp,
                "data": payload.data,
            }
        ).encode()

        req = urllib.request.Request(
            config.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:

            def fetch() -> tuple[str, int]:
                with urllib.request.urlopen(req, timeout=config.timeout_s) as response:
                    return response.read(32_000).decode(
                        errors="replace"
                    ), 0 if response.status == 200 else 1

            response_body, exit_code = await asyncio.to_thread(fetch)
        except urllib.error.HTTPError as e:
            response_body = e.read(32_000).decode(errors="replace") if e.fp else ""
            exit_code = 2 if e.code == 403 else 1
        except (TimeoutError, urllib.error.URLError, OSError) as e:
            # R-1: DNS failure, connection refused, or timeout.
            response_body = ""
            exit_code = 1
            return HookResult(
                hook_id=config.id,
                event=payload.event,
                exit_code=exit_code,
                error=f"HTTP hook unreachable: {e}",
                duration_ms=int((time.time() - start) * 1000),
            )

        return HookResult(
            hook_id=config.id,
            event=payload.event,
            exit_code=exit_code,
            stdout=response_body,
            duration_ms=int((time.time() - start) * 1000),
        )

    async def _execute_python(
        self,
        config: HookConfig,
        payload: HookPayload,
        start: float,
    ) -> HookResult:
        """Execute an in-process Python hook handler."""
        handler = self._python_handlers.get(config.python_callable)
        if not handler:
            return HookResult(
                hook_id=config.id,
                event=payload.event,
                exit_code=1,
                error=f"Python handler not found: {config.python_callable}",
                duration_ms=int((time.time() - start) * 1000),
            )

        result = await handler(payload)

        # Interpret result: if it returns an int, use as exit code
        exit_code = 0
        stdout = ""
        if isinstance(result, int):
            exit_code = result
        elif isinstance(result, str):
            stdout = result
        elif isinstance(result, dict):
            exit_code = result.get("exit_code", 0)
            stdout = result.get("output", "")

        return HookResult(
            hook_id=config.id,
            event=payload.event,
            exit_code=exit_code,
            stdout=stdout,
            duration_ms=int((time.time() - start) * 1000),
        )

    def _matches(self, config: HookConfig, payload: HookPayload) -> bool:
        """Check if a hook config's matcher applies to this payload."""
        if not config.matcher:
            return True

        import re

        # Match against relevant payload data
        match_target = ""
        if payload.event in (HookEvent.PRE_TOOL_USE, HookEvent.POST_TOOL_USE):
            match_target = payload.data.get("tool_name", "")
        elif payload.event in (HookEvent.PRE_FILE_EDIT, HookEvent.POST_FILE_EDIT):
            match_target = payload.data.get("path", "")
        else:
            # For other events, match against stringified data
            match_target = json.dumps(payload.data)

        try:
            return bool(re.search(config.matcher, match_target))
        except re.error:
            return False

    async def _record_result(self, result: HookResult, payload: HookPayload) -> None:
        """Record hook execution in hook_runs table."""
        now = datetime.now(UTC).isoformat()
        await self._engine.execute_write(
            """INSERT INTO hook_runs
               (id, hook_id, event, exit_code, stdout, stderr, error,
                duration_ms, blocked, session_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                generate_id("hr_"),
                result.hook_id,
                result.event.value,
                result.exit_code,
                result.stdout[:4096] if result.stdout else None,  # cap stored output
                result.stderr[:4096] if result.stderr else None,
                result.error,
                result.duration_ms,
                result.blocked,
                payload.session_id,
                now,
            ),
        )
