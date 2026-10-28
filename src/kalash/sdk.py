"""Embed Kalash in Python.

    async with Kalash(cwd=".", model="anthropic/claude-sonnet-4-5") as agent:
        result = await agent.run("fix the failing test")
        print(result.reason, result.text)

    async with Kalash(cwd=".") as agent:
        async for event in agent.stream("explain this repo"):
            if isinstance(event, TextChunk):
                print(event.text, end="")

Settings come from the same files as the CLI (user, project, local); ``settings`` overrides
them. With no ``approver``, actions that need approval are denied and reported in
``TurnResult.denials``. One turn runs at a time per instance.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from kalash.agent.build import Runtime, create_runtime
from kalash.agent.events import AgentEvent, TurnEnd, TurnResult
from kalash.config import load_settings
from kalash.llm.http import Adapter
from kalash.llm.types import Usage
from kalash.permissions import Approver
from kalash.session.log import SessionLog, find_session, list_sessions

__all__ = ["Kalash", "TurnResult"]


class Kalash:
    def __init__(
        self,
        cwd: str | Path = ".",
        *,
        model: str | None = None,
        settings: dict[str, Any] | None = None,
        approver: Approver | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
        resume: str | None = None,
        continue_latest: bool = False,
        adapter: Adapter | None = None,
    ) -> None:
        self.cwd = Path(cwd).expanduser().resolve()
        self._model = model
        self._overrides = dict(settings or {})
        self._approver = approver
        self._on_event = on_event
        self._resume = resume
        self._continue = continue_latest
        self._adapter = adapter
        self._runtime: Runtime | None = None
        self._queue: asyncio.Queue[AgentEvent] | None = None
        self._turn: asyncio.Task[TurnResult] | None = None
        self.warnings: list[str] = []

    # -- lifecycle ---------------------------------------------------------------------

    async def __aenter__(self) -> Kalash:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._runtime is not None:
            return
        if self._model:
            self._overrides.setdefault("model", self._model)
        settings, self.warnings = load_settings(self.cwd, self._overrides)
        from kalash.paths import project_root

        root = project_root(self.cwd)
        session: SessionLog | None = None
        if self._resume:
            session = SessionLog.open(find_session(root, self._resume))
        elif self._continue:
            found = list_sessions(root)
            if found:
                session = SessionLog.open(found[0].path)
        self._runtime = await create_runtime(
            cwd=self.cwd,
            settings=settings,
            model=self._model,
            session=session,
            approver=self._approver,
            emit=self._dispatch,
            adapter=self._adapter,
        )

    async def close(self) -> None:
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._turn
        if self._runtime is not None:
            runtime, self._runtime = self._runtime, None
            await runtime.aclose()

    # -- running -----------------------------------------------------------------------

    def _require(self) -> Runtime:
        if self._runtime is None:
            raise RuntimeError("call start() or use `async with Kalash(...)` first")
        if self._turn is not None and not self._turn.done():
            raise RuntimeError("a turn is already running on this Kalash instance")
        return self._runtime

    def _dispatch(self, event: AgentEvent) -> None:
        if self._queue is not None:
            self._queue.put_nowait(event)
        if self._on_event is not None:
            self._on_event(event)

    async def run(self, prompt: str) -> TurnResult:
        """Run one turn to completion. Cancelling the awaiting task cancels the turn."""
        runtime = self._require()
        self._queue = None
        self._turn = asyncio.ensure_future(runtime.agent.run_turn(prompt))
        return await self._turn

    async def stream(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """Yield events as they happen; the last one is ``TurnEnd``. Breaking out of the loop
        cancels the turn and leaves the session log consistent."""
        runtime = self._require()
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._queue = queue
        turn = asyncio.ensure_future(runtime.agent.run_turn(prompt))
        self._turn = turn
        try:
            while True:
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({getter, turn}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    event = getter.result()
                    yield event
                    if isinstance(event, TurnEnd):
                        return
                else:
                    getter.cancel()
                    while not queue.empty():
                        event = queue.get_nowait()
                        yield event
                        if isinstance(event, TurnEnd):
                            return
                    turn.result()  # surfaces an unexpected exception
                    return
        finally:
            if not turn.done():
                turn.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await turn
            self._queue = None

    # -- state -------------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._require_started().session.id

    @property
    def usage(self) -> Usage:
        return self._require_started().session.usage_total()

    @property
    def cost_usd(self) -> float | None:
        return self._require_started().session.cost_total()

    @property
    def model(self) -> str:
        return str(self._require_started().ref)

    def _require_started(self) -> Runtime:
        if self._runtime is None:
            raise RuntimeError("Kalash is not started")
        return self._runtime
