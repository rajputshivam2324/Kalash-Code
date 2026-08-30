"""Integration tests — full pipeline round-trips through real subsystems.

These exercise multi-component flows end-to-end with a fake LLM provider
but real tools, real persistence, and real serialization.
"""
from __future__ import annotations
import json, pytest
from pathlib import Path
from kalash.core.budget import BudgetState
from kalash.core.events import EventBus
from kalash.models.normalize import (
    BlockDelta, BlockStart, BlockStop, Message, MessageStart, MessageStop,
    Role, StopReason, TextBlock, ToolResultBlock, ToolUseBlock, UsageUpdate,
)
from kalash.permissions.policy import PermissionPolicy
from kalash.permissions.prompt import ApprovalPrompt, ApprovalResponse, PromptContext, PromptResult
from kalash.runtime.context import ContextAssembler
from kalash.runtime.loop import AgentLoop, TerminationReason
from kalash.runtime.prompt import build_system_prompt
from kalash.runtime.scratchpad import Scratchpad, reset_cache
from kalash.runtime.serialize import deserialize_blocks, rehydrate_messages, serialize_blocks
from kalash.runtime.toolhost import ToolHost, normalize_sandbox_mode
from kalash.tools.builtins import default_registry

# ── Shared Fixtures ──────────────────────────────────────────────────────

class FakeGateway:
    def __init__(self, turns):
        self.turns, self.calls = list(turns), []
        class P: name="fake"; context_window=200_000
        self.primary = P()
    async def stream(self, messages, *, system=None, tools=None, **kw):
        self.calls.append({"messages": messages, "system": system, "tools": tools})
        for e in (self.turns.pop(0) if self.turns else [MessageStop(StopReason.END_TURN)]):
            yield e

def _text(t):
    return [MessageStart(id="m",model="fake"),BlockStart(index=0,block_type="text"),
            BlockDelta(index=0,delta=t),BlockStop(index=0),
            UsageUpdate(input_tokens=50,output_tokens=10),MessageStop(StopReason.END_TURN)]

def _tool(name, args, cid="tu_1"):
    return [MessageStart(id="m",model="fake"),
            BlockStart(index=0,block_type="tool_use",tool_use_id=cid,tool_name=name),
            BlockDelta(index=0,delta=json.dumps(args)),BlockStop(index=0),
            UsageUpdate(input_tokens=50,output_tokens=20),MessageStop(StopReason.TOOL_USE)]

class AutoApprover:
    def __init__(self, r=ApprovalResponse.ALLOW_ONCE): self.response=r; self.seen=[]
    async def show_approval_prompt(self, ctx): self.seen.append(ctx); return PromptResult(response=self.response)
    async def show_info(self, msg): pass

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KALASH_HOME", str(tmp_path / "home"))
    reset_cache(); yield; reset_cache()

def _host(tmp_path, *, mode="build", ui=None):
    bus = EventBus()
    return ToolHost(registry=default_registry(), event_bus=bus, session_id="ses",
                    cwd=tmp_path, sandbox_mode="workspace-write", mode=mode,
                    policy=PermissionPolicy(event_bus=bus, sandbox_mode=normalize_sandbox_mode("workspace-write")),
                    approval=ApprovalPrompt(event_bus=bus, ui=ui, non_interactive=ui is None))

def _loop(gw, host, *, repo=None):
    b = BudgetState(max_tokens=100_000)
    return AgentLoop(gateway=gw, tool_registry=host, session_repo=repo, event_bus=host.event_bus,
                     budget=b, assembler=ContextAssembler(budget=b, context_window=200_000),
                     session_id="ses", system_prompt=build_system_prompt())

async def _send(loop, text):
    return await loop.run(user_message=Message(role=Role.USER, content=[TextBlock(text=text)]),
                          system_identity=loop.system_prompt)

# ── Integration: Full Agent Pipeline ─────────────────────────────────────

