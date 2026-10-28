"""Test isolation: a private home and project root, no credentials or non-loopback network."""

from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import pytest

from kalash.agent.build import Runtime, create_runtime
from kalash.agent.events import AgentEvent
from kalash.config import Settings, load_settings
from kalash.permissions import Approver
from tests.harness.scripted import Reply, ScriptedAdapter

_KEY_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET")
_real_connect = socket.socket.connect


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    # Stop project discovery here, even when pytest's temp directory is inside a checkout.
    # An empty marker keeps tests outside a real Git repository until they explicitly init one.
    (work / ".git").mkdir()
    monkeypatch.setenv("KALASH_HOME", str(home / ".kalash"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KALASH_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    for name in list(os.environ):
        if name.endswith(_KEY_SUFFIXES) or name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(work)
    yield


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Tests may only connect to loopback; anything else is a bug in the test.

    Tests marked ``live`` are the one exception: they exist to talk to real providers."""
    if request.node.get_closest_marker("live"):
        return

    def guarded(self: socket.socket, address: object) -> None:
        host = address[0] if isinstance(address, tuple) else address
        if self.family in (socket.AF_INET, socket.AF_INET6) and host not in (
            "127.0.0.1",
            "::1",
            "localhost",
        ):
            raise AssertionError(f"tests must not use the network (connect to {host!r})")
        _real_connect(self, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def work(tmp_path: Path) -> Path:
    return (tmp_path / "work").resolve()


@pytest.fixture
def settings(work: Path) -> Settings:
    return load_settings(work)[0]


class Harness:
    """A real runtime (loop, tools, permissions, session log) over a scripted model."""

    def __init__(self, runtime: Runtime, adapter: ScriptedAdapter, events: list[AgentEvent]):
        self.runtime = runtime
        self.adapter = adapter
        self.events = events

    @property
    def agent(self):  # type: ignore[no-untyped-def]
        return self.runtime.agent

    @property
    def session(self):  # type: ignore[no-untyped-def]
        return self.runtime.session

    async def turn(self, prompt: str):  # type: ignore[no-untyped-def]
        return await self.runtime.agent.run_turn(prompt)

    def of(self, kind: type) -> list:  # type: ignore[type-arg]
        return [e for e in self.events if isinstance(e, kind)]


HarnessFactory = Callable[..., "AsyncIterator[Harness]"]


@pytest.fixture
async def make_harness(work: Path) -> AsyncIterator[Callable[..., object]]:
    created: list[Runtime] = []

    async def factory(
        script: list[Reply],
        *,
        settings: Settings | None = None,
        approver: Approver | None = None,
        window: int = 100_000,
        session=None,  # type: ignore[no-untyped-def]
        **overrides: object,
    ) -> Harness:
        cfg = settings or load_settings(work, {"model": "scripted/model", **overrides})[0]
        adapter = ScriptedAdapter(script, window=window)
        events: list[AgentEvent] = []
        runtime = await create_runtime(
            cwd=work,
            settings=cfg,
            model="scripted/model",
            adapter=adapter,
            approver=approver,
            emit=events.append,
            with_mcp=False,
            session=session,
        )
        created.append(runtime)
        return Harness(runtime, adapter, events)

    yield factory
    for runtime in created:
        await runtime.aclose()
