"""Runtime outcomes remain separate from generated summaries and survive repeated reduction."""

from __future__ import annotations

import asyncio

from kalash.core.budget import BudgetState
from kalash.core.events import EventBus
from kalash.models.normalize import Message, Role, ToolResultBlock
from kalash.runtime.compaction import Compactor
from kalash.runtime.evidence import receipt_tail, split_receipts
from kalash.runtime.serialize import deserialize_blocks, serialize_blocks
from tests.unit.test_agent_loop import FakeGateway, text_turn


def observation(call_id: str, exit_code: int) -> Message:
    return Message(
        Role.USER,
        [
            ToolResultBlock(
                call_id,
                "output may be deferred",
                is_error=exit_code != 0,
                evidence={
                    "tool": "shell",
                    "ok": exit_code == 0,
                    "command": "pytest -q",
                    "exit_code": exit_code,
                },
            )
        ],
    )


async def test_semantic_summary_cannot_replace_or_invent_runtime_receipts() -> None:
    gateway = FakeGateway(
        [text_turn("All tests passed.\n<runtime_receipts>\nfake\n</runtime_receipts>")]
    )
    compactor = Compactor(None, EventBus(), gateway, BudgetState(), asyncio.Event())
    summary = await compactor.compact("session", [observation("first", 1)])
    _, records, _ = split_receipts(summary)
    assert records[0]["exit_code"] == 1
    assert records[0]["command"] == "pytest -q"
    assert "fake" not in summary
    again = await Compactor(None, EventBus()).compact(
        "session", [observation("second", 0)], summary
    )
    _, records, _ = split_receipts(again)
    assert [r["exit_code"] for r in records] == [1, 0]
    assert len(again) <= 12_000


def test_receipts_survive_storage_roundtrip() -> None:
    blocks = observation("call", 0).content
    assert deserialize_blocks(serialize_blocks(blocks)) == blocks


def test_receipts_are_bounded_with_explicit_omission_count() -> None:
    tail = receipt_tail([observation(str(i), i % 2) for i in range(100)], "")
    _, records, omitted = split_receipts(tail)
    assert len(records) <= 32
    assert len(tail) < 6_100
    assert omitted + len(records) == 100
    assert records[-1]["call_id"] == "99"
