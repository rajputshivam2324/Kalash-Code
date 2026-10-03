"""Receipts survive abrupt process death and prevent subsequent dependent writes."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from kalash.core.budget import BudgetState
from kalash.core.events import EventBus
from kalash.models.normalize import ToolResultBlock, ToolUseBlock
from kalash.runtime.execution import ToolExecutor
from kalash.runtime.serialize import rehydrate_messages
from kalash.runtime.toolhost import ToolCategory
from kalash.storage.engine import StorageEngine
from kalash.storage.repositories.sessions import SessionRepository


@pytest.mark.parametrize("phase", ["before_effect", "after_effect", "after_receipt"])
async def test_abrupt_exit_retains_known_and_unknown_outcomes(tmp_path: Path, phase: str) -> None:
    worker = Path(__file__).parents[1] / "fixtures" / "crash_tool_worker.py"
    process = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(worker), str(tmp_path), phase],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert process.returncode == 93, process.stderr.decode()
    engine = StorageEngine(tmp_path / "crash.db")
    repo = SessionRepository(engine)
    rows = await repo.get_session_messages((tmp_path / "session").read_text())
    history = rehydrate_messages(rows)
    results = {
        b.tool_use_id: b for m in history for b in m.content if isinstance(b, ToolResultBlock)
    }
    assert set(results) == {"first", "second"}
    assert results["first"].is_error == (phase != "after_receipt")
    assert results["second"].is_error
    assert "Outcome unknown" in results["second"].content
    if phase == "before_effect":
        assert not (tmp_path / "effects").exists()
    else:
        assert (tmp_path / "effects").read_text() == "first\n"
    # Rehydration is pure: repeated recovery must not replay the recorded intents.
    assert rehydrate_messages(rows) == history
    engine.close()


async def test_receipt_failure_blocks_next_write() -> None:
    effects: list[str] = []

    class Host:
        def category(self, _name: str) -> ToolCategory:
            return ToolCategory.EXEC

        def schemas(self) -> list[dict[str, Any]]:
            return []

        async def execute(self, _name: str, _arguments: dict[str, Any], *, tool_use_id: str) -> str:
            effects.append(tool_use_id)
            return "done"

    async def fail_receipt(_result: ToolResultBlock) -> None:
        raise OSError("disk unavailable")

    executor = ToolExecutor(
        Host(), BudgetState(), EventBus(), "session", asyncio.Event(), settle=fail_receipt
    )
    with pytest.raises(OSError, match="disk unavailable"):
        await executor.execute(
            [ToolUseBlock("first", "write", {}), ToolUseBlock("second", "write", {})]
        )
    assert effects == ["first"]


async def test_receipt_failure_cancels_and_joins_read_siblings() -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    class Host:
        def category(self, _name: str) -> ToolCategory:
            return ToolCategory.READ

        def schemas(self) -> list[dict[str, Any]]:
            return []

        async def execute(self, _name: str, _arguments: dict[str, Any], *, tool_use_id: str) -> str:
            if tool_use_id == "fast":
                await started.wait()
                return "done"
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                closed.set()
            return "unreachable"

    async def fail_receipt(_result: ToolResultBlock) -> None:
        raise OSError("disk unavailable")

    executor = ToolExecutor(
        Host(), BudgetState(), EventBus(), "session", asyncio.Event(), settle=fail_receipt
    )
    with pytest.raises(ExceptionGroup):
        await executor.execute([ToolUseBlock("fast", "read", {}), ToolUseBlock("slow", "read", {})])
    assert closed.is_set()