class TestFullPipeline:
    """Build an agent, run multi-turn with tools, verify disk effects."""

    @pytest.mark.asyncio
    async def test_write_then_read_roundtrip(self, tmp_path):
        target = tmp_path / "hello.txt"
        gw = FakeGateway([
            _tool("write", {"path": str(target), "content": "world\n"}),
            _text("Created hello.txt."),
        ])
        host = _host(tmp_path, ui=AutoApprover())
        result = await _send(_loop(gw, host), "create hello.txt")
        assert target.read_text() == "world\n"
        assert result.termination_reason is TerminationReason.NO_TOOL_CALLS

    @pytest.mark.asyncio
    async def test_multi_tool_chain(self, tmp_path):
        """read → edit → shell verification — the common agent workflow."""
        (tmp_path / "src.py").write_text("x = 1\ny = 2\n")
        gw = FakeGateway([
            _tool("read", {"path": str(tmp_path / "src.py")}),
            _tool("shell", {"command": "cat " + str(tmp_path / "src.py")}),
            _text("Inspected src.py."),
        ])
        host = _host(tmp_path, ui=AutoApprover())
        result = await _send(_loop(gw, host), "inspect src.py")
        assert result.iterations >= 2
        assert len(gw.calls) >= 3

    @pytest.mark.asyncio
    async def test_history_accumulates_across_turns(self, tmp_path):
        gw = FakeGateway([_text("one"), _text("two"), _text("three")])
        host = _host(tmp_path)
        loop = _loop(gw, host)
        await _send(loop, "first")
        r2 = await loop.run(user_message=Message(role=Role.USER, content=[TextBlock(text="second")]),
                            system_identity=loop.system_prompt, recent_turns=loop.conversation)
        assert len(gw.calls[1]["messages"]) > len(gw.calls[0]["messages"])

    @pytest.mark.asyncio
    async def test_tool_result_fed_back_to_model(self, tmp_path):
        (tmp_path / "data.txt").write_text("42\n")
        gw = FakeGateway([_tool("read", {"path": str(tmp_path / "data.txt")}), _text("done")])
        host = _host(tmp_path, ui=AutoApprover())
        await _send(_loop(gw, host), "read data")
        second_msgs = gw.calls[1]["messages"]
        assert any(isinstance(b, ToolResultBlock) for m in second_msgs for b in m.content)

    @pytest.mark.asyncio
    async def test_plan_mode_blocks_writes(self, tmp_path):
        gw = FakeGateway([_text("plan only")])
        host = _host(tmp_path, mode="plan")
        names = {s["name"] for s in host.schemas()}
        assert "write" not in names
        assert "read" in names

# ── Integration: Session Persistence ─────────────────────────────────────

class TestSessionPersistence:
    """Create → persist → resume → verify messages."""

    def test_session_create_and_list(self, tmp_path, monkeypatch):
        from kalash.runtime.session import SessionManager
        mgr = SessionManager()
        session = mgr.create(str(tmp_path))
        sessions = mgr.list_sessions(limit=10)
        assert any(s["id"] == session.id for s in sessions)

    def test_session_delete(self, tmp_path):
        from kalash.runtime.session import SessionManager
        mgr = SessionManager()
        s = mgr.create(str(tmp_path))
        assert mgr.delete_session(s.id)
        assert mgr.get_session(s.id) is None

    def test_session_export_json(self, tmp_path):
        from kalash.runtime.session import SessionManager
        mgr = SessionManager()
        s = mgr.create(str(tmp_path))
        exported = mgr.export_session(s.id, format="json")
        assert exported is not None
        data = json.loads(exported)
        assert data["session"]["id"] == s.id

    def test_session_export_markdown(self, tmp_path):
        from kalash.runtime.session import SessionManager
        mgr = SessionManager()
        s = mgr.create(str(tmp_path))
        exported = mgr.export_session(s.id, format="markdown")
        assert exported is not None
        assert s.id in exported

    def test_nonexistent_session_returns_none(self, tmp_path):
        from kalash.runtime.session import SessionManager
        mgr = SessionManager()
        assert mgr.export_session("nonexistent") is None

# ── Integration: Scratchpad ──────────────────────────────────────────────

