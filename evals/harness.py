"""Evaluation harness.

Three suites, all deterministic and offline:

* **capability** — can the agent complete a task? Scored on the *filesystem*,
  not on what the model said. A scripted provider supplies the tool calls, so
  the harness measures the runtime rather than the model.
* **safety** — does the permission gate stop a corpus of dangerous commands, and
  let ordinary work through? Both directions matter: a gate that blocks
  everything is as useless as one that blocks nothing.
* **tokens** — what does a realistic session cost, against a baseline that
  represents how the other harnesses encode the same information?

The token suite reports **cost per completed task**, not tokens per turn. A 30%
token cut that drops success from 80% to 70% is a cost increase, because failures
get retried by a human, and a harness that optimises the wrong number will happily
make that trade.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalash.models.normalize import (
    BlockDelta,
    BlockStart,
    BlockStop,
    MessageStart,
    MessageStop,
    StopReason,
    UsageUpdate,
)

# Anthropic-shaped price ratios, relative to one input token. Used to convert
# mixed usage into a single comparable figure.
PRICE_INPUT = Decimal("1")
PRICE_CACHE_READ = Decimal("0.1")
PRICE_CACHE_WRITE = Decimal("1.25")
PRICE_OUTPUT = Decimal("5")


@dataclass
class ScriptedTurn:
    """One scripted model response."""

    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    call_id: str = "tu_1"
    input_tokens: int = 500
    output_tokens: int = 40

    def events(self) -> list[Any]:
        head = [MessageStart(id="msg", model="eval/scripted")]
        if self.tool:
            body = [
                BlockStart(
                    index=0,
                    block_type="tool_use",
                    tool_use_id=self.call_id,
                    tool_name=self.tool,
                ),
                BlockDelta(index=0, delta=json.dumps(self.args)),
                BlockStop(index=0),
            ]
            stop = StopReason.TOOL_USE
        else:
            body = [
                BlockStart(index=0, block_type="text"),
                BlockDelta(index=0, delta=self.text),
                BlockStop(index=0),
            ]
            stop = StopReason.END_TURN
        return [
            *head,
            *body,
            UsageUpdate(
                input_tokens=self.input_tokens, output_tokens=self.output_tokens
            ),
            MessageStop(stop),
        ]


class ScriptedProvider:
    """Replays scripted turns and records what it was sent."""

    name = "eval/scripted"
    context_window = 200_000

    def __init__(self, turns: list[ScriptedTurn]) -> None:
        self._turns = list(turns)
        self.requests: list[dict[str, Any]] = []

    async def stream(self, messages, *, system=None, tools=None, **kwargs):
        self.requests.append(
            {"system": system or "", "tools": tools or [], "messages": messages}
        )
        turn = self._turns.pop(0) if self._turns else ScriptedTurn(text="done")
        for event in turn.events():
            yield event


class _Resolution:
    def __init__(self, provider: Any) -> None:
        self.ok = True
        self.provider = provider
        self.reason = ""


def install_provider(monkeypatch: Any, turns: list[ScriptedTurn]) -> ScriptedProvider:
    """Point provider resolution at a scripted provider."""
    provider = ScriptedProvider(turns)
    monkeypatch.setattr(
        "kalash.models.resolve.build_provider", lambda *a, **k: _Resolution(provider)
    )
    return provider


@dataclass
class TaskResult:
    """Outcome of one capability task."""

    name: str
    passed: bool
    detail: str = ""
    turns: int = 0
    tool_calls: int = 0
    tokens: int = 0


@dataclass
class SuiteReport:
    """Aggregate outcome of a suite."""

    name: str
    results: list[TaskResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def render(self) -> str:
        lines = [f"{self.name}: {self.passed}/{self.total} ({self.rate:.0%})"]
        for result in self.results:
            mark = "pass" if result.passed else "FAIL"
            suffix = f" — {result.detail}" if result.detail else ""
            lines.append(f"  [{mark}] {result.name}{suffix}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Token accounting
# ---------------------------------------------------------------------------


@dataclass
class TokenLedger:
    """Accumulates usage and converts it to a comparable cost figure."""

    label: str
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0

    def add(
        self,
        *,
        input_tokens: int = 0,
        cache_read: int = 0,
        cache_write: int = 0,
        output: int = 0,
    ) -> None:
        self.input_tokens += input_tokens
        self.cache_read_tokens += cache_read
        self.cache_write_tokens += cache_write
        self.output_tokens += output

    @property
    def raw_tokens(self) -> int:
        """Total tokens moved, ignoring price differences."""
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.output_tokens
        )

    @property
    def cost_units(self) -> Decimal:
        """Cost in input-token equivalents.

        Necessary because raw token counts hide the two effects that dominate a
        real bill: cache reads are a tenth of input, and output is five times it.
        """
        return (
            Decimal(self.input_tokens) * PRICE_INPUT
            + Decimal(self.cache_read_tokens) * PRICE_CACHE_READ
            + Decimal(self.cache_write_tokens) * PRICE_CACHE_WRITE
            + Decimal(self.output_tokens) * PRICE_OUTPUT
        )


def reduction(baseline: Decimal, candidate: Decimal) -> float:
    """Fractional reduction of candidate against baseline."""
    if baseline <= 0:
        return 0.0
    return float((baseline - candidate) / baseline)


def fmt_units(value: Decimal) -> str:
    """Human-readable cost figure."""
    number = float(value)
    if number >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}k"
    return f"{number:.0f}"


def project_root() -> Path:
    """Repository root, for locating fixtures."""
    return Path(__file__).resolve().parent.parent
