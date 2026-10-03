"""Suite accounting rejects overspend and counts cancellations/reasoning honestly."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from evals.benchmark_tasks import load_tasks
from evals.compact_budget import MeteredProvider, SuiteBudgetExceeded, SuiteLedger

from kalash.models.normalize import UsageUpdate
from kalash.models.providers.sarvam import SarvamProvider


def test_shared_budget_counts_unknown_attempts_and_refunds_known_usage(tmp_path: Any) -> None:
    ledger = SuiteLedger(tmp_path / "ledger.json", limit=12_000)
    index = ledger.reserve("first", 100, 2000)
    ledger.settle(index, None, "cancelled")
    assert ledger.charged == 6196
    with pytest.raises(SuiteBudgetExceeded):
        ledger.reserve("second", 100, 2000)
    ledger.settle(
        index, UsageUpdate(input_tokens=500, output_tokens=900, reasoning_tokens=800), "complete"
    )
    assert ledger.charged == 1400  # reasoning is a subset of completion, cache reads of prompt
    ledger.reserve("second", 100, 2000)
    assert ledger.charged == 7596


def test_unexpected_provider_overrun_closes_suite(tmp_path: Any) -> None:
    ledger = SuiteLedger(tmp_path / "ledger.json")
    index = ledger.reserve("one", 0, 100)
    with pytest.raises(SuiteBudgetExceeded):
        ledger.settle(index, UsageUpdate(input_tokens=5000, output_tokens=100), "complete")
    assert ledger.violated
    with pytest.raises(SuiteBudgetExceeded):
        ledger.reserve("two", 0, 100)


async def test_cancelled_stream_keeps_reservation_and_closes_provider(tmp_path: Any) -> None:
    closed = asyncio.Event()

    class Provider:
        name = "test"
        default_output_tokens = 100

        def serialize_messages(self, _messages: Any, _system: Any) -> list[Any]:
            return []

        async def stream(self, _messages: Any, **_kwargs: Any) -> Any:
            try:
                yield "started"
                await asyncio.Event().wait()
            finally:
                closed.set()

    ledger = SuiteLedger(tmp_path / "ledger.json")
    stream = MeteredProvider(Provider(), ledger, "one").stream([])
    assert await anext(stream) == "started"
    await stream.aclose()
    assert closed.is_set()
    assert ledger.charged > 4096
    assert ledger.attempts[0]["usage"] is None


def test_model_specific_reasoning_defaults() -> None:
    assert SarvamProvider(model="sarvam-105b").reasoning_effort == "high"
    assert SarvamProvider(model="glm5.3").reasoning_effort == "max"
    assert (
        SarvamProvider(model="sarvam-105b", reasoning_effort="medium").reasoning_effort == "medium"
    )
    with pytest.raises(ValueError, match="V1"):
        SarvamProvider(model="sarvam-105b", reasoning_effort="max")
    with pytest.raises(ValueError, match="V2"):
        SarvamProvider(model="glm5.3", reasoning_effort="medium")


def test_fresh_suite_has_four_nontrivial_tasks() -> None:
    from pathlib import Path

    tasks = load_tasks(Path("evals/tasks/compact_v1.json"))
    assert len(tasks) == 4
    for task in tasks:
        assert len(task.hidden_test) > 1000
        assert task.scripted_edits
        for edit in task.scripted_edits:
            compile(edit["new_str"], edit["path"], "exec")
        compile(task.hidden_test, "hidden_grader", "exec")