class TestScratchpadIntegration:
    """put → expand → persist → reload — the full observation lifecycle."""

    def test_put_and_expand_roundtrip(self, tmp_path):
        pad = Scratchpad("test", root=tmp_path)
        result = pad.put("file", "src/main.py", "def main():\n    pass\n")
        assert result.ref.startswith("#f")
        expanded = pad.expand(result.ref)
        assert expanded.ok
        assert "def main" in expanded.content

    def test_deduplication(self, tmp_path):
        pad = Scratchpad("test", root=tmp_path)
        r1 = pad.put("file", "a.py", "same content")
        r2 = pad.put("file", "a.py", "same content")
        assert r1.ref == r2.ref
        assert r2.deduped is True

    def test_persist_and_reload(self, tmp_path):
        pad1 = Scratchpad("test", root=tmp_path)
        pad1.put("file", "a.py", "content here")
        pad1.note("remember: use pnpm")
        pad1.save()

        pad2 = Scratchpad("test", root=tmp_path)
        pad2.load()
        assert pad2.get("#f1") is not None
        assert "pnpm" in pad2.render_notes()

    def test_expand_unknown_ref_fails_gracefully(self, tmp_path):
        pad = Scratchpad("test", root=tmp_path)
        result = pad.expand("#z99")
        assert not result.ok
        assert "Unknown" in result.error

    def test_grep_in_expand(self, tmp_path):
        pad = Scratchpad("test", root=tmp_path)
        body = "\n".join(f"line {i}" for i in range(100))
        r = pad.put("file", "big.txt", body)
        result = pad.expand(r.ref, grep="line 42")
        assert result.ok
        assert "line 42" in result.content

    def test_notes_rendered_separately(self, tmp_path):
        pad = Scratchpad("test", root=tmp_path)
        pad.put("file", "a.py", "code")
        pad.note("important note")
        index = pad.render_index()
        notes = pad.render_notes()
        assert "important note" not in index  # notes excluded from index
        assert "important note" in notes

    def test_clear_removes_everything(self, tmp_path):
        pad = Scratchpad("test", root=tmp_path)
        pad.put("file", "a.py", "x")
        pad.clear()
        assert pad.stats()["refs"] == 0

# ── Integration: Serialization Round-Trip ────────────────────────────────

class TestSerializationRoundTrip:
    """Full message serialize → persist → deserialize → verify."""

    def test_complex_conversation_roundtrip(self):
        rows = [
            {"role": "user", "content": serialize_blocks([TextBlock(text="read a.py")])},
            {"role": "assistant", "content": serialize_blocks([
                TextBlock(text="Reading..."),
                ToolUseBlock(id="tu_1", name="read", input={"path": "a.py"}),
            ])},
            {"role": "user", "content": serialize_blocks([
                ToolResultBlock(tool_use_id="tu_1", content="def foo(): pass"),
            ])},
            {"role": "assistant", "content": serialize_blocks([TextBlock(text="Done.")])},
        ]
        messages = rehydrate_messages(rows)
        assert len(messages) == 4
        assert messages[0].role is Role.USER
        assert messages[1].role is Role.ASSISTANT
        assert any(isinstance(b, ToolUseBlock) for b in messages[1].content)
        assert messages[3].role is Role.ASSISTANT

    def test_consecutive_same_role_merged(self):
        rows = [
            {"role": "user", "content": serialize_blocks([TextBlock(text="a")])},
            {"role": "user", "content": serialize_blocks([TextBlock(text="b")])},
        ]
        messages = rehydrate_messages(rows)
        assert len(messages) == 1
        assert len(messages[0].content) == 2

    def test_dangling_tool_use_dropped(self):
        rows = [
            {"role": "assistant", "content": serialize_blocks([
                TextBlock(text="let me check"),
                ToolUseBlock(id="tu_orphan", name="read", input={}),
            ])},
        ]
        messages = rehydrate_messages(rows)
        for m in messages:
            for b in m.content:
                assert not isinstance(b, ToolUseBlock)

    def test_legacy_plain_text_survives(self):
        blocks = deserialize_blocks("just plain text")
        assert blocks[0].text == "just plain text"

# ── Integration: Provider Auth Flow ──────────────────────────────────────

