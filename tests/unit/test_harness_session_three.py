"""Connected boundaries for the final reliability batch; no paid calls."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from kalash.core.events import Event, EventBus, EventType
from kalash.mcp.client import MCPToolSchema
from kalash.mcp.server import KalashMCPServer, ServerConfig, ServerSecurityError
from kalash.mcp.tools import MCPToolAdapter
from kalash.tools.base import ToolContext
from kalash.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from pathlib import Path


async def test_post_edit_failure_reports_completed_effect(tmp_path: Path) -> None:
    from kalash.hooks.events import HookEvent
    from kalash.tools.fs.write import WriteParams, WriteTool

    class BrokenPostHook:
        async def dispatch(self, payload):
            if payload.event == HookEvent.POST_FILE_EDIT:
                raise RuntimeError("post hook failed")
            return []

    bus = EventBus()
    events = []

    async def record(event: Event) -> None:
        events.append(event)

    bus.on(EventType.HOOK_RUN, record)
    result = await WriteTool().execute(
        WriteParams(path="created.txt", content="committed"),
        ToolContext(
            session_id="fixture",
            run_id="run",
            cwd=tmp_path,
            writable_roots=(tmp_path,),
            hooks=BrokenPostHook(),
            event_bus=bus,
        ),
    )
    assert result.ok
    assert (tmp_path / "created.txt").read_text() == "committed"
    assert result.side_effects
    assert result.metadata["hook_warnings"]
    assert events[0].data["effect_completed"] is True
    assert events[0].data["status"] == "failed"


def test_changed_trusted_hook_configuration_cannot_disable_policy(tmp_path: Path) -> None:
    from kalash.core.errors import HookDeniedError
    from kalash.core.trust import trust_file
    from kalash.hooks.load import build_hook_runner

    path = tmp_path / ".kalash/hooks/policy.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"hooks": [{"required": true, "trigger": "PreToolUse", "action": {"command": "true"}}]}'
    )
    trust_file(path)
    path.write_text("{ malformed")
    with pytest.raises(HookDeniedError, match="changed"):
        build_hook_runner(tmp_path)


def test_deleted_trusted_hook_requires_explicit_revocation(tmp_path: Path) -> None:
    from kalash.core.errors import HookDeniedError
    from kalash.core.trust import trust_file
    from kalash.hooks.load import build_hook_runner

    path = tmp_path / ".kalash/hooks/policy.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"hooks": [{"required": true, "trigger": "PreToolUse", "action": {"command": "true"}}]}'
    )
    trust_file(path)
    path.unlink()
    with pytest.raises(HookDeniedError, match="missing"):
        build_hook_runner(tmp_path)
    from typer.testing import CliRunner

    from kalash.cli.hooks_cmd import hooks_app

    result = CliRunner().invoke(hooks_app, ["untrust", str(path)])
    assert result.exit_code == 0, result.output
    assert "revoked" in result.output
    assert build_hook_runner(tmp_path) is None


@pytest.mark.parametrize("invalid", ["{ invalid", "[]", '{"relative-path": "bad-digest"}'])
def test_corrupt_authorization_registry_cannot_silently_remove_hook_policy(
    tmp_path: Path, invalid: str
) -> None:
    from kalash.core.errors import HookDeniedError
    from kalash.core.paths import kalash_home
    from kalash.core.trust import trust_file
    from kalash.hooks.load import build_hook_runner

    path = tmp_path / ".kalash/hooks/policy.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"hooks": []}')
    trust_file(path)
    registry = kalash_home() / "trusted-files.json"
    registry.write_text(invalid)
    with pytest.raises(HookDeniedError, match="authorization registry"):
        build_hook_runner(tmp_path)
    assert registry.read_text() == invalid


async def test_tools_cannot_rewrite_user_authorization_registry(tmp_path: Path) -> None:
    from kalash.core.paths import kalash_home
    from kalash.core.trust import trust_file
    from kalash.tools.fs.common import _content_digest
    from kalash.tools.fs.write import WriteParams, WriteTool

    policy = tmp_path / "policy.json"
    policy.write_text("{}")
    trust_file(policy)
    registry = kalash_home() / "trusted-files.json"
    before = registry.read_bytes()
    result = await WriteTool().execute(
        WriteParams(path=str(registry), content="{}", digest=_content_digest(before)),
        ToolContext(session_id="fixture", run_id="run", cwd=tmp_path, writable_roots=(tmp_path,)),
    )
    assert not result.ok
    assert result.error.code == "KALASH_SANDBOX_PATH_PROTECTED"
    assert registry.read_bytes() == before
    from kalash.sandbox.policy import protected_existing_paths

    assert registry in protected_existing_paths([tmp_path])


def test_required_hook_runner_initialization_cannot_fail_open(tmp_path: Path, monkeypatch) -> None:
    from kalash.core.errors import HookDeniedError
    from kalash.core.trust import trust_file
    from kalash.hooks.load import build_hook_runner

    path = tmp_path / ".kalash/hooks/policy.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"hooks": [{"required": true, "trigger": "PreToolUse", "action": {"command": "true"}}]}'
    )
    trust_file(path)

    def unavailable():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("kalash.storage.engine.get_engine", unavailable)
    with pytest.raises(HookDeniedError, match="initialise"):
        build_hook_runner(tmp_path)


@pytest.mark.parametrize(
    "arguments",
    [
        {"rows": [{"count": "2"}]},
        {"rows": [{"count": -1}]},
        {"rows": [{"count": 2, "extra": True}]},
        {"rows": [], "extra": 1},
    ],
)
async def test_mcp_nested_schema_failure_never_reaches_remote(
    tmp_path: Path, arguments: dict
) -> None:
    manager = AsyncMock()
    tool = MCPToolAdapter(
        MCPToolSchema(
            name="read",
            namespaced_name="mcp__fixture__read",
            server_name="fixture",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["rows"],
                "properties": {
                    "rows": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["count"],
                            "properties": {"count": {"type": "integer", "minimum": 0}},
                        },
                    }
                },
            },
        ),
        manager,
    )
    registry = ToolRegistry()
    registry.register(tool, source="mcp")
    ctx = ToolContext(
        session_id="fixture",
        run_id="run",
        cwd=tmp_path,
        capabilities=tool.capabilities,
        allow_network=True,
    )
    result = await registry.dispatch(tool.name, arguments, ctx)
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "KALASH_TOOL_INVALID_ARGS"
    manager.call_tool.assert_not_awaited()


@pytest.mark.parametrize("host", ["::", "192.0.2.1", "example.test"])
def test_mcp_nonloopback_bind_requires_explicit_authority(host: str) -> None:
    with pytest.raises(ServerSecurityError):
        ServerConfig(host=host)


class MemoryWriter:
    def __init__(self) -> None:
        self.output = bytearray()

    def write(self, data: bytes) -> None:
        self.output.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


async def test_mcp_authentication_does_not_wait_for_declared_body() -> None:
    server = KalashMCPServer(ServerConfig(auth_token="synthetic-test-value"))  # noqa: S106 - Synthetic test credential.
    server._running = True  # noqa: SLF001 - exercise actual HTTP connection boundary.
    reader = asyncio.StreamReader()
    writer = MemoryWriter()
    reader.feed_data(b"POST /mcp HTTP/1.1\r\nContent-Length: 100000000\r\n\r\n")
    await asyncio.wait_for(server._handle_connection(reader, writer), 0.15)  # noqa: SLF001
    assert b"401 Unauthorized" in writer.output


async def test_mcp_valid_notification_has_no_jsonrpc_reply() -> None:
    server = KalashMCPServer()
    result = await server._handle_jsonrpc(  # noqa: SLF001 - protocol boundary.
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert result is None


async def test_slow_presentation_handler_does_not_block_runtime() -> None:
    bus = EventBus()
    release = asyncio.Event()
    received = []

    async def slow(event: Event) -> None:
        await release.wait()
        received.append(event.data["n"])

    bus.on_presentation(EventType.TOOL_OUTPUT, slow)
    try:
        for n in range(10):
            await asyncio.wait_for(bus.emit(Event(EventType.TOOL_OUTPUT, {"n": n})), 0.1)
        release.set()
        await bus.drain_presentation()
        assert received == list(range(10))
    finally:
        await bus.close_presentation()


async def test_python_hook_obeys_its_deadline(tmp_path: Path) -> None:
    from kalash.hooks.events import HookEvent, HookPayload
    from kalash.hooks.runner import HookRunner
    from kalash.storage.engine import StorageEngine

    engine = StorageEngine(tmp_path / "hooks.db")
    await engine.initialize()
    runner = HookRunner(engine)

    async def never(_payload):
        await asyncio.Event().wait()

    runner.register_python("timeout", HookEvent.PRE_TOOL_USE, never)
    config = runner._hooks[HookEvent.PRE_TOOL_USE][0]  # noqa: SLF001 - fault fixture.
    config.timeout_s = 0.01
    try:
        results = await asyncio.wait_for(runner.dispatch(HookPayload(HookEvent.PRE_TOOL_USE)), 0.2)
        assert results[0].error is not None
        assert "timed out" in results[0].error
    finally:
        engine.close()


async def test_sustained_transcript_evicts_widgets_without_losing_export() -> None:
    from kalash.tui.app import KalashApp
    from kalash.tui.messages import SubagentLine, SystemMessage

    app = KalashApp()
    async with app.run_test(size=(52, 18)) as pilot:
        transcript = app._transcript()  # noqa: SLF001 - mounted UI integration.
        transcript.MAX_WIDGETS = 12
        for n in range(90):
            await app._post(SystemMessage(f"entry-{n:03d} हिंदी 🙂"))  # noqa: SLF001
            if n == 0:
                child = SubagentLine("archive this completed child")
                child.finish(turns=2, tokens=123)
                await app._post(child)  # noqa: SLF001 - mounted UI integration.
        await pilot.resize_terminal(100, 30)
        await pilot.pause()
        assert len(transcript.children) <= 12
        text = app._transcript_text()  # noqa: SLF001
        assert text.count("हिंदी 🙂") == 90
        assert text.index("entry-000") < text.index("entry-089")
        assert "archive this completed child" in text
        assert "2 turns, 123 tok" in text
        assert not transcript.archive_error


async def test_required_hook_failure_blocks_and_records_fresh_schema(tmp_path: Path) -> None:
    from kalash.core.errors import HookDeniedError
    from kalash.hooks.events import HookEvent, HookPayload
    from kalash.hooks.runner import HookRunner
    from kalash.storage.engine import StorageEngine

    engine = StorageEngine(tmp_path / "required-hooks.db")
    await engine.initialize()
    runner = HookRunner(engine)

    async def broken(_payload):
        return 1

    runner.register_python("required", HookEvent.PRE_TOOL_USE, broken, required=True)
    try:
        with pytest.raises(HookDeniedError, match="blocked"):
            await runner.dispatch(HookPayload(HookEvent.PRE_TOOL_USE))
        rows = engine.execute_read("SELECT status, output FROM hook_runs")
        assert rows[0]["status"] == "blocked"
        assert '"blocked": true' in rows[0]["output"]
    finally:
        engine.close()


async def test_sdk_and_cli_share_run_attempt_event_projection(tmp_path: Path) -> None:
    import io
    import json

    from kalash.cli.output import OutputFormat, OutputSink
    from kalash.core.config import KalashConfig, MemoryConfig
    from kalash.runtime.agent import build_agent
    from kalash.sdk import KalashClient, KalashSession
    from tests.unit.test_harness_recovery import Provider

    agent, reason = build_agent(
        cwd=tmp_path,
        provider=Provider(),
        persist=False,
        extensions=False,
        config=KalashConfig(memory=MemoryConfig(enabled=False)),
    )
    assert agent is not None, reason
    try:
        response = await KalashSession(agent, KalashClient()).complete("answer")
        assert response.run_id.startswith("run_")
        assert all(event["run_id"] == response.run_id for event in response.events)
        assert [e["seq"] for e in response.events] == sorted(e["seq"] for e in response.events)
        requests = [e for e in response.events if e["event"] == "model.request"]
        assert requests[0]["data"]["dispatch_id"]
        assert response.usage["input_tokens"] == 120
        output = io.StringIO()
        sink = OutputSink(OutputFormat.STREAM_JSON, stdout=output)
        event = Event(
            EventType.MODEL_REQUEST,
            requests[0]["data"],
            session_id=agent.session_id,
            run_id=response.run_id,
        )
        sink.emit_runtime_event(event)
        cli = json.loads(output.getvalue())
        assert cli["data"] == requests[0]["data"]
        assert cli["run_id"] == response.run_id
    finally:
        await agent.close()


async def test_sdk_usage_survives_event_collector_exhaustion(tmp_path: Path, monkeypatch) -> None:
    from kalash.core.budget import Usage
    from kalash.core.config import KalashConfig, MemoryConfig
    from kalash.runtime.agent import build_agent
    from kalash.runtime.loop import LoopResult, TerminationReason
    from kalash.sdk import KalashClient, KalashSession
    from tests.unit.test_harness_recovery import Provider

    agent, reason = build_agent(
        cwd=tmp_path,
        provider=Provider(),
        persist=False,
        extensions=False,
        config=KalashConfig(memory=MemoryConfig(enabled=False)),
    )
    assert agent is not None, reason

    async def settled_after_queue_fills(_prompt, **_kwargs):
        # Cancellation can arrive after a physical request has already settled.
        # Its real usage must not disappear with a bounded event projection.
        for _ in range(2001):
            await agent.loop.event_bus.emit(Event(EventType.NOTIFICATION))
        monkeypatch.setattr(
            type(agent.loop.gateway),
            "total_usage",
            property(lambda _self: Usage(input_tokens=123, output_tokens=7)),
        )
        return LoopResult(TerminationReason.USER_INTERRUPT, call_tokens=130, run_id="run-overflow")

    monkeypatch.setattr(agent, "send", settled_after_queue_fills)
    try:
        response = await KalashSession(agent, KalashClient()).complete("fixture")
        assert "incomplete" in response.error
        assert response.usage["input_tokens"] == 123
        assert response.usage["output_tokens"] == 7
        assert response.tokens_used == 130
    finally:
        await agent.close()


async def test_real_mcp_http_peer_checks_arguments_and_closes() -> None:
    from kalash.mcp.client import MCPClient, MCPServerConfig, TransportType

    server = KalashMCPServer(ServerConfig(port=0))
    calls = []

    async def echo(count):
        calls.append(count)
        return {"count": count}

    server.register_tool(
        "echo",
        echo,
        input_schema={
            "type": "object",
            "properties": {"count": {"type": "integer", "minimum": 0}},
            "required": ["count"],
            "additionalProperties": False,
        },
    )
    await server.start()
    client = MCPClient(
        MCPServerConfig(
            name="peer",
            transport=TransportType.HTTP,
            url=server.endpoint,
            headers={"Authorization": f"Bearer {server.auth_token}"},
        )
    )
    try:
        await client.connect()
        result = await client.call_tool("echo", {"count": 2})
        assert not result.get("isError")
        invalid = await client.call_tool("echo", {"count": "2"})
        assert invalid["isError"]
        assert calls == [2]
    finally:
        await client.disconnect()
        await server.stop()


async def test_first_full_openai_identity_is_not_emitted_twice() -> None:
    from kalash.models.providers.openai import OpenAIProvider
    from kalash.runtime.stream import StreamHandler
    from tests.unit.test_provider_interruptions import wire_chunk, wire_provider, wire_usage

    wire, _ = wire_provider(
        [
            wire_chunk(
                delta={
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-unique",
                            "function": {"name": "read", "arguments": '{"path":"source.py"}'},
                        }
                    ]
                },
                finish_reason="tool_calls",
            ),
            wire_chunk(usage=wire_usage(), choices=False),
        ]
    )
    provider = OpenAIProvider(api_key="fixture")
    provider._client = wire._client  # noqa: SLF001 - Exact provider-wire fixture.
    handler = StreamHandler()
    try:
        async for event in provider.stream([]):
            handler.feed(event)
    finally:
        await provider.close()
    assert [(b.id, b.name) for b in handler.result().tool_calls] == [("call-unique", "read")]


def test_completed_large_exchange_is_retrievable_and_pending_calls_stay_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kalash.models.normalize import Message, Role, TextBlock, ToolResultBlock, ToolUseBlock
    from kalash.runtime.history import reference_completed_exchanges
    from kalash.runtime.scratchpad import get_scratchpad

    monkeypatch.setenv("KALASH_HOME", str(tmp_path))
    instruction = Message(Role.USER, [TextBlock("Change source files only")])
    call = Message(Role.ASSISTANT, [ToolUseBlock("settled", "write", {"content": "x" * 9000})])
    receipt = Message(Role.USER, [ToolResultBlock("settled", "written", evidence={"ok": True})])
    tail = [
        Message(Role.ASSISTANT, [TextBlock("continuing")]),
        Message(Role.ASSISTANT, [ToolUseBlock("pending", "read", {"path": "x"})]),
        Message(Role.USER, [TextBlock("latest constraints")]),
    ]
    original = [instruction, call, receipt, *tail]
    reduced = reference_completed_exchanges(original, "reference-regression")
    assert reduced[0] is instruction
    assert reduced[-3:] == tail
    assert len(reduced) == 5
    summary = reduced[1].content[0].text
    assert '"ok": true' in summary
    ref = summary.split("expand(")[1].split(",")[0]
    assert "x" * 9000 in get_scratchpad("reference-regression").expand(ref, max_bytes=16000).content
    assert len(call.content[0].input["content"]) == 9000
