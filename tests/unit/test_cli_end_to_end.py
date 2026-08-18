"""End-to-end tests through the real CLI assembly path.

These exercise ``build_agent`` — provider resolution, session creation, tool
registration, the permission gate, context assembly — and the headless entry
point, with only the network faked. If this file passes, ``kalash -p "…"`` can
build software.

The screenshot bug that motivated this: the agent replied "As a text-only AI I
don't have access to your filesystem" because no tools were ever attached.
:meth:`TestHeadless.test_agent_writes_a_real_project` is the regression test.
"""

import json

import pytest

from kalash.models.normalize import (
    BlockDelta,
    BlockStart,
    BlockStop,
    MessageStart,
    MessageStop,
    StopReason,
    UsageUpdate,
)
from kalash.runtime.scratchpad import reset_cache


class FakeProvider:
    """Minimal provider satisfying what the gateway and loop touch."""

    name = "fake/model-1"
    context_window = 200_000

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests: list[dict] = []

    async def stream(self, messages, *, system=None, tools=None, **kwargs):
        self.requests.append({"system": system, "tools": tools, "messages": messages})
        events = self.turns.pop(0) if self.turns else [MessageStop(StopReason.END_TURN)]
        for event in events:
            yield event


class FakeResolution:
    def __init__(self, provider):
        self.ok = True
        self.provider = provider
        self.reason = ""


def tool_call(name, args, call_id="tu_1"):
    return [
        MessageStart(id="m", model="fake/model-1"),
        BlockStart(index=0, block_type="tool_use", tool_use_id=call_id, tool_name=name),
        BlockDelta(index=0, delta=json.dumps(args)),
        BlockStop(index=0),
        UsageUpdate(input_tokens=200, output_tokens=40),
        MessageStop(StopReason.TOOL_USE),
    ]


