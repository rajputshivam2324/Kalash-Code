"""Reservation refusals identify the controller ceiling without provider spending."""

import json
from typing import Any

import pytest
from evals.compact import cost_inr
from evals.compact_budget import MeteredProvider, SuiteBudgetExceededError, SuiteLedger

from kalash.models.gateway import ModelGateway
from kalash.models.normalize import MessageStop, StopReason, UsageUpdate
from kalash.runtime.loop import TerminationReason
from tests.unit.test_agent_loop import make_host, make_loop, send


@pytest.mark.parametrize(
    ("limit", "task", "expected"),
    [(12_000, "second", "suite_tokens"), (240_000, "first", "task_tokens")],
)
def test_reservation_refusal_records_ceiling_and_breakdown(
    tmp_path: Any, limit: int, task: str, expected: str
) -> None:
    ledger = SuiteLedger(tmp_path / "ledger.json", limit=limit)
    index = ledger.reserve("first", 0, 100)
    ledger.settle(index, UsageUpdate(input_tokens=4000, output_tokens=100), "complete")
    maximum = 4000 if expected == "suite_tokens" else 48_000
    with pytest.raises(SuiteBudgetExceededError) as caught:
        ledger.reserve(task, 500, maximum)
    assert expected in str(caught.value)
    assert "wire_bytes=500" in str(caught.value)
    assert f"output_tokens={maximum}" in str(caught.value)
    assert ledger.charged == 4100
    saved = json.loads(ledger.path.read_text())
    assert saved["refusals"][-1]["dimensions"] == [expected]
    assert saved["refusals"][-1]["reserved_tokens"] == 500 + 4096 + maximum
    assert len(saved["attempts"]) == 1


async def test_suite_refusal_is_budget_termination_without_retry_or_provider_call(tmp_path: Any):
    class Provider:
        name = "local/refused"
        default_output_tokens = 4096
        calls = 0

        def serialize_messages(self, _messages: Any, _system: Any) -> list[Any]:
            return []

        async def stream(self, _messages: Any, **_kwargs: Any) -> Any:
            self.calls += 1
            yield MessageStop(StopReason.END_TURN)

    provider = Provider()
    ledger = SuiteLedger(tmp_path / "ledger.json", limit=5000)
    gateway = ModelGateway(MeteredProvider(provider, ledger, "task"))
    loop = make_loop(gateway, make_host(tmp_path))
    result = await send(loop, "answer")
    assert result.termination_reason == TerminationReason.BUDGET_EXHAUSTED
    assert "suite_tokens" in result.error
    assert provider.calls == 0
    assert ledger.charged == loop.budget.tokens_used == 0
    assert len(ledger.refusals) == 1


def test_incomplete_reported_usage_does_not_imply_known_cost(tmp_path: Any) -> None:
    ledger = SuiteLedger(tmp_path / "ledger.json")
    index = ledger.reserve("interrupted", 100, 4096)
    ledger.settle(index, UsageUpdate(input_tokens=100, output_tokens=30), "incomplete")
    assert ledger.attempts[0]["usage"]["input_tokens"] == 100
    assert cost_inr(ledger) is None
