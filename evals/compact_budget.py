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
from pathlib import Path
from typing import Any

from kalash.core.privacy import scrub_metadata
from kalash.models.normalize import UsageUpdate


class SuiteBudgetExceeded(RuntimeError):
    """Another request cannot fit without exceeding the suite allowance."""


@dataclass
class SuiteLedger:
    path: Path
    limit: int = 240_000
    attempts: list[dict[str, Any]] = field(default_factory=list)
    violated: bool = False

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
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(scrub_metadata(payload), indent=2) + "\n")
        temporary.replace(self.path)

    def reserve(self, task: str, wire_bytes: int, max_output: int) -> int:
        reserved = wire_bytes + 4096 + max_output
        task_spend = sum(
            int(item["charged_tokens"]) for item in self.attempts if item["task"] == task
        )
        if self.violated or reserved > self.remaining or task_spend + reserved > 52_000:
            raise SuiteBudgetExceeded("Compact-suite token allowance cannot fit the next request")
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
            item["charged_tokens"] = actual
        self.save()
        if self.violated:
            raise SuiteBudgetExceeded(
                "Provider usage exceeded the conservative reservation; stopping"
            )


class MeteredProvider:
    """Wrap every attempt, including gateway retries and compaction calls."""

    def __init__(self, provider: Any, ledger: SuiteLedger, task: str) -> None:
        self.provider, self.ledger, self.task = provider, ledger, task

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
        stream = self.provider.stream(messages, **kwargs)
        try:
            async for event in stream:
                if isinstance(event, UsageUpdate):
                    usage = event
                yield event
            status = "complete"
        finally:
            try:
                await stream.aclose()
            finally:
                self.ledger.settle(index, usage, status)