def final(text):
    return [
        MessageStart(id="m2", model="fake/model-1"),
        BlockStart(index=0, block_type="text"),
        BlockDelta(index=0, delta=text),
        BlockStop(index=0),
        UsageUpdate(input_tokens=250, output_tokens=25),
        MessageStop(StopReason.END_TURN),
    ]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """An isolated workspace, Kalash home, and cwd."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("KALASH_HOME", str(home))
    monkeypatch.chdir(project)
    reset_cache()
    yield project
    reset_cache()


def install_provider(monkeypatch, turns):
    provider = FakeProvider(turns)
    monkeypatch.setattr(
        "kalash.models.resolve.build_provider",
        lambda *a, **k: FakeResolution(provider),
    )
    return provider


class TestBuildAgent:
    def test_assembles_a_working_agent(self, workspace, monkeypatch):
        install_provider(monkeypatch, [final("hi")])
        from kalash.runtime.agent import build_agent

        agent, reason = build_agent(cwd=workspace, interactive=False)

        assert agent is not None, reason
        assert agent.session_id.startswith("ses_")
        assert agent.host.schemas(), "tools must be registered"
        ctx = agent.host.build_context()
        assert workspace.resolve() in ctx.writable_roots
        assert "fs.write" in ctx.capabilities

    def test_reports_missing_provider_instead_of_raising(self, workspace, monkeypatch):
        class Bad:
            ok = False
            provider = None
            reason = "no API key configured"

        monkeypatch.setattr(
            "kalash.models.resolve.build_provider", lambda *a, **k: Bad()
        )
        from kalash.runtime.agent import build_agent

        agent, reason = build_agent(cwd=workspace)
        assert agent is None
        assert "no API key" in reason

    def test_plan_mode_withholds_mutating_tools(self, workspace, monkeypatch):
        install_provider(monkeypatch, [final("plan")])
        from kalash.runtime.agent import build_agent

        agent, _ = build_agent(cwd=workspace, mode="plan", interactive=False)
        names = {s["name"] for s in agent.host.schemas()}
        assert "read" in names
        assert "write" not in names
        assert "shell" not in names

    def test_mode_switch_changes_tools_and_prompt(self, workspace, monkeypatch):
        install_provider(monkeypatch, [final("x")])
        from kalash.runtime.agent import build_agent

        agent, _ = build_agent(cwd=workspace, interactive=False)
        assert "write" in {s["name"] for s in agent.host.schemas()}

        agent.set_mode("plan")
        assert "write" not in {s["name"] for s in agent.host.schemas()}
        assert "Mode: PLAN" in agent.loop.system_prompt

    def test_project_instructions_are_discovered(self, workspace, monkeypatch):
        (workspace / "KALASH.md").write_text("Always use pnpm.")
        install_provider(monkeypatch, [final("ok")])
        from kalash.runtime.agent import build_agent

        agent, _ = build_agent(cwd=workspace, interactive=False)
        assert any("pnpm" in text for text in agent.instructions)


class TestHeadless:
    @pytest.mark.asyncio
    async def test_agent_writes_a_real_project(self, workspace, monkeypatch):
        """The regression test for "I cannot access your filesystem"."""
        install_provider(
            monkeypatch,
            [
                tool_call(
                    "write",
                    {
                        "path": "server.js",
                        "content": "const http=require('http');\n",
                        "create_dirs": True,
                    },
                    call_id="tu_a",
                ),
                tool_call(
                    "write",
                    {
                        "path": "package.json",
                        "content": '{"name":"todo","main":"server.js"}\n',
                        "create_dirs": True,
                    },
                    call_id="tu_b",
                ),
                final("Created server.js and package.json."),
            ],
        )
        from kalash.cli._pipe import EXIT_OK, _run

        code = await _run("build me a node todo app")

        assert code == EXIT_OK
        assert (workspace / "server.js").exists()
        assert (workspace / "package.json").exists()
        assert "require('http')" in (workspace / "server.js").read_text()

    @pytest.mark.asyncio
    async def test_exit_code_when_no_provider(self, workspace, monkeypatch):
        class Bad:
            ok = False
            provider = None
            reason = "not configured"

        monkeypatch.setattr(
            "kalash.models.resolve.build_provider", lambda *a, **k: Bad()
        )
        from kalash.cli._pipe import EXIT_PROVIDER_UNAVAILABLE, _run

        assert await _run("hello") == EXIT_PROVIDER_UNAVAILABLE

    @pytest.mark.asyncio
    async def test_plan_mode_refuses_to_write(self, workspace, monkeypatch):
        install_provider(
            monkeypatch,
            [
                tool_call("write", {"path": "nope.txt", "content": "x"}),
                final("I would create nope.txt."),
            ],
        )
        from kalash.cli._pipe import _run

        await _run("make a file", mode="plan")
        assert not (workspace / "nope.txt").exists()

    @pytest.mark.asyncio
    async def test_tools_and_system_prompt_are_sent(self, workspace, monkeypatch):
        provider = install_provider(monkeypatch, [final("hi")])
        from kalash.cli._pipe import _run

        await _run("hello")

        first = provider.requests[0]
        assert first["tools"], "tools must be attached"
        assert "filesystem" in first["system"].lower()
        assert all("input_schema" in tool for tool in first["tools"])


class TestSessionPersistenceAndResume:
    @pytest.mark.asyncio
    async def test_turns_are_persisted_and_resumable(self, workspace, monkeypatch):
        install_provider(monkeypatch, [final("remembered answer")])
        from kalash.runtime.agent import build_agent, load_history_async

        agent, _ = build_agent(cwd=workspace, interactive=False)
        await agent.send("first question")
        session_id = agent.session_id

        assert agent.loop.session_repo is not None
        rows = await agent.loop.session_repo.get_session_messages(session_id)
        assert rows, "messages must reach storage for resume to be possible"

        history = await load_history_async(agent.loop.session_repo, session_id)
        assert history, "stored rows must rehydrate into messages"
        assert any(
            "first question" in getattr(b, "text", "")
            for m in history
            for b in m.content
        )

    @pytest.mark.asyncio
    async def test_turn_sequences_do_not_collide(self, workspace, monkeypatch):
        """Regression: turns.seq has a UNIQUE constraint per session.

        Deriving seq from the per-run iteration counter collided between the
        user and assistant turn of one exchange, and again with stored turns on
        resume. The IntegrityError was swallowed by the persist except-block, so
        turns silently stopped being recorded.
        """
        install_provider(monkeypatch, [final("one"), final("two"), final("three")])
        from kalash.runtime.agent import build_agent, load_history_async

        agent, _ = build_agent(cwd=workspace, interactive=False)
        await agent.send("first")
        await agent.send("second")
        session_id = agent.session_id
        repo = agent.loop.session_repo
        assert repo is not None

        rows = await repo.get_session_messages(session_id)
        assert len(rows) == 4, "two exchanges must store four messages"

        resumed, _ = build_agent(
            cwd=workspace, session_id=session_id, resume=True, interactive=False
        )
        resumed.history = await load_history_async(repo, session_id)
        await resumed.send("third")

        after = await repo.get_session_messages(session_id)
        assert len(after) == 6, "a resumed turn must append, not collide"

    @pytest.mark.asyncio
    async def test_next_turn_seq_continues_after_stored_turns(self, workspace, monkeypatch):
        install_provider(monkeypatch, [final("x")])
        from kalash.runtime.agent import build_agent

        agent, _ = build_agent(cwd=workspace, interactive=False)
        repo = agent.loop.session_repo
        assert repo is not None

        assert await repo.next_turn_seq(agent.session_id) == 0
        await repo.create_turn(session_id=agent.session_id, seq=0, role="user")
        await repo.create_turn(session_id=agent.session_id, seq=1, role="assistant")
        assert await repo.next_turn_seq(agent.session_id) == 2

    def test_session_listing_and_export(self, workspace, monkeypatch):
        install_provider(monkeypatch, [final("x")])
        from kalash.runtime.agent import build_agent
        from kalash.runtime.session import SessionManager

        agent, _ = build_agent(cwd=workspace, interactive=False)

        manager = SessionManager()
        listed = manager.list_sessions(limit=10)
        assert any(s["id"] == agent.session_id for s in listed)
        assert "turn_count" in listed[0]

        detail = manager.get_session(agent.session_id)
        assert detail is not None
        assert "total_tokens" in detail

        exported = manager.export_session(agent.session_id, format="markdown")
        assert exported is not None
        assert agent.session_id in exported

        assert manager.delete_session(agent.session_id) is True
        assert manager.get_session(agent.session_id) is None
        assert manager.delete_session("ses_nonexistent") is False


class TestDoctor:
    def test_every_check_runs_without_a_broken_import(self, workspace, monkeypatch):
        """doctor imported seven modules that do not exist.

        `kalash.config.paths`, `kalash.models.registry`, `kalash.memory.manager`,
        `kalash.mcp.registry`, `kalash.storage.db`, `kalash.config.manager`, and
        `kalash.config.secrets` were all absent, so the command crashed with
        ModuleNotFoundError before printing anything.
        """
        install_provider(monkeypatch, [final("x")])
        from kalash.cli import doctor as doctor_module

        checks = [
            doctor_module._check_platform,  # noqa: SLF001
            doctor_module._check_sandbox,  # noqa: SLF001
            doctor_module._check_provider,  # noqa: SLF001
            doctor_module._check_tools,  # noqa: SLF001
            doctor_module._check_permissions,  # noqa: SLF001
            doctor_module._check_database,  # noqa: SLF001
            doctor_module._check_scratchpad,  # noqa: SLF001
            doctor_module._check_search,  # noqa: SLF001
            doctor_module._check_ripgrep,  # noqa: SLF001
            doctor_module._check_hooks,  # noqa: SLF001
            doctor_module._check_skills,  # noqa: SLF001
            doctor_module._check_instructions,  # noqa: SLF001
            doctor_module._check_disk,  # noqa: SLF001
            doctor_module._check_config,  # noqa: SLF001
        ]

        for check in checks:
            result = check()
            assert result.status in ("pass", "warn", "fail"), check.__name__
            assert result.name and result.detail, check.__name__

    def test_tool_check_sees_a_populated_registry(self, workspace, monkeypatch):
        install_provider(monkeypatch, [final("x")])
        from kalash.cli.doctor import _check_tools

        result = _check_tools()  # noqa: SLF001
        assert result.status == "pass"
        assert "registered" in result.detail


class TestPromptCaching:
    def test_anthropic_request_declares_cache_breakpoints(self):
        from kalash.models.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider(api_key="k", model="claude-sonnet-4-20250514")
        request = provider._build_request(  # noqa: SLF001
            [],
            system="the operating contract",
            tools=[
                {"name": "read", "description": "d", "input_schema": {}},
                {"name": "write", "description": "d", "input_schema": {}},
            ],
        )

        # Caching never happens implicitly; a breakpoint must be declared.
        assert isinstance(request["system"], list)
        assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert request["tools"][-1]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in request["tools"][0]

    def test_caching_can_be_disabled(self):
        from kalash.models.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider(api_key="k", cache_prompt=False)
        request = provider._build_request([], system="s", tools=[{"name": "r"}])  # noqa: SLF001
        assert request["system"] == "s"
        assert "cache_control" not in request["tools"][0]


class TestScratchpadIntegration:
    @pytest.mark.asyncio
    async def test_notes_survive_into_the_next_turn(self, workspace, monkeypatch):
        install_provider(
            monkeypatch,
            [
                tool_call("note", {"action": "add", "text": "user requires pnpm"}),
                final("noted"),
                final("second turn"),
            ],
        )
        from kalash.runtime.agent import build_agent

        agent, _ = build_agent(cwd=workspace, interactive=False)
        await agent.send("remember to use pnpm")

        blocks = agent.scratchpad_blocks()
        assert any("pnpm" in block for block in blocks)
