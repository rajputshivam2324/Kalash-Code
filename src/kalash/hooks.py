"""User-defined command hooks (from user or local settings only; never from the repo).

A hook is a shell command that receives a JSON event on stdin:

* exit 0       - success (for ``UserPromptSubmit`` the stdout is added as context)
* exit 2       - block: ``PreToolUse`` refuses the call, ``UserPromptSubmit`` rejects the
                 prompt, ``Stop`` sends stderr back to the model as feedback
* anything else - non-blocking failure, reported as a warning

A ``PreToolUse`` hook that times out or cannot start blocks the call: it was configured as a
guard, so failing open would silently remove the guard.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kalash.config import Hooks, HookSpec
from kalash.sandbox import clean_env


@dataclass
class HookOutcome:
    blocked: bool = False
    message: str = ""
    context: str = ""
    warnings: list[str] = field(default_factory=list)


class HookRunner:
    def __init__(self, hooks: Hooks, cwd: Path, session_id: str) -> None:
        self.hooks = hooks
        self.cwd = cwd
        self.session_id = session_id

    def has_any(self) -> bool:
        return any(
            getattr(self.hooks, name)
            for name in ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop")
        )

    async def run(
        self, event: str, payload: dict[str, Any], *, match: str | None = None
    ) -> HookOutcome:
        specs: list[HookSpec] = getattr(self.hooks, event)
        outcome = HookOutcome()
        body = json.dumps(
            {"event": event, "session_id": self.session_id, "cwd": str(self.cwd), **payload}
        ).encode()
        for spec in specs:
            if spec.matcher and match is not None and not re.search(spec.matcher, match):
                continue
            blocked, text, out = await self._one(spec, body, guard=event == "PreToolUse")
            if blocked:
                outcome.blocked = True
                outcome.message = text
                break
            if text:
                outcome.warnings.append(text)
            if out:
                outcome.context += out
        return outcome

    async def _one(self, spec: HookSpec, body: bytes, *, guard: bool) -> tuple[bool, str, str]:
        label = f"hook `{spec.command}`"
        try:
            process = await asyncio.create_subprocess_shell(
                spec.command,
                cwd=self.cwd,
                env=clean_env(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        except OSError as error:
            return guard, f"{label} could not start: {error}", ""
        communication = asyncio.create_task(process.communicate(body))
        try:
            out, err = await asyncio.wait_for(asyncio.shield(communication), spec.timeout)
        except TimeoutError:
            await self._stop(process, communication)
            return guard, f"{label} timed out after {spec.timeout:.0f}s", ""
        except asyncio.CancelledError:
            await self._stop(process, communication)
            raise
        text = err.decode("utf-8", "replace").strip()
        if process.returncode == 0:
            return False, "", out.decode("utf-8", "replace")
        if process.returncode == 2:
            return True, text or f"{label} blocked this action", ""
        return False, f"{label} exited {process.returncode}: {text[:300]}", ""

    @staticmethod
    async def _stop(
        process: asyncio.subprocess.Process, communication: asyncio.Task[tuple[bytes, bytes]]
    ) -> None:
        # A shell's descendants inherit its pipes. Killing only the shell leaves
        # those pipes open and can hang wait() beyond the hook's timeout.
        with contextlib.suppress(ProcessLookupError):
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            elif process.returncode is None:
                process.kill()
        # Keep communicate alive through timeout/cancellation so it drains and
        # closes every pipe and reaps the shell before the hook returns.
        await communication
