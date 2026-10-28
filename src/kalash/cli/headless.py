"""Non-interactive runs: ``kalash -p "task"``. Output formats: text, json, stream-json.

Exit codes:  0 completed | 1 error | 2 usage/config | 3 completed but actions were denied |
4 limit reached | 5 stopped early (output limit / refusal) | 130 cancelled.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, TextIO

from kalash.agent.build import Runtime, create_runtime
from kalash.agent.events import (
    AgentEvent,
    Compacted,
    Notice,
    Retry,
    TextChunk,
    ToolDone,
    ToolRunning,
    TurnResult,
)
from kalash.config import ConfigError, Settings
from kalash.llm.http import Adapter
from kalash.llm.providers import ProviderError
from kalash.paths import project_root
from kalash.permissions import Answer, ApprovalRequest
from kalash.session.log import SessionError, SessionLog, find_session, list_sessions

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_DENIED, EXIT_LIMIT, EXIT_INCOMPLETE = 0, 1, 2, 3, 4, 5
EXIT_CANCELLED = 130

FORMATS = ("text", "json", "stream-json")


def exit_code(result: TurnResult) -> int:
    if result.reason == "cancelled":
        return EXIT_CANCELLED
    if result.reason == "error":
        return EXIT_ERROR
    if result.reason == "limit":
        return EXIT_LIMIT
    if result.reason in ("max_tokens", "refused", "stuck"):
        return EXIT_INCOMPLETE
    return EXIT_DENIED if result.denials else EXIT_OK


def result_payload(result: TurnResult, runtime: Runtime | None, code: int) -> dict[str, Any]:
    return {
        "type": "result",
        "ok": code == EXIT_OK,
        "exit_code": code,
        "reason": result.reason,
        "result": result.text,
        "error": result.error,
        "session_id": runtime.session.id if runtime else None,
        "model": str(runtime.ref) if runtime else None,
        "steps": result.steps,
        "tool_calls": result.tool_calls,
        "denials": result.denials,
        "usage": asdict(result.usage),
        "cost_usd": result.cost,
    }


class Reporter:
    """Writes progress to stderr (text) or every event to stdout (stream-json)."""

    def __init__(self, fmt: str, out: TextIO, err: TextIO) -> None:
        self.fmt, self.out, self.err = fmt, out, err

    def __call__(self, event: AgentEvent) -> None:
        if self.fmt == "stream-json":
            payload = _event_json(event)
            if payload is not None:
                print(json.dumps(payload), file=self.out, flush=True)
            return
        if isinstance(event, ToolRunning):
            print(f"  > {event.summary}", file=self.err, flush=True)
        elif isinstance(event, ToolDone) and (event.denied or event.is_error):
            first = event.text.strip().splitlines()[0] if event.text.strip() else ""
            label = "denied" if event.denied else "error"
            print(f"    ({label}) {first[:160]}", file=self.err, flush=True)
        elif isinstance(event, Notice):
            print(f"[{event.level}] {event.text}", file=self.err, flush=True)
        elif isinstance(event, Retry):
            print(
                f"[retry {event.attempt} in {event.delay:.1f}s] {event.error}",
                file=self.err,
                flush=True,
            )
        elif isinstance(event, Compacted):
            print(
                f"[compacted {event.tokens_before} -> {event.tokens_after} tokens]",
                file=self.err,
                flush=True,
            )


def _event_json(event: AgentEvent) -> dict[str, Any] | None:
    if isinstance(event, TextChunk):
        return {"type": "text", "text": event.text}
    if isinstance(event, ToolRunning):
        return {
            "type": "tool_start",
            "id": event.call_id,
            "name": event.name,
            "arguments": event.arguments,
        }
    if isinstance(event, ToolDone):
        return {
            "type": "tool_result",
            "id": event.call_id,
            "name": event.name,
            "is_error": event.is_error,
            "denied": event.denied,
            "text": event.text,
        }
    if isinstance(event, Notice):
        return {"type": "notice", "level": event.level, "text": event.text}
    if isinstance(event, Retry):
        return {
            "type": "retry",
            "attempt": event.attempt,
            "delay": event.delay,
            "error": event.error,
        }
    if isinstance(event, Compacted):
        return {"type": "compacted", "before": event.tokens_before, "after": event.tokens_after}
    return None


def stdin_approver(err: TextIO) -> Any:
    """Prompt on the terminal. Only used when stdin is an interactive TTY."""

    async def ask(request: ApprovalRequest) -> Answer:
        def prompt() -> str:
            print(f"\nApproval needed: {request.summary}\n  reason: {request.reason}", file=err)
            print(
                "  [y] allow once   [s] allow for this session   [n] deny",
                file=err,
                end=": ",
                flush=True,
            )
            return sys.stdin.readline().strip().lower()

        reply = await asyncio.to_thread(prompt)
        if reply in ("y", "yes", "o", "once"):
            return "once"
        if reply in ("s", "session"):
            return "session"
        return "deny"

    return ask


async def run_headless(
    *,
    prompt: str,
    cwd: Path,
    settings: Settings,
    fmt: str,
    model: str | None = None,
    session_ref: str | None = None,
    continue_latest: bool = False,
    out: TextIO | None = None,
    err: TextIO | None = None,
    adapter: Adapter | None = None,
    interactive_approval: bool = False,
    transport: object | None = None,
) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    reporter = Reporter(fmt, out, err)
    runtime: Runtime | None = None
    result: TurnResult | None = None
    try:
        session: SessionLog | None = None
        root = project_root(cwd)
        if session_ref:
            session = SessionLog.open(find_session(root, session_ref))
        elif continue_latest:
            sessions = list_sessions(root)
            if not sessions:
                raise SessionError("no previous session in this project to continue")
            session = SessionLog.open(sessions[0].path)
        approver = (
            stdin_approver(err)
            if interactive_approval and settings.permissions.approval == "ask"
            else None
        )
        runtime = await create_runtime(
            cwd=cwd,
            settings=settings,
            model=model,
            session=session,
            approver=approver,
            emit=reporter,
            adapter=adapter,
            transport=transport,
        )
    except (ConfigError, ProviderError, SessionError) as error:
        return _fail(fmt, out, err, str(error), EXIT_USAGE)
    try:
        result = await runtime.agent.run_turn(prompt)
    except (asyncio.CancelledError, KeyboardInterrupt):
        result = TurnResult("cancelled", error="interrupted")
    finally:
        await runtime.aclose()
    code = exit_code(result)
    payload = result_payload(result, runtime, code)
    if fmt == "json":
        print(json.dumps(payload, indent=2), file=out, flush=True)
    elif fmt == "stream-json":
        print(json.dumps(payload), file=out, flush=True)
    else:
        if result.text:
            print(result.text, file=out, flush=True)
        if result.error:
            print(f"kalash: {result.reason}: {result.error}", file=err, flush=True)
        for denial in result.denials:
            print(f"kalash: denied action - {denial}", file=err, flush=True)
    return code


def _fail(fmt: str, out: TextIO, err: TextIO, message: str, code: int) -> int:
    if fmt in ("json", "stream-json"):
        print(
            json.dumps(
                {
                    "type": "result",
                    "ok": False,
                    "exit_code": code,
                    "reason": "error",
                    "error": message,
                }
            ),
            file=out,
            flush=True,
        )
    else:
        print(f"kalash: {message}", file=err, flush=True)
    return code
