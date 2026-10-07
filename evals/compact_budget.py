"""One durable conservative allowance across every compact-suite API attempt.

Before dispatch reserve UTF-8 wire bytes + 4096 framing allowance + max output.
Successful provider usage replaces that reservation. Failed/missing-usage attempts
retain it, including cancellation. This is deliberately much larger than the
runtime's character estimate. A server exceeding the reservation stops the suite.
No exact server tokenizer is exposed, so this is not a mathematical billing cap.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, TextIO

from kalash.core.errors import BudgetError
from kalash.core.privacy import scrub_metadata
from kalash.models.normalize import BlockDelta, MessageStop, StopReason, StreamError, UsageUpdate

if TYPE_CHECKING:
    from pathlib import Path


class SuiteBudgetExceededError(BudgetError):
    """Another request cannot fit without exceeding the suite allowance."""


@dataclass
class SuiteLedger:
    path: Path
    limit: int = 240_000
    attempts: list[dict[str, Any]] = field(default_factory=list)
    violated: bool = False
    refusals: list[dict[str, Any]] = field(default_factory=list)
    task_limit: int = 52_000

    def __post_init__(self) -> None:
        if not 0 < self.limit < 250_000:
            raise ValueError("Suite allowance must remain below 250000 tokens")
        if self.task_limit <= 0:
            raise ValueError("Task allowance must be positive")

    @property
    def charged(self) -> int:
        return sum(int(item["charged_tokens"]) for item in self.attempts)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.charged)

    def save(self) -> None:
        payload = {
            "limit": self.limit,
            "charged_tokens": self.charged,
            "violated": self.violated,
            "attempts": self.attempts,
            "refusals": self.refusals,
            "task_limit": self.task_limit,
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(scrub_metadata(payload), indent=2) + "\n")
        temporary.replace(self.path)

    def reserve(self, task: str, wire_bytes: int, max_output: int) -> int:
        reserved = wire_bytes + 4096 + max_output
        task_spend = sum(
            int(item["charged_tokens"]) for item in self.attempts if item["task"] == task
        )
        dimensions = []
        if self.violated:
            dimensions.append("reservation_overrun")
        if reserved > self.remaining:
            dimensions.append("suite_tokens")
        if task_spend + reserved > self.task_limit:
            dimensions.append("task_tokens")
        if dimensions:
            self.refusals.append(
                {
                    "task": task,
                    "dimensions": dimensions,
                    "wire_bytes": wire_bytes,
                    "framing_tokens": 4096,
                    "output_tokens": max_output,
                    "reserved_tokens": reserved,
                    "suite_remaining_tokens": self.remaining,
                    "task_remaining_tokens": max(0, self.task_limit - task_spend),
                }
            )
            self.save()  # a refusal records evidence but never debits another call
            raise SuiteBudgetExceededError(
                f"Controller token reservation refused ({', '.join(dimensions)}): "
                f"required={reserved} (wire_bytes={wire_bytes}, framing_tokens=4096, "
                f"output_tokens={max_output}); suite_remaining={self.remaining}, "
                f"task_remaining={max(0, self.task_limit - task_spend)}. "
                "Narrow the request/context or explicitly choose a smaller output allowance."
            )
        self.attempts.append(
            {
                "task": task,
                "reserved_tokens": reserved,
                "charged_tokens": reserved,
                "status": "reserved",
                "usage": None,
            }
        )
        self.save()  # durable debit precedes any external request
        return len(self.attempts) - 1

    def settle(self, index: int, usage: UsageUpdate | None, status: str) -> None:
        item = self.attempts[index]
        item["status"] = status
        if usage is not None:
            counters = (usage.input_tokens, usage.output_tokens, usage.cache_write_tokens)
            if any(value < 0 for value in counters):
                raise ValueError("Negative provider usage")
            actual = sum(counters)
            item["usage"] = asdict(usage)
            if actual > item["reserved_tokens"]:
                self.violated = True
            # A cancelled/failed stream may report usage before generation ends.
            # Preserve its reservation as well as all observed counters.
            item["charged_tokens"] = (
                actual if status == "complete" else max(item["reserved_tokens"], actual)
            )
        self.save()
        if self.violated:
            raise SuiteBudgetExceededError(
                "Provider usage exceeded the conservative reservation; stopping"
            )


class MeteredProvider:
    """Wrap every attempt, including gateway retries and compaction calls."""

    def __init__(
        self,
        provider: Any,
        ledger: SuiteLedger,
        task: str,
        *,
        trace: TextIO | None = None,
        credential: str = "",
    ) -> None:
        self.provider, self.ledger, self.task = provider, ledger, task
        self.trace, self.credential = trace, credential

    def _record(self, kind: str, index: int, data: Any) -> None:
        if self.trace is None:
            return
        payload = json.dumps(
            scrub_metadata({"kind": kind, "attempt": index, "data": data}),
            ensure_ascii=False,
            default=str,
        )
        if self.credential:
            payload = payload.replace(self.credential, "[credential]")
        self.trace.write(payload + "\n")
        self.trace.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.provider, name)

    async def stream(self, messages: Any, **kwargs: Any) -> Any:
        wire = {
            "messages": self.provider.serialize_messages(messages, kwargs.get("system")),
            "tools": kwargs.get("tools") or [],
            "model": self.provider.name,
        }
        size = len(json.dumps(wire, ensure_ascii=False).encode("utf-8"))
        maximum = int(kwargs.get("max_tokens") or self.provider.default_output_tokens)
        index = self.ledger.reserve(self.task, size, maximum)
        usage = None
        status = "incomplete"
        finished = False
        failed = False
        stream = self.provider.stream(messages, **kwargs)
        try:
            self._record("request", index, {**wire, "max_output_tokens": maximum})
            async for event in stream:
                if isinstance(event, UsageUpdate):
                    usage = (usage or UsageUpdate()).merge(event)
                elif isinstance(event, MessageStop):
                    finished = event.stop_reason != StopReason.UNKNOWN
                elif isinstance(event, StreamError):
                    failed = True
                if self.trace is not None:
                    data = asdict(event)
                    if isinstance(event, BlockDelta):
                        # Raw lexical fragments could split a credential across
                        # records. Retain sizes/types, not fragment bodies.
                        data["delta_chars"] = len(data.pop("delta"))
                    self._record("stream", index, data)
                yield event
            if finished and not failed:
                status = "complete"
        finally:
            try:
                await stream.aclose()
            finally:
                self.ledger.settle(index, usage, status)
