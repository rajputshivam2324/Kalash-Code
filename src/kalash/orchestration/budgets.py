"""Per-run budget ceilings with reservation-based inheritance.

Child budget: min(requested, parent_remaining - outstanding_reservations).
Graduated response: warning at 0.75, soft limit at 0.90, hard at 1.00.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from kalash.core.budget import BudgetDimension, BudgetState, Usage, Pricing
from kalash.core.errors import BudgetError, BudgetImmutableError
from kalash.core.events import Event, EventType, get_event_bus


class BudgetLevel(StrEnum):
    """Graduated budget response levels."""

    OK = "ok"
    WARNING = "warning"  # 0.75 threshold
    SOFT_LIMIT = "soft_limit"  # 0.90 threshold
    HARD_LIMIT = "hard_limit"  # 1.00 threshold


@dataclass
class BudgetViolation:
    """Details of a budget ceiling violation."""

    dimension: BudgetDimension
    level: BudgetLevel
    current: float
    ceiling: float
    ratio: float


@dataclass
class RunBudget:
    """Per-run budget with graduated enforcement.

    Inherits ceilings from parent via reservation. The child never
    exceeds what the parent reserved for it.
    """

    run_id: str
    parent_run_id: str | None = None

    # Ceilings
    max_tokens: int = 500_000
    max_turns: int = 100
    max_wallclock_s: int = 3600
    max_cost: Decimal = field(default_factory=lambda: Decimal("2.00"))

    # Current usage
    tokens_used: int = 0
    turns_used: int = 0
    cost_used: Decimal = field(default_factory=lambda: Decimal("0"))
    _start_time: float = field(default_factory=time.time)

    # Reservations given to children
    tokens_reserved: int = 0
    cost_reserved: Decimal = field(default_factory=lambda: Decimal("0"))

    # Thresholds
    warning_threshold: float = 0.75
    soft_limit_threshold: float = 0.90

    # State
    _warned_dimensions: set[BudgetDimension] = field(default_factory=set)
    _soft_limited_dimensions: set[BudgetDimension] = field(default_factory=set)

    @property
    def wallclock_used_s(self) -> float:
        """Compute wallclock usage from start time."""
        return time.time() - self._start_time

    @property
    def remaining_tokens(self) -> int:
        return self.max_tokens - self.tokens_used - self.tokens_reserved

    @property
    def remaining_cost(self) -> Decimal:
        return self.max_cost - self.cost_used - self.cost_reserved

    @property
    def remaining_turns(self) -> int:
        return self.max_turns - self.turns_used

    @property
    def remaining_wallclock_s(self) -> float:
        return self.max_wallclock_s - self.wallclock_used_s

    @classmethod
    def from_parent(
        cls,
        run_id: str,
        parent: "RunBudget",
        *,
        requested_tokens: int = 100_000,
        requested_turns: int = 25,
        requested_wallclock_s: int = 300,
        requested_cost: Decimal | None = None,
    ) -> "RunBudget":
        """Create a child budget via reservation from parent.

        child_ceiling = min(requested, parent_remaining - outstanding)
        """
        if requested_cost is None:
            requested_cost = Decimal("0.50")

        # Compute actual ceilings: min(requested, parent_available)
        actual_tokens = min(requested_tokens, parent.remaining_tokens)
        actual_turns = min(requested_turns, parent.remaining_turns)
        actual_wallclock = min(requested_wallclock_s, int(parent.remaining_wallclock_s))
        actual_cost = min(requested_cost, parent.remaining_cost)

        if actual_tokens <= 0 or actual_cost <= 0:
            raise BudgetError(
                "Parent has insufficient remaining budget for child reservation",
                recoverable=True,
            )

        # Reserve in parent
        parent.tokens_reserved += actual_tokens
        parent.cost_reserved += actual_cost

        return cls(
            run_id=run_id,
            parent_run_id=parent.run_id,
            max_tokens=actual_tokens,
            max_turns=actual_turns,
            max_wallclock_s=actual_wallclock,
            max_cost=actual_cost,
        )

    def release_to_parent(self, parent: "RunBudget") -> None:
        """Release reservation back to parent, debiting actuals."""
        parent.tokens_reserved -= self.max_tokens
        parent.cost_reserved -= self.max_cost
        # Debit what was actually used
        parent.tokens_used += self.tokens_used
        parent.cost_used += self.cost_used

    def record_usage(self, usage: Usage, pricing: Pricing | None = None) -> None:
        """Record token usage and compute cost."""
        self.tokens_used += usage.total_tokens

        if pricing:
            cost = Decimal(0)
            cost += Decimal(usage.input_tokens) * pricing.input_per_mtok / Decimal(1_000_000)
            cost += Decimal(usage.output_tokens) * pricing.output_per_mtok / Decimal(1_000_000)
            if pricing.cache_read_per_mtok and usage.cache_read_tokens:
                cost += (
                    Decimal(usage.cache_read_tokens)
                    * pricing.cache_read_per_mtok
                    / Decimal(1_000_000)
                )
            self.cost_used += cost

    def record_turn(self) -> None:
        """Record a turn completion."""
        self.turns_used += 1

    async def check_and_enforce(self) -> BudgetViolation | None:
        """Check budget dimensions and enforce graduated response.

        Returns the highest-severity violation found, or None.
        Emits events at threshold crossings.
        """
        violations: list[BudgetViolation] = []

        # Check each dimension
        violations.extend(self._check_dimension(
            BudgetDimension.TOKENS,
            float(self.tokens_used),
            float(self.max_tokens),
        ))
        violations.extend(self._check_dimension(
            BudgetDimension.COST,
            float(self.cost_used),
            float(self.max_cost),
        ))
        violations.extend(self._check_dimension(
            BudgetDimension.WALLCLOCK,
            self.wallclock_used_s,
            float(self.max_wallclock_s),
        ))
        violations.extend(self._check_dimension(
            BudgetDimension.TURNS,
            float(self.turns_used),
            float(self.max_turns),
        ))

        if not violations:
            return None

        # Sort by severity (hard > soft > warning)
        severity_order = {
            BudgetLevel.HARD_LIMIT: 3,
            BudgetLevel.SOFT_LIMIT: 2,
            BudgetLevel.WARNING: 1,
        }
        violations.sort(key=lambda v: severity_order.get(v.level, 0), reverse=True)
        worst = violations[0]

        # Emit events for threshold crossings
        bus = get_event_bus()
        if worst.level == BudgetLevel.WARNING and worst.dimension not in self._warned_dimensions:
            self._warned_dimensions.add(worst.dimension)
            await bus.emit(Event(
                type=EventType.BUDGET_WARNING,
                data={"dimension": worst.dimension, "ratio": worst.ratio},
                run_id=self.run_id,
            ))
        elif worst.level == BudgetLevel.SOFT_LIMIT and worst.dimension not in self._soft_limited_dimensions:
            self._soft_limited_dimensions.add(worst.dimension)
            await bus.emit(Event(
                type=EventType.BUDGET_SOFT_LIMIT,
                data={"dimension": worst.dimension, "ratio": worst.ratio},
                run_id=self.run_id,
            ))
        elif worst.level == BudgetLevel.HARD_LIMIT:
            await bus.emit(Event(
                type=EventType.BUDGET_EXCEEDED,
                data={"dimension": worst.dimension, "ratio": worst.ratio},
                run_id=self.run_id,
            ))
            raise BudgetError(
                f"Budget hard limit reached: {worst.dimension} at {worst.ratio:.0%}",
                recoverable=False,
            )

        return worst

    def _check_dimension(
        self,
        dimension: BudgetDimension,
        current: float,
        ceiling: float,
    ) -> list[BudgetViolation]:
        """Check a single dimension against thresholds."""
        if ceiling <= 0:
            return []

        ratio = current / ceiling
        violations: list[BudgetViolation] = []

        if ratio >= 1.0:
            violations.append(BudgetViolation(
                dimension=dimension,
                level=BudgetLevel.HARD_LIMIT,
                current=current,
                ceiling=ceiling,
                ratio=ratio,
            ))
        elif ratio >= self.soft_limit_threshold:
            violations.append(BudgetViolation(
                dimension=dimension,
                level=BudgetLevel.SOFT_LIMIT,
                current=current,
                ceiling=ceiling,
                ratio=ratio,
            ))
        elif ratio >= self.warning_threshold:
            violations.append(BudgetViolation(
                dimension=dimension,
                level=BudgetLevel.WARNING,
                current=current,
                ceiling=ceiling,
                ratio=ratio,
            ))

        return violations

    def to_budget_state(self) -> BudgetState:
        """Convert to a core BudgetState for compatibility."""
        return BudgetState(
            max_tokens=self.max_tokens,
            max_cost=self.max_cost,
            max_wallclock_s=self.max_wallclock_s,
            max_turns=self.max_turns,
            tokens_used=self.tokens_used,
            cost_used=self.cost_used,
            wallclock_used_s=self.wallclock_used_s,
            turns_used=self.turns_used,
            tokens_reserved=self.tokens_reserved,
            cost_reserved=self.cost_reserved,
        )
