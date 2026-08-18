"""Graduated request shrinking for throughput-bound providers.

Different providers enforce different per-minute token ceilings — Groq free tier
is 8k, other hosts may allow 60k+, and the exact limit can differ by account
tier. Rather than hard-coding one shrink path, the agent loop walks these tiers
until the measured ``prompt + max_output`` fits the effective allowance.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class ShrinkTier(IntEnum):
    """Input-shrinking stages, from least to most aggressive."""

    NORMAL = 0
    STANDARD_TOOLS = 1
    MINIMAL_TOOLS = 2
    COMPACT_PROMPT = 3
    DROP_MEMORY = 4
    DROP_PROJECT_CONTEXT = 5


@dataclass(frozen=True, slots=True)
class TierFlags:
    """What a shrink tier changes in the assembled request."""

    force_tool_profile: str | None
    """``None`` = auto-size; ``standard`` / ``minimal`` = forced profile."""

    compact_prompt: bool
    drop_memory: bool
    drop_project_context: bool


_TIER_FLAGS: tuple[TierFlags, ...] = (
    TierFlags(None, False, False, False),
    TierFlags("standard", False, False, False),
    TierFlags("minimal", False, False, False),
    TierFlags("minimal", True, False, False),
    TierFlags("minimal", True, True, False),
    TierFlags("minimal", True, True, True),
)


def tier_flags(tier: ShrinkTier) -> TierFlags:
    """Flags for ``tier``."""
    return _TIER_FLAGS[int(tier)]


def next_tier(tier: ShrinkTier) -> ShrinkTier | None:
    """The next more aggressive tier, or ``None`` when exhausted."""
    value = int(tier) + 1
    if value >= len(_TIER_FLAGS):
        return None
    return ShrinkTier(value)
