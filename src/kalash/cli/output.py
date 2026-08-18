"""Structured output for headless ``kalash -p``.

Implements the contract from docs/interfaces.md §4: stdout is data, stderr is
diagnostics. ``text`` streams plain assistant text; ``json`` emits one object on
completion; ``stream-json`` emits NDJSON events with a terminal result/error.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, TextIO

SCHEMA_VERSION = "1.0"


class OutputFormat(StrEnum):
    TEXT = "text"
    JSON = "json"
    STREAM_JSON = "stream-json"


# Exit codes — docs/interfaces.md §4.9
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_APPROVAL_REQUIRED = 3
EXIT_BUDGET_EXCEEDED = 4
EXIT_CANCELLED = 5
EXIT_PROVIDER_UNAVAILABLE = 10


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class RunMetrics:
    """Collected during a headless run for structured output."""

    session_id: str = ""
    model_id: str = ""
    sandbox: str = ""
    started_at: str = field(default_factory=_now_iso)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: str | None = None
    files_changed: list[dict[str, Any]] = field(default_factory=list)
    partial_text: str = ""


class OutputSink:
    """Routes agent output to stdout according to the selected format."""

    def __init__(
        self,
        fmt: OutputFormat,
        *,
        stdout: TextIO | None = None,
        metrics: RunMetrics | None = None,
    ) -> None:
        self.format = fmt
        self.stdout = stdout or sys.stdout
        self.metrics = metrics or RunMetrics()
        self._seq = 0
        self._text_buffer: list[str] = []

    def on_text_delta(self, delta: str) -> None:
        if self.format is OutputFormat.TEXT:
            self.stdout.write(delta)
            self.stdout.flush()
        elif self.format is OutputFormat.STREAM_JSON:
            self._text_buffer.append(delta)
            self._emit(
                "message.delta",
                {"block_index": 0, "text": delta},
            )
        else:
            self._text_buffer.append(delta)

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self.format is not OutputFormat.STREAM_JSON:
            return
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "seq": self._seq,
            "ts": _now_iso(),
            "event": event,
            "session_id": self.metrics.session_id,
            "data": data,
        }
        self._seq += 1
        self.stdout.write(json.dumps(envelope, separators=(",", ":")) + "\n")
        self.stdout.flush()

    def emit_session_started(self) -> None:
        if self.format is OutputFormat.STREAM_JSON:
            self._emit(
                "session.started",
                {
                    "model": self.metrics.model_id,
                    "sandbox": self.metrics.sandbox,
                },
            )

    def emit_tool_event(self, name: str, *, ok: bool, detail: str = "") -> None:
        if self.format is OutputFormat.STREAM_JSON:
            self._emit(
                "tool.completed",
                {
                    "name": name,
                    "ok": ok,
                    "detail": detail[:200],
                },
            )

    def finish_success(
        self,
        *,
        result_text: str,
        stop_reason: str,
        iterations: int,
        total_tokens: int,
    ) -> int:
        finished = _now_iso()
        text = result_text or "".join(self._text_buffer)

        if self.format is OutputFormat.JSON:
            body = {
                "schema_version": SCHEMA_VERSION,
                "ok": True,
                "session_id": self.metrics.session_id,
                "result": text,
                "stop_reason": stop_reason,
                "model": {
                    "requested": self.metrics.model_id,
                    "used": self.metrics.model_id,
                    "fallback_chain": [],
                },
                "usage": {
                    "input_tokens": self.metrics.input_tokens,
                    "output_tokens": self.metrics.output_tokens,
                    "source": "estimated",
                    "cost_usd": self.metrics.cost_usd,
                },
                "budget": {
                    "turns": {"used": iterations, "ceiling": 100},
                    "tokens": {"used": total_tokens, "ceiling": 500_000},
                    "state": "ok",
                },
                "files_changed": self.metrics.files_changed,
                "started_at": self.metrics.started_at,
                "finished_at": finished,
            }
            self.stdout.write(json.dumps(body) + "\n")
            self.stdout.flush()
            return EXIT_OK

        if self.format is OutputFormat.STREAM_JSON:
            self._emit(
                "result",
                {
                    "result": text,
                    "stop_reason": stop_reason,
                    "usage": {
                        "input_tokens": self.metrics.input_tokens,
                        "output_tokens": self.metrics.output_tokens,
                    },
                    "turns": iterations,
                },
            )
            return EXIT_OK

        # text mode: ensure trailing newline if we streamed
        if text and self.format is OutputFormat.TEXT:
            self.stdout.write("\n")
            self.stdout.flush()
        elif text and not self._text_buffer:
            self.stdout.write(text + "\n")
            self.stdout.flush()
        return EXIT_OK

    def finish_error(
        self,
        *,
        code: str,
        message: str,
        error_class: str = "internal",
        exit_code: int = EXIT_FAILURE,
        partial: str = "",
    ) -> int:
        finished = _now_iso()
        partial_text = partial or "".join(self._text_buffer)

        if self.format is OutputFormat.JSON:
            body = {
                "schema_version": SCHEMA_VERSION,
                "ok": False,
                "session_id": self.metrics.session_id,
                "error": {
                    "code": code,
                    "class": error_class,
                    "message": message,
                    "retryable": False,
                },
                "partial_result": partial_text,
                "finished_at": finished,
            }
            self.stdout.write(json.dumps(body) + "\n")
            self.stdout.flush()
            return exit_code

        if self.format is OutputFormat.STREAM_JSON:
            self._emit(
                "error",
                {
                    "code": code,
                    "message": message,
                    "partial_result": partial_text,
                },
            )
            return exit_code

        return exit_code
