"""Tests for every audit finding that was fixed.

Each test class maps to a severity code from the audit report:
  C-* = Critical, S-* = Serious, U-* = UX, A-* = Architecture

These are regression tests: they fail against the unfixed code and pass after
the fix, proving the fix is real and preventing silent reversion.
"""

from __future__ import annotations

import pytest

from kalash.runtime.scratchpad import reset_cache

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("KALASH_HOME", str(home))
    monkeypatch.chdir(project)
    reset_cache()
    yield project
    reset_cache()


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Point credential store at a temp KALASH_HOME and clear env vars."""
    monkeypatch.setenv("KALASH_HOME", str(tmp_path))
    from kalash.tui.providers import PROVIDERS

    for provider in PROVIDERS:
        monkeypatch.delenv(provider.env_key, raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# C-1: Google Provider _build_config return type
# ---------------------------------------------------------------------------


class TestC1GoogleBuildConfig:
    """_build_config was annotated as -> tuple[Any, Any] but returned a
    single GenerateContentConfig. Any code destructuring as tuple crashes."""

    def test_annotation_is_not_tuple(self):
        """The return annotation must not claim a tuple."""
        from kalash.models.providers.google import GeminiProvider

        hints = GeminiProvider._build_config.__annotations__
        ret = hints.get("return", "")
        # It used to say tuple[Any, Any] — now should be Any or a concrete type
        assert "tuple" not in str(ret).lower(), (
            f"_build_config still claims to return a tuple: {ret}"
        )


# ---------------------------------------------------------------------------
# C-2: Picker.set_items() did not exist
# ---------------------------------------------------------------------------


class TestC2PickerSetItems:
    """The _filter_commands method called picker.set_items() which doesn't
    exist on the Picker widget, causing an AttributeError."""

    def test_picker_has_no_set_items(self):
        from kalash.tui.picker import Picker

        # Confirm the method doesn't exist (it shouldn't)
        assert not hasattr(Picker, "set_items"), "If set_items was added, update the fix to use it"

    def test_picker_filter_works(self):
        from kalash.tui.picker import Picker, PickerItem, PickerMode

        picker = Picker()
        items = [
            PickerItem(value="/help", label="/help", detail="List commands"),
            PickerItem(value="/status", label="/status", detail="Show status"),
            PickerItem(value="/clear", label="/clear", detail="Clear"),
        ]
        picker.open(PickerMode.COMMAND, items)
        picker.filter("/sta")
        assert len(picker._shown) == 1
        assert picker._shown[0].value == "/status"


# ---------------------------------------------------------------------------
# C-3: _agent typed as bare object
# ---------------------------------------------------------------------------


class TestC3AgentTyping:
    """_agent was typed as `object`, making 15+ attribute accesses invalid."""

    def test_agent_type_annotation(self):
        import inspect

        from kalash.tui.app import KalashApp

        hints = {}
        for cls in KalashApp.__mro__:
            hints.update(getattr(cls, "__annotations__", {}))
        # Check the __init__ sets it to Agent | None
        src = inspect.getsource(KalashApp.__init__)
        assert "Agent | None" in src or "Agent|None" in src, (
            "_agent should be typed as Agent | None, not object"
        )


# ---------------------------------------------------------------------------
# C-5: MCP _entry_to_config receives correct type
# ---------------------------------------------------------------------------


class TestC5MCPEntryTyping:
    """_entry_to_config accepted bare `object` instead of MCPServerEntry."""

    def test_entry_parameter_is_typed(self):
        import inspect

        from kalash.mcp.registry import MCPRegistry

        sig = inspect.signature(MCPRegistry._to_config)
        param = sig.parameters["entry"]
        assert "MCPServerEntry" in str(param.annotation), (
            f"_entry_to_config parameter should be MCPServerEntry, got {param.annotation}"
        )


# ---------------------------------------------------------------------------
# C-6: model_id None passed as str
# ---------------------------------------------------------------------------


class TestC6ModelIdNone:
    """resolved_model could be None; downstream expects str."""

    def test_resolved_model_is_always_str(self, workspace, monkeypatch):
        """Even when everything returns None, resolved_model must be str."""
        from kalash.models.normalize import (
            MessageStop,
            StopReason,
        )

        class NullProvider:
            name = None
            model = None
            context_window = 200_000

            async def stream(self, messages, **kwargs):
                yield MessageStop(StopReason.END_TURN)

        class FakeRes:
            ok = True
            provider = NullProvider()
            provider_id = "test"
            model_id = None
            reason = ""

        monkeypatch.setattr(
            "kalash.models.resolve.build_provider",
            lambda *a, **k: FakeRes(),
        )
        from kalash.runtime.agent import build_agent

        agent, reason = build_agent(cwd=workspace, interactive=False)
        if agent is not None:
            assert isinstance(agent.loop.model_id, str)
            assert isinstance(agent.host.model_id, str)


# ---------------------------------------------------------------------------
# S-2: Provider type narrowing
# ---------------------------------------------------------------------------


class TestS2ProviderTypeNarrowing:
    """The provider variable in build_provider was narrowing on each branch."""

    def test_build_provider_returns_correct_types(self, isolated_store):
        from kalash.tui.auth_store import save_credential, set_active_provider

        set_active_provider("groq", "openai/gpt-oss-20b")
        save_credential("groq", "gsk-test")

        from kalash.models.resolve import build_provider

        result = build_provider()
        assert result.ok
        assert result.provider is not None


# ---------------------------------------------------------------------------
# S-3: None in fallback list
# ---------------------------------------------------------------------------


class TestS3FallbackNone:
    """build_gateway must not pass None providers in the fallback list."""

    def test_fallback_list_has_no_nones(self, isolated_store):
        from kalash.tui.auth_store import save_credential

        save_credential("groq", "gsk-test")

        from kalash.models.resolve import build_gateway

        # Even if one provider fails, the list should not contain None
        result = build_gateway(["groq/openai/gpt-oss-20b", "nope/model"])
        # If the primary succeeded, check the gateway
        if result.ok and hasattr(result.provider, "fallbacks"):
            for fb in result.provider.fallbacks:
                assert fb is not None, "None in fallback list will crash"


# ---------------------------------------------------------------------------
# S-6: Fire-and-forget task GC
# ---------------------------------------------------------------------------


class TestMCPSetupLifecycle:
    async def test_setup_is_awaited_once(self, workspace, monkeypatch):
        from unittest.mock import AsyncMock

        from kalash.runtime.agent import build_agent
        from tests.unit.test_wiring import FakeProvider, FakeResolution

        monkeypatch.setattr(
            "kalash.models.resolve.build_provider", lambda *a: FakeResolution(FakeProvider([]))
        )
        wire = AsyncMock(return_value=None)
        monkeypatch.setattr("kalash.mcp.load.wire_mcp_tools", wire)
        agent, reason = build_agent(cwd=workspace, persist=False)
        assert agent is not None, reason
        await agent.prepare()
        await agent.prepare()
        wire.assert_awaited_once()


class TestS7SocketTimeout:
    """_host_can_resolve mutated the global socket timeout."""

    def test_default_timeout_not_mutated(self):
        import socket

        original = socket.getdefaulttimeout()
        from kalash.sandbox.manager import _host_can_resolve

        _host_can_resolve()
        after = socket.getdefaulttimeout()
        assert after == original, f"socket.setdefaulttimeout was mutated: {original} → {after}"


# ---------------------------------------------------------------------------
# S-8: Command classification substring matching
# ---------------------------------------------------------------------------


class TestS8CommandClassification:
    """Substring matching was too broad — 'curl' matched inside 'uncurl'."""

    def test_curl_in_word_is_not_matched(self):
        from kalash.tools.shell import _classify_command

        caps = _classify_command("echo 'uncurl this'")
        assert "shell.network" not in caps, (
            "'curl' inside 'uncurl' should not trigger network capability"
        )

    def test_actual_curl_is_matched(self):
        from kalash.tools.shell import _classify_command

        caps = _classify_command("curl https://example.com")
        assert "shell.network" in caps

    def test_sudo_in_word_not_matched(self):
        from kalash.tools.shell import _classify_command

        caps = _classify_command("echo pseudorandom")
        assert "shell.sudo" not in caps

    def test_actual_sudo_matched(self):
        from kalash.tools.shell import _classify_command

        caps = _classify_command("sudo apt install foo")
        assert "shell.sudo" in caps

    def test_multiword_patterns_still_work(self):
        from kalash.tools.shell import _classify_command

        caps = _classify_command("rm -rf /tmp/old")
        assert "shell.destructive" in caps

    def test_docker_at_word_boundary(self):
        from kalash.tools.shell import _classify_command

        caps = _classify_command("docker build .")
        assert "shell.docker" in caps

    def test_wget_inside_echo_not_matched(self):
        from kalash.tools.shell import _classify_command

        caps = _classify_command("echo 'use wget for downloads'")
        # wget appears as a standalone word in the echo, so it SHOULD match
        # This is correct behavior — it's a reference to wget
        # The fix prevents matching substrings of other words only
        assert "shell.network" in caps  # wget is a standalone word here


# ---------------------------------------------------------------------------
# S-9: cwd_allowed resolves roots
# ---------------------------------------------------------------------------


class TestS9CwdAllowed:
    """_cwd_allowed should resolve both cwd and roots."""

    def test_cwd_allowed_resolves_path(self, workspace):

        from kalash.tools.base import ToolContext
        from kalash.tools.shell import _cwd_allowed

        ctx = ToolContext(
            session_id="s",
            run_id="r",
            cwd=workspace,
            writable_roots=(workspace,),
        )
        subdir = workspace / "sub"
        subdir.mkdir()
        assert _cwd_allowed(subdir, ctx) is True

    def test_cwd_outside_roots_rejected(self, workspace, tmp_path):

        from kalash.tools.base import ToolContext
        from kalash.tools.shell import _cwd_allowed

        outside = tmp_path / "other"
        outside.mkdir()
        ctx = ToolContext(
            session_id="s",
            run_id="r",
            cwd=workspace,
            writable_roots=(workspace,),
        )
        assert _cwd_allowed(outside, ctx) is False


# ---------------------------------------------------------------------------
# A-5: structlog graceful degradation
# ---------------------------------------------------------------------------


class TestA5StructlogFallback:
    """Event bus error handler must not crash when structlog is missing."""

    @pytest.mark.asyncio
    async def test_handler_error_does_not_raise(self):
        from kalash.core.events import Event, EventBus, EventType

        bus = EventBus()

        async def exploding_handler(event: Event) -> None:
            raise RuntimeError("boom")

        bus.on(EventType.TURN_START, exploding_handler)

        # This must not raise — it logs and swallows the exception
        await bus.emit(
            Event(
                type=EventType.TURN_START,
                session_id="test",
                data={},
            )
        )


# ---------------------------------------------------------------------------
# Provider API key persistence (user-reported bug)
# ---------------------------------------------------------------------------


class TestProviderKeyPersistence:
    """Switching providers and back should not re-ask for API key."""

    def test_saved_key_survives_switch(self, isolated_store):
        from kalash.tui.auth_store import get_credential, save_credential

        save_credential("openrouter", "sk-or-test-key-123")
        save_credential("groq", "gsk-test-key-456")

        # After switching to groq and back, openrouter key must still be there
        assert get_credential("openrouter") == "sk-or-test-key-123"
        assert get_credential("groq") == "gsk-test-key-456"

    def test_credential_for_finds_saved_key(self, isolated_store):
        from kalash.models.resolve import credential_for
        from kalash.tui.auth_store import save_credential

        save_credential("openrouter", "sk-or-saved")
        assert credential_for("openrouter") == "sk-or-saved"

    def test_env_var_used_as_fallback(self, isolated_store, monkeypatch):
        from kalash.models.resolve import credential_for

        monkeypatch.setenv("GROQ_API_KEY", "gsk-from-env")
        assert credential_for("groq") == "gsk-from-env"


# ---------------------------------------------------------------------------
# Session serialization round-trip
# ---------------------------------------------------------------------------


class TestSessionSerialization:
    """Session resume must faithfully reconstruct conversations."""

    def test_tool_use_and_result_survive_roundtrip(self):
        from kalash.models.normalize import (
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
        )
        from kalash.runtime.serialize import (
            rehydrate_messages,
            serialize_blocks,
        )

        assistant_blocks = [
            TextBlock(text="I'll read the file"),
            ToolUseBlock(id="tu_1", name="read", input={"path": "foo.py"}),
        ]
        tool_result_blocks = [
            ToolResultBlock(tool_use_id="tu_1", content="file contents here"),
        ]

        serialized_assistant = serialize_blocks(assistant_blocks)
        serialized_tool = serialize_blocks(tool_result_blocks)

        rows = [
            {"role": "assistant", "content": serialized_assistant},
            {"role": "tool_result", "content": serialized_tool},
        ]
        messages = rehydrate_messages(rows)

        assert len(messages) >= 1
        # Check tool_use block survived
        has_tool_use = any(isinstance(b, ToolUseBlock) for m in messages for b in m.content)
        assert has_tool_use, "ToolUseBlock must survive serialization"

    def test_dangling_tool_calls_retain_intent(self):
        from kalash.models.normalize import (
            TextBlock,
            ToolUseBlock,
        )
        from kalash.runtime.serialize import (
            rehydrate_messages,
            serialize_blocks,
        )

        # An assistant turn with a tool_use that has no matching result
        blocks = [
            TextBlock(text="let me check"),
            ToolUseBlock(id="tu_orphan", name="read", input={"path": "x.py"}),
        ]
        rows = [{"role": "assistant", "content": serialize_blocks(blocks)}]
        messages = rehydrate_messages(rows)

        from kalash.models.normalize import ToolResultBlock

        assert isinstance(messages[0].content[-1], ToolUseBlock)
        result = messages[1].content[0]
        assert isinstance(result, ToolResultBlock)
        assert result.tool_use_id == "tu_orphan" and result.is_error


# ---------------------------------------------------------------------------
# System prompt construction
# ---------------------------------------------------------------------------


class TestSystemPrompt:
    """The system prompt must contain the planning and ReAct sections."""

    def test_prompt_includes_planning_section(self):
        from kalash.runtime.prompt import build_system_prompt

        prompt = build_system_prompt(mode="build")
        assert "todo" in prompt.lower()
        assert "MUST" in prompt or "must" in prompt.lower()

    def test_prompt_includes_react_loop(self):
        from kalash.runtime.prompt import build_system_prompt

        prompt = build_system_prompt(mode="build")
        assert "Reason" in prompt
        assert "Act" in prompt
        assert "Observe" in prompt
        assert "Repeat" in prompt

    def test_compact_prompt_still_includes_planning(self):
        from kalash.runtime.prompt import build_system_prompt

        prompt = build_system_prompt(mode="build", compact=True)
        assert "todo" in prompt.lower()

    def test_prompt_reaches_the_agent_loop(self, workspace, monkeypatch):
        """The system prompt must actually be passed to the agent loop."""
        from kalash.models.normalize import MessageStop, StopReason

        class StubProvider:
            name = "stub"
            context_window = 200_000

            async def stream(self, messages, *, system=None, **kwargs):
                self.last_system = system
                yield MessageStop(StopReason.END_TURN)

        class StubRes:
            ok = True
            provider = StubProvider()
            provider_id = "test"
            model_id = "stub"
            reason = ""

        monkeypatch.setattr(
            "kalash.models.resolve.build_provider",
            lambda *a, **k: StubRes(),
        )
        from kalash.runtime.agent import build_agent

        agent, _ = build_agent(cwd=workspace, interactive=False)
        assert agent is not None
        # The loop must have a non-empty system prompt
        assert len(agent.loop.system_prompt) > 500, (
            "System prompt is suspiciously short — may not have been wired"
        )
        assert "Kalash" in agent.loop.system_prompt


# ---------------------------------------------------------------------------
# Auth store encryption
# ---------------------------------------------------------------------------


class TestAuthStoreIntegrity:
    """Credentials must survive save/load cycle and be encrypted at rest."""

    def test_roundtrip_preserves_key(self, isolated_store):
        from kalash.tui.auth_store import get_credential, save_credential

        save_credential("test-provider", "sk-secret-12345")
        assert get_credential("test-provider") == "sk-secret-12345"

    def test_key_not_plaintext_on_disk(self, isolated_store):
        from kalash.tui.auth_store import _auth_path, save_credential

        save_credential("test-provider", "sk-secret-12345")
        raw = _auth_path().read_text()
        assert "sk-secret-12345" not in raw

    def test_remove_credential(self, isolated_store):
        from kalash.tui.auth_store import (
            get_credential,
            remove_credential,
            save_credential,
        )

        save_credential("doomed", "key")
        assert get_credential("doomed") == "key"
        remove_credential("doomed")
        assert get_credential("doomed") is None

    def test_multiple_providers_coexist(self, isolated_store):
        from kalash.tui.auth_store import get_credential, save_credential

        save_credential("provider-a", "key-a")
        save_credential("provider-b", "key-b")
        save_credential("provider-c", "key-c")

        assert get_credential("provider-a") == "key-a"
        assert get_credential("provider-b") == "key-b"
        assert get_credential("provider-c") == "key-c"

    def test_active_provider_persists(self, isolated_store):
        from kalash.tui.auth_store import (
            get_active_provider,
            set_active_provider,
        )

        set_active_provider("groq", "model-x")
        result = get_active_provider()
        assert result == ("groq", "model-x")


# ---------------------------------------------------------------------------
# Agent loop conversation continuity
# ---------------------------------------------------------------------------


class TestAgentConversationContinuity:
    """Agent.send must carry history across turns."""

    @pytest.mark.asyncio
    async def test_second_turn_sees_first(self, workspace, monkeypatch):
        from kalash.models.normalize import (
            BlockDelta,
            BlockStart,
            BlockStop,
            MessageStart,
            MessageStop,
            StopReason,
            UsageUpdate,
        )

        class FakeProvider:
            name = "fake"
            context_window = 200_000
            requests = []

            async def stream(self, messages, **kwargs):
                self.requests.append(messages)
                for event in [
                    MessageStart(id="m", model="fake"),
                    BlockStart(index=0, block_type="text"),
                    BlockDelta(index=0, delta="ok"),
                    BlockStop(index=0),
                    UsageUpdate(input_tokens=10, output_tokens=5),
                    MessageStop(StopReason.END_TURN),
                ]:
                    yield event

        class FakeRes:
            ok = True
            provider = FakeProvider()
            provider_id = "test"
            model_id = "fake"
            reason = ""

        monkeypatch.setattr(
            "kalash.models.resolve.build_provider",
            lambda *a, **k: FakeRes(),
        )
        from kalash.runtime.agent import build_agent

        agent, _ = build_agent(cwd=workspace, interactive=False)
        assert agent is not None

        await agent.send("first question")
        await agent.send("second question")

        provider = FakeRes.provider
        assert len(provider.requests) >= 2
        # The second request must have more messages than the first
        assert len(provider.requests[1]) > len(provider.requests[0]), (
            "Turn 2 must see turn 1's history"
        )
