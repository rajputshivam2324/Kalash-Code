"""Structured output for headless ``kalash -p``.

See docs/CLI_OUTPUT.md: stdout is data, stderr is
diagnostics. ``text`` streams plain assistant text; ``json`` emits one object on
completion; ``stream-json`` emits NDJSON events with a terminal result/error.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TextIO

from kalash.core.public_events import public_event

SCHEMA_VERSION = "1.0"


class OutputFormat(StrEnum):
    TEXT = "text"
    JSON = "json"
    STREAM_JSON = "stream-json"


# Public headless exit codes.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_APPROVAL_REQUIRED = 3
EXIT_BUDGET_EXCEEDED = 4
EXIT_CANCELLED = 5
EXIT_PROVIDER_UNAVAILABLE = 10


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class RunMetrics:
    """Collected during a headless run for structured output."""

    session_id: str = ""
    model_id: str = ""
    sandbox: str = ""
    started_at: str = field(default_factory=_now_iso)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    usage_unknown: bool = False
    usage_source: str = "provider_reported"
    max_tokens: int | None = None
    max_turns: int | None = None
    budget_tokens_used: int | None = None
    turns_used: int = 0
    completion_status: str = "partial"
    used_model: str = ""
    run_id: str = ""
    fallback_chain: list[str] = field(default_factory=list)
    cost_usd: str | None = None
    files_changed: list[dict[str, Any]] = field(default_factory=list)
    verification: list[dict[str, Any]] = field(default_factory=list)
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
        self._streamed_text = False

    def on_text_delta(self, delta: str) -> None:
        if self.format is OutputFormat.TEXT:
            self._streamed_text = True
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
            "run_id": self.metrics.run_id or None,
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

    def emit_runtime_tool_event(self, event: str, data: dict[str, Any]) -> None:
        """Preserve runtime identities, bounded chunks and error evidence."""
        self._emit(event, data)

    def emit_runtime_event(self, event: Any) -> None:
        if self.format is not OutputFormat.STREAM_JSON:
            return
        envelope = public_event(event)
        envelope["runtime_seq"] = envelope["seq"]
        envelope["seq"] = self._seq
        self._seq += 1
        self.stdout.write(json.dumps(envelope, separators=(",", ":")) + "\n")
        self.stdout.flush()

    def _usage(self) -> dict[str, Any]:
        return {
            "input_tokens": self.metrics.input_tokens,
            "output_tokens": self.metrics.output_tokens,
            "cache_read_tokens": self.metrics.cache_read_tokens,
            "cache_write_tokens": self.metrics.cache_write_tokens,
            "reasoning_tokens": self.metrics.reasoning_tokens,
            "source": self.metrics.usage_source,
            "usage_unknown": self.metrics.usage_unknown,
            "cost_usd": self.metrics.cost_usd,
        }

    def _budget(self, state: str) -> dict[str, Any]:
        return {
            "turns": {"used": self.metrics.turns_used, "ceiling": self.metrics.max_turns},
            "tokens": {"used": self.metrics.budget_tokens_used, "ceiling": self.metrics.max_tokens},
            "state": state,
        }

    def _model(self) -> dict[str, Any]:
        return {
            "requested": self.metrics.model_id,
            "used": self.metrics.used_model or self.metrics.model_id,
            "fallback_chain": self.metrics.fallback_chain,
        }

    def finish_success(
        self,
        *,
        result_text: str,
        stop_reason: str,
        iterations: int,
        total_tokens: int,
        completion_status: str = "stopped_unverified",
        usage_unknown: bool = False,
    ) -> int:
        finished = _now_iso()
        if self.metrics.budget_tokens_used is None:
            self.metrics.budget_tokens_used = total_tokens
        text = result_text or "".join(self._text_buffer)

        if self.format is OutputFormat.JSON:
            body = {
                "schema_version": SCHEMA_VERSION,
                "ok": True,
                "completion_status": completion_status,
                "usage_unknown": usage_unknown,
                "session_id": self.metrics.session_id,
                "run_id": self.metrics.run_id or None,
                "result": text,
                "stop_reason": stop_reason,
                "model": self._model(),
                "usage": self._usage(),
                "budget": self._budget("ok"),
                "files_changed": self.metrics.files_changed,
                "verification": self.metrics.verification,
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
                    "completion_status": completion_status,
                    "verification": self.metrics.verification,
                    "files_changed": self.metrics.files_changed,
                    "model": self._model(),
                    "usage_unknown": usage_unknown,
                    "stop_reason": stop_reason,
                    "usage": self._usage(),
                    "budget": self._budget("ok"),
                    "turns": iterations,
                },
            )
            return EXIT_OK

        # text mode: ensure trailing newline if we streamed
        if text:
            self.stdout.write("\n" if self._streamed_text else text + "\n")
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
                "run_id": self.metrics.run_id or None,
                "model": self._model(),
                "files_changed": self.metrics.files_changed,
                "verification": self.metrics.verification,
                "error": {
                    "code": code,
                    "class": error_class,
                    "message": message,
                    "retryable": False,
                },
                "partial_result": partial_text,
                "completion_status": self.metrics.completion_status,
                "usage_unknown": self.metrics.usage_unknown,
                "usage": self._usage(),
                "budget": self._budget("stopped"),
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
                    "completion_status": self.metrics.completion_status,
                    "model": self._model(),
                    "files_changed": self.metrics.files_changed,
                    "verification": self.metrics.verification,
                    "usage_unknown": self.metrics.usage_unknown,
                    "usage": self._usage(),
                    "budget": self._budget("stopped"),
                },
            )
            return exit_code

        return exit_code
