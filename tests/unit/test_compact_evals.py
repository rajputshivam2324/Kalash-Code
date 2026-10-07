"""Suite accounting rejects overspend and counts cancellations/reasoning honestly."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from evals.benchmark_tasks import load_tasks
from evals.compact_budget import MeteredProvider, SuiteBudgetExceededError, SuiteLedger

from kalash.models.normalize import UsageUpdate
from kalash.models.providers.sarvam import SarvamProvider


def test_shared_budget_counts_unknown_attempts_and_refunds_known_usage(tmp_path: Any) -> None:
    ledger = SuiteLedger(tmp_path / "ledger.json", limit=12_000)
    index = ledger.reserve("first", 100, 2000)
    ledger.settle(index, None, "cancelled")
    assert ledger.charged == 6196
    with pytest.raises(SuiteBudgetExceededError):
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
    with pytest.raises(SuiteBudgetExceededError):
        ledger.settle(index, UsageUpdate(input_tokens=5000, output_tokens=100), "complete")
    assert ledger.violated
    with pytest.raises(SuiteBudgetExceededError):
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


async def test_read_exposes_digest_and_overwrite_errors_remain_actionable(tmp_path: Any) -> None:
    import hashlib

    from kalash.core.events import Event, EventType
    from tests.unit.test_agent_loop import AutoApprover, make_host

    source = tmp_path / "module.py"
    source.write_text("answer = 0\n")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    host = make_host(tmp_path, ui=AutoApprover())
    events: list[Event] = []

    async def record(event: Event) -> None:
        events.append(event)

    host.event_bus.on_all(record)
    result = await host.execute_result("read", {"path": str(source)}, tool_use_id="read")
    assert expected in result.content
    assert "answer = 0" in result.content
    refused = await host.execute_result(
        "write", {"path": str(source), "content": "answer = 42\n"}, tool_use_id="missing"
    )
    assert refused.is_error
    failure = next(
        e
        for e in events
        if e.type == EventType.TOOL_COMPLETE and e.data.get("tool_use_id") == "missing"
    )
    assert failure.data["error_code"] == "KALASH_TOOL_STALE_READ"
    written = await host.execute_result(
        "write",
        {"path": str(source), "content": "answer = 42\n", "digest": expected},
        tool_use_id="write",
    )
    assert not written.is_error
    assert source.read_text() == "answer = 42\n"


async def test_metered_trace_preserves_observations_and_redacts_credentials(tmp_path: Any) -> None:
    import io
    import json

    from kalash.models.normalize import BlockDelta, MessageStop, StopReason

    fixture_marker = "fixture-secret-do-not-store"

    class Provider:
        name = "fixture/model"
        default_output_tokens = 200

        def serialize_messages(self, _messages: Any, _system: Any) -> list[Any]:
            return [{"role": "tool", "content": f"SHA-256: abc; {fixture_marker}"}]

        async def stream(self, _messages: Any, **_kwargs: Any) -> Any:
            yield BlockDelta(index=0, delta=fixture_marker[:10])
            yield BlockDelta(index=0, delta=fixture_marker[10:])
            yield UsageUpdate(input_tokens=100, output_tokens=10)
            yield MessageStop(StopReason.END_TURN)

    trace = io.StringIO()
    ledger = SuiteLedger(tmp_path / "ledger.json", limit=60000)
    provider = MeteredProvider(Provider(), ledger, "trial", trace=trace, credential=fixture_marker)
    assert len([event async for event in provider.stream([])]) == 4
    rows = [json.loads(line) for line in trace.getvalue().splitlines()]
    assert rows[0]["data"]["messages"][0]["content"] == "SHA-256: abc; [credential]"
    assert rows[0]["data"]["max_output_tokens"] == 200
    assert rows[-1]["data"]["stop_reason"] == "end_turn"
    assert fixture_marker not in trace.getvalue()
    assert fixture_marker[:10] not in trace.getvalue()
    assert fixture_marker[10:] not in trace.getvalue()
    assert rows[1]["data"]["delta_chars"] == 10
    assert ledger.charged == 110


async def test_selected_live_trial_without_credential_never_dispatches(
    tmp_path: Any, monkeypatch: Any
) -> None:
    import argparse
    import json
    from pathlib import Path

    from evals.compact import run

    monkeypatch.setattr("evals.compact.preflight_ok", lambda: True)
    monkeypatch.setattr("evals.compact.credential_for", lambda _: "")
    args = argparse.Namespace(
        output=tmp_path / "trial",
        tasks=Path("evals/tasks/compact_v1.json"),
        task="ttl-lru",
        reasoning="off",
        total_tokens=60000,
        output_limit=6144,
        access_log=None,
        prior_ledger=None,
        scripted=False,
    )
    assert await run(args) == 2
    assert json.loads((args.output / "ledger.json").read_text())["attempts"] == []
    manifest = json.loads((args.output / "manifest.json").read_text())
    assert manifest["selected_task"] == "ttl-lru"
    assert manifest["reasoning"] == "off"
