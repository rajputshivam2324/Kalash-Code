"""Exit without cleanup at explicit tool boundaries for recovery regression tests."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from kalash.core.budget import BudgetState
from kalash.core.events import EventBus
from kalash.models.normalize import Message, Role, ToolResultBlock, ToolUseBlock
from kalash.runtime.execution import ToolExecutor
from kalash.runtime.toolhost import ToolCategory
from kalash.runtime.transcript import Transcript
from kalash.storage.engine import StorageEngine
from kalash.storage.repositories.sessions import SessionRepository


async def run(root: Path, phase: str) -> None:
    engine = StorageEngine(root / "crash.db")
    await engine.initialize()
    repo = SessionRepository(engine)
    session_id = await repo.create_session(str(root))
    (root / "session").write_text(session_id)
    transcript = Transcript(repo, session_id)
    calls = [
        ToolUseBlock(id="first", name="effect", input={}),
        ToolUseBlock(id="second", name="effect", input={}),
    ]
    await transcript.append(Message(Role.ASSISTANT, calls))
    if phase == "before_effect":
        os._exit(93)

    class Host:
        def category(self, _name: str) -> ToolCategory:
            return ToolCategory.EXEC

        def schemas(self) -> list[dict[str, Any]]:
            return []

        async def execute(self, _name: str, _arguments: dict[str, Any], *, tool_use_id: str) -> str:
            with (root / "effects").open("a") as file:
                file.write(tool_use_id + "\n")
            if phase == "after_effect":
                os._exit(93)
            return "effect observed"

    async def settle(result: ToolResultBlock) -> None:
        await transcript.append(Message(Role.USER, [result]))
        os._exit(93)

    await ToolExecutor(
        Host(), BudgetState(), EventBus(), session_id, asyncio.Event(), settle=settle
    ).execute(calls)
    raise AssertionError("worker should have exited at its crash boundary")


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2]))
