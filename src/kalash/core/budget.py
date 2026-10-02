"""Token accounting and cost math.

All cost calculations use Decimal to avoid float drift.
Budget ceilings are enforced at state transitions, not by polling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any


class BudgetDimension(StrEnum):
    """Budget dimensions that can be capped."""

    TOKENS = "tokens"
    COST = "cost"
    WALLCLOCK = "wallclock"
    TURNS = "turns"
    TOOL_CALLS = "tool_calls"
    SPAWN_DEPTH = "spawn_depth"


@dataclass
class Usage:
    """Normalized token usage from a model call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    source: str = "provider_reported"  # or "estimated"
    provider_raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        """Total tokens including cache writes."""
        return self.input_tokens + self.output_tokens + self.cache_write_tokens


@dataclass
class Pricing:
    """Model pricing information."""

    input_per_mtok: Decimal = field(default_factory=lambda: Decimal("3.00"))
    output_per_mtok: Decimal = field(default_factory=lambda: Decimal("15.00"))
    cache_read_per_mtok: Decimal | None = None
    cache_write_per_mtok: Decimal | None = None
    reasoning_per_mtok: Decimal | None = None
    currency: str = "USD"
    pricing_version: str = ""


@dataclass
class BudgetState:
    """Current spend against budget ceilings."""

    # Ceilings
    max_tokens: int = 500_000
    max_cost: Decimal = field(default_factory=lambda: Decimal("2.00"))
    max_wallclock_s: int = 3600
    max_turns: int = 100
    max_tool_calls: int = 500
    max_spawn_depth: int = 3

    # Current spend
    tokens_used: int = 0
    cost_used: Decimal = field(default_factory=lambda: Decimal("0"))
    wallclock_used_s: float = 0.0
    turns_used: int = 0
    tool_calls_used: int = 0
    current_depth: int = 0

    # Reservations (for child agents)
    tokens_reserved: int = 0
    cost_reserved: Decimal = field(default_factory=lambda: Decimal("0"))

    def remaining_tokens(self) -> int:
        return self.max_tokens - self.tokens_used - self.tokens_reserved

    def remaining_cost(self) -> Decimal:
        return self.max_cost - self.cost_used - self.cost_reserved

    def check_ceiling(self) -> BudgetDimension | None:
        """Check if any ceiling is hit. Returns the dimension or None."""
        if self.tokens_used >= self.max_tokens:
            return BudgetDimension.TOKENS
        if self.cost_used >= self.max_cost:
            return BudgetDimension.COST
        if self.wallclock_used_s >= self.max_wallclock_s:
            return BudgetDimension.WALLCLOCK
        if self.turns_used >= self.max_turns:
            return BudgetDimension.TURNS
        if self.tool_calls_used >= self.max_tool_calls:
            return BudgetDimension.TOOL_CALLS
        return None

    def check_warning(self, threshold: float = 0.75) -> BudgetDimension | None:
        """Check if any dimension is at warning level."""
        if self.tokens_used >= self.max_tokens * threshold:
            return BudgetDimension.TOKENS
        if self.cost_used >= self.max_cost * Decimal(str(threshold)):
            return BudgetDimension.COST
        if self.wallclock_used_s >= self.max_wallclock_s * threshold:
            return BudgetDimension.WALLCLOCK
        if self.turns_used >= int(self.max_turns * threshold):
            return BudgetDimension.TURNS
        return None

    def record_usage(self, usage: Usage, pricing: Pricing | None = None) -> None:
        """Record token usage and compute cost."""
        self.tokens_used += usage.total_tokens

        if pricing:
            cost = Decimal(0)
            uncached = usage.input_tokens
            if pricing.cache_read_per_mtok is not None:
                uncached = max(0, uncached - usage.cache_read_tokens)
            cost += Decimal(uncached) * pricing.input_per_mtok / Decimal(1_000_000)
            cost += Decimal(usage.output_tokens) * pricing.output_per_mtok / Decimal(1_000_000)
            if pricing.cache_read_per_mtok and usage.cache_read_tokens:
                cost += (
                    Decimal(usage.cache_read_tokens)
                    * pricing.cache_read_per_mtok
                    / Decimal(1_000_000)
                )
            if pricing.cache_write_per_mtok and usage.cache_write_tokens:
                cost += (
                    Decimal(usage.cache_write_tokens)
                    * pricing.cache_write_per_mtok
                    / Decimal(1_000_000)
                )
            if pricing.reasoning_per_mtok and usage.reasoning_tokens:
                cost += (
                    Decimal(usage.reasoning_tokens)
                    * pricing.reasoning_per_mtok
                    / Decimal(1_000_000)
                )
            self.cost_used += cost

    def reserve_for_child(self, tokens: int, cost: Decimal) -> bool:
        """Reserve budget for a child agent. Returns False if insufficient."""
        if tokens > self.remaining_tokens() or cost > self.remaining_cost():
            return False
        self.tokens_reserved += tokens
        self.cost_reserved += cost
        return True

    def release_reservation(
        self, tokens: int, cost: Decimal, actual_tokens: int, actual_cost: Decimal
    ) -> None:
        """Release a child's reservation and debit actual spend."""
        self.tokens_reserved -= tokens
        self.cost_reserved -= cost
        self.tokens_used += actual_tokens
        self.cost_used += actual_cost


def estimate_tokens(text: str, mode: str = "code") -> int:
    """Estimate token count using character heuristic.

    Conservative (over-estimates): 3.2 chars/token for code, 4.0 for prose.
    Safety multiplier: 1.05.
    """
    if not text:
        return 0
    divisor = 3.2 if mode == "code" else 4.0
    return int(len(text) / divisor * 1.05)