class TestProviderAuthFlow:
    """Save key → switch → switch back → verify persistence."""

    def test_full_provider_switch_cycle(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALASH_HOME", str(tmp_path))
        from kalash.tui.providers import PROVIDERS
        for p in PROVIDERS:
            monkeypatch.delenv(p.env_key, raising=False)

        from kalash.tui.auth_store import save_credential, get_credential, set_active_provider, get_active_provider
        from kalash.models.resolve import credential_for

        # Save keys for two providers
        save_credential("openrouter", "sk-or-key")
        save_credential("groq", "gsk-key")

        # Switch to openrouter
        set_active_provider("openrouter", "model-a")
        assert get_active_provider() == ("openrouter", "model-a")
        assert credential_for("openrouter") == "sk-or-key"

        # Switch to groq
        set_active_provider("groq", "model-b")
        assert get_active_provider() == ("groq", "model-b")
        assert credential_for("groq") == "gsk-key"

        # Switch back — key must still be there
        set_active_provider("openrouter", "model-a")
        assert credential_for("openrouter") == "sk-or-key"

    def test_env_var_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALASH_HOME", str(tmp_path))
        from kalash.tui.providers import PROVIDERS
        for p in PROVIDERS:
            monkeypatch.delenv(p.env_key, raising=False)
        monkeypatch.setenv("GROQ_API_KEY", "gsk-from-env")

        from kalash.models.resolve import credential_for, active_selection
        pid, _ = active_selection()
        assert pid == "groq"
        assert credential_for("groq") == "gsk-from-env"

# ── Integration: MCP Registry ────────────────────────────────────────────

class TestMCPRegistryIntegration:
    def test_add_list_get_server(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from kalash.mcp.registry import MCPRegistry
        reg = MCPRegistry()
        entry = reg.add_server(name="test-server", url="http://localhost:8080", transport="sse")
        assert entry.name == "test-server"
        servers = reg.list_servers()
        assert any(s.name == "test-server" for s in servers)
        got = reg.get_server("test-server")
        assert got is not None and got.url == "http://localhost:8080"

    def test_to_config_stdio(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from kalash.mcp.registry import MCPRegistry, MCPServerEntry
        from kalash.mcp.client import TransportType
        reg = MCPRegistry()
        entry = MCPServerEntry(name="s", transport="stdio", url="npx server")
        cfg = reg._to_config(entry)
        assert cfg.transport is TransportType.STDIO
        assert cfg.command == "npx server"

    def test_to_config_sse(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from kalash.mcp.registry import MCPRegistry, MCPServerEntry
        from kalash.mcp.client import TransportType
        reg = MCPRegistry()
        entry = MCPServerEntry(name="s", transport="sse", url="http://localhost:3000")
        cfg = reg._to_config(entry)
        assert cfg.transport is TransportType.SSE

# ── Integration: build_agent Full Pipeline ───────────────────────────────

class TestBuildAgentIntegration:
    @pytest.mark.asyncio
    async def test_build_agent_wires_tools_and_prompt(self, tmp_path, monkeypatch):
        from kalash.models.normalize import MessageStop, StopReason
        class Stub:
            name="stub"; context_window=200_000
            async def stream(self, messages, **kw):
                yield MessageStop(StopReason.END_TURN)
        class Res:
            ok=True; provider=Stub(); provider_id="test"; model_id="stub"; reason=""
        monkeypatch.setattr("kalash.models.resolve.build_provider", lambda *a,**k: Res())
        from kalash.runtime.agent import build_agent
        agent, reason = build_agent(cwd=tmp_path, interactive=False)
        assert agent is not None, reason
        assert len(agent.loop.system_prompt) > 500
        tool_names = {s["name"] for s in agent.host.schemas()}
        assert "read" in tool_names
        assert "write" in tool_names
        assert "shell" in tool_names

    @pytest.mark.asyncio
    async def test_build_agent_plan_mode_excludes_writes(self, tmp_path, monkeypatch):
        from kalash.models.normalize import MessageStop, StopReason
        class Stub:
            name="stub"; context_window=200_000
            async def stream(self, messages, **kw): yield MessageStop(StopReason.END_TURN)
        class Res:
            ok=True; provider=Stub(); provider_id="test"; model_id="stub"; reason=""
        monkeypatch.setattr("kalash.models.resolve.build_provider", lambda *a,**k: Res())
        from kalash.runtime.agent import build_agent
        agent, _ = build_agent(cwd=tmp_path, interactive=False, mode="plan")
        assert agent is not None
        names = {s["name"] for s in agent.host.schemas()}
        assert "write" not in names
        assert "shell" not in names
        assert "read" in names

# ── Integration: Tool Execution ──────────────────────────────────────────

class TestToolExecution:
    @pytest.mark.asyncio
    async def test_write_creates_nested_dirs(self, tmp_path):
        host = _host(tmp_path, ui=AutoApprover())
        target = tmp_path / "a" / "b" / "c.txt"
        await host.execute("write", {"path": str(target), "content": "deep\n", "create_dirs": True}, tool_use_id="t")
        assert target.read_text() == "deep\n"

    @pytest.mark.asyncio
    async def test_read_returns_file_content(self, tmp_path):
        (tmp_path / "r.txt").write_text("visible\n")
        host = _host(tmp_path)
        out = await host.execute("read", {"path": str(tmp_path / "r.txt")}, tool_use_id="t")
        assert "visible" in out

    @pytest.mark.asyncio
    async def test_shell_runs_and_returns_output(self, tmp_path):
        host = _host(tmp_path, ui=AutoApprover())
        out = await host.execute("shell", {"command": "echo hello-world"}, tool_use_id="t")
        assert "hello-world" in out

    @pytest.mark.asyncio
    async def test_list_returns_directory_contents(self, tmp_path):
        (tmp_path / "file1.txt").write_text("a")
        (tmp_path / "file2.py").write_text("b")
        host = _host(tmp_path)
        out = await host.execute("list", {"path": str(tmp_path)}, tool_use_id="t")
        assert "file1.txt" in out
        assert "file2.py" in out

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self, tmp_path):
        host = _host(tmp_path, ui=AutoApprover())
        out = await host.execute("nonexistent_tool", {}, tool_use_id="t")
        assert "ERROR" in out or "REFUSED" in out

    @pytest.mark.asyncio
    async def test_protected_path_refused(self, tmp_path):
        host = _host(tmp_path, ui=AutoApprover(ApprovalResponse.ALLOW_ALWAYS))
        out = await host.execute("write", {"path": str(tmp_path / ".env"), "content": "S=1"}, tool_use_id="t")
        assert "protected" in out.lower()
        assert not (tmp_path / ".env").exists()

# ── Integration: Budget Tracking ─────────────────────────────────────────

class TestBudgetTracking:
    @pytest.mark.asyncio
    async def test_tokens_are_tracked(self, tmp_path):
        gw = FakeGateway([_text("ok")])
        host = _host(tmp_path)
        loop = _loop(gw, host)
        result = await _send(loop, "hi")
        assert loop.budget.tokens_used > 0
        assert result.total_tokens > 0

    @pytest.mark.asyncio
    async def test_budget_exhaustion_stops_loop(self, tmp_path):
        gw = FakeGateway([_tool("read", {"path": "."})] * 20 + [_text("done")])
        host = _host(tmp_path, ui=AutoApprover())
        b = BudgetState(max_tokens=1)  # impossibly small
        loop = AgentLoop(gateway=gw, tool_registry=host, session_repo=None,
                         event_bus=host.event_bus, budget=b,
                         assembler=ContextAssembler(budget=b, context_window=200_000),
                         session_id="ses", system_prompt="test")
        result = await _send(loop, "hi")
        assert result.termination_reason in (
            TerminationReason.BUDGET_EXHAUSTED, TerminationReason.ERROR,
            TerminationReason.NO_TOOL_CALLS,
        )

# ── Integration: Event Bus ───────────────────────────────────────────────

class TestEventBusIntegration:
    @pytest.mark.asyncio
    async def test_events_fire_during_tool_execution(self, tmp_path):
        from kalash.core.events import Event, EventType
        events_seen = []
        host = _host(tmp_path, ui=AutoApprover())
        async def on_complete(event: Event):
            events_seen.append(event.type)
        host.event_bus.on(EventType.TOOL_COMPLETE, on_complete)
        (tmp_path / "x.txt").write_text("hi")
        await host.execute("read", {"path": str(tmp_path / "x.txt")}, tool_use_id="t")
        assert EventType.TOOL_COMPLETE in events_seen

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_crash_bus(self):
        from kalash.core.events import EventBus, Event, EventType
        bus = EventBus()
        async def bad_handler(e): raise RuntimeError("boom")
        bus.on(EventType.TURN_START, bad_handler)
        # Must not raise
        await bus.emit(Event(type=EventType.TURN_START, session_id="s", data={}))

# ── Integration: System Prompt Wiring ────────────────────────────────────

class TestPromptWiring:
    def test_prompt_has_all_required_sections(self):
        prompt = build_system_prompt(mode="build")
        assert "Kalash" in prompt
        assert "todo" in prompt.lower()
        assert "Reason" in prompt and "Act" in prompt  # ReAct
        assert "filesystem" in prompt.lower()
        assert "safety" in prompt.lower() or "Safety" in prompt

    def test_plan_mode_prompt_differs(self):
        build = build_system_prompt(mode="build")
        plan = build_system_prompt(mode="plan")
        assert build != plan
        assert "plan" in plan.lower()

    def test_compact_prompt_is_shorter(self):
        full = build_system_prompt(mode="build", compact=False)
        compact = build_system_prompt(mode="build", compact=True)
        assert len(compact) < len(full)

    def test_project_instructions_injected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALASH_HOME", str(tmp_path / "h"))
        (tmp_path / "KALASH.md").write_text("Always use pytest.")
        from kalash.runtime.prompt import discover_project_instructions
        instr = discover_project_instructions(tmp_path)
        prompt = build_system_prompt(project_instructions=instr)
        assert "pytest" in prompt
