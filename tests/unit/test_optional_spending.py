"""Spending is measured by default and capped only when explicitly configured."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from kalash.core.budget import BudgetState, Pricing
from kalash.core.config import load_config
from kalash.models.normalize import (
    BlockDelta,
    BlockStart,
    BlockStop,
    MessageStop,
    StopReason,
    UsageUpdate,
)
from kalash.runtime.request import request


class RecordingGateway:
    primary = SimpleNamespace(pricing=Pricing())

    def __init__(self):
        self.calls = []

    async def stream(self, _messages, **kwargs):
        self.calls.append(kwargs)
        yield BlockStart(index=0, block_type="text")
        yield BlockDelta(index=0, delta="Done")
        yield BlockStop(index=0)
        yield UsageUpdate(input_tokens=10, output_tokens=4)
        yield MessageStop(stop_reason=StopReason.END_TURN)


def test_default_config_has_no_spending_ceiling(tmp_path):
    config = load_config(tmp_path)
    assert config.budget.max_tokens is None
    assert config.budget.max_cost_usd is None


async def test_default_spend_does_not_shrink_or_stop_responses():
    gateway = RecordingGateway()
    budget = BudgetState(tokens_used=600_000, cost_used=Decimal("3"))
    result = await request(
        gateway,
        budget,
        asyncio.Event(),
        [],
        system="test",
        tools=[],
        max_output_tokens=8192,
        temperature=None,
        on_text_delta=None,
    )
    assert result.error is None
    assert gateway.calls[0]["max_tokens"] == 8192
    assert budget.tokens_used == 600_014
    assert budget.cost_used > Decimal("3")


async def test_explicit_insufficient_cap_refuses_without_shortening_response():
    gateway = RecordingGateway()
    budget = BudgetState(max_tokens=1000)
    result = await request(
        gateway,
        budget,
        asyncio.Event(),
        [],
        system="test",
        tools=[],
        max_output_tokens=8192,
        temperature=None,
        on_text_delta=None,
    )
    assert result.error is not None
    assert result.error.code == "budget_exhausted"
    assert "tokens" in result.error.error
    assert gateway.calls == []
    assert budget.turns_used == 0


def test_explicit_config_and_null_override(tmp_path):
    config = load_config(
        tmp_path, overrides={"budget": {"max_tokens": 1000, "max_cost_usd": "0.5"}}
    )
    assert config.budget.max_tokens == 1000
    assert config.budget.max_cost_usd == Decimal("0.5")
    config = load_config(tmp_path, overrides={"budget": {"max_tokens": None, "max_cost_usd": None}})
    assert config.budget.max_tokens is None
    assert config.budget.max_cost_usd is None


async def test_cost_cap_is_independent_of_token_cap():
    gateway = RecordingGateway()
    budget = BudgetState(max_cost=Decimal("0.001"))
    result = await request(
        gateway,
        budget,
        asyncio.Event(),
        [],
        system="test",
        tools=[],
        max_output_tokens=8192,
        temperature=None,
        on_text_delta=None,
    )
    assert result.error is not None
    assert "cost" in result.error.error
    assert gateway.calls == []


def test_uncapped_parent_still_accounts_for_children():
    budget = BudgetState()
    assert budget.reserve_for_child(10_000, Decimal("1"))
    budget.release_reservation(10_000, Decimal("1"), 200, Decimal("0.02"))
    assert budget.tokens_reserved == 0
    assert budget.cost_reserved == 0
    assert budget.tokens_used == 200
    assert budget.cost_used == Decimal("0.02")


async def test_environment_changes_preserve_cacheable_instruction_and_history_prefix():
    from kalash.models.normalize import Message, Role, TextBlock
    from kalash.runtime.context import ContextAssembler

    assembler = ContextAssembler(BudgetState(), 32_768)
    args = {
        "system_identity": "Stable instructions",
        "tool_schemas": [],
        "skills_catalog": [],
        "kalash_md_chain": ["Project rules"],
        "memory_blocks": [],
        "recent_turns": [Message(Role.USER, [TextBlock(text="Earlier task")])],
        "current_message": Message(Role.USER, [TextBlock(text="Continue")]),
    }
    first = await assembler.assemble(**args, environment={"git_status": "clean"})
    second = await assembler.assemble(**args, environment={"git_status": "2 modified files"})
    assert first[:-1] == second[:-1]
    assert "2 modified files" in second[-1].content[0].text
