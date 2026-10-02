"""Wire and runtime regression tests for live benchmark invariants."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from kalash.core.budget import BudgetState, Pricing, Usage
from kalash.models.normalize import (
    Message,
    MessageStop,
    Role,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from kalash.models.providers.sarvam import SarvamProvider
from kalash.runtime.history import split_history
from kalash.runtime.loop import TerminationReason
from kalash.runtime.stream import StreamHandler
from tests.unit.test_agent_loop import (
    AutoApprover,
    FakeGateway,
    make_host,
    make_loop,
    send,
    text_turn,
    tool_turn,
)


class SDKStream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def close(self):
        self.closed = True


async def test_sarvam_reasoning_usage_tail_and_wire_roundtrip():
    stream = SDKStream(
        [
            NS(
                choices=[
                    NS(
                        delta=NS(reasoning_content="inspect source", content=None, tool_calls=None),
                        finish_reason=None,
                    )
                ]
            ),
            NS(
                choices=[
                    NS(
                        delta=NS(
                            content=None,
                            tool_calls=[
                                NS(
                                    index=0,
                                    id="call",
                                    function=NS(name="read", arguments='{"path":"src/a.py"}'),
                                )
                            ],
                        ),
                        finish_reason="tool_calls",
                    )
                ]
            ),
            NS(
                choices=[],
                usage=NS(
                    prompt_tokens=100,
                    completion_tokens=30,
                    prompt_cache_hit_tokens=80,
                    completion_tokens_details=NS(reasoning_tokens=20),
                ),
            ),
        ]
    )
    create = AsyncMock(return_value=stream)
    provider = SarvamProvider(model="sarvam-105b")
    provider._client = NS(chat=NS(completions=NS(create=create)))
    handler = StreamHandler()
    events = [
        event
        async for event in provider.stream(
            [Message(role=Role.USER, content=[TextBlock(text="fix")])]
        )
    ]
    for event in events:
        handler.feed(event)
    result = handler.result()
    assert stream.closed
    assert isinstance(events[-1], MessageStop)
    assert (
        result.input_tokens,
        result.output_tokens,
        result.cache_read_tokens,
        result.reasoning_tokens,
    ) == (100, 30, 80, 20)
    assert result.tool_calls[0].input == {"path": "src/a.py"}
    wire = provider.serialize_messages(
        [
            Message(role=Role.ASSISTANT, content=result.content),
            Message(
                role=Role.USER, content=[ToolResultBlock(tool_use_id="call", content="source")]
            ),
        ],
        None,
    )
    assert wire[0]["reasoning_content"] == "inspect source"
    assert wire[1]["tool_call_id"] == "call"
    assert create.call_args.kwargs["reasoning_effort"] == "max"


async def test_sarvam_complete_preserves_sdk_reasoning_and_usage():
    response = NS(
        id="response",
        model="sarvam-105b",
        choices=[
            NS(
                message=NS(content="done", reasoning_content="opaque reasoning", tool_calls=[]),
                finish_reason="stop",
            )
        ],
        usage=NS(
            prompt_tokens=10,
            completion_tokens=4,
            prompt_tokens_details=NS(cached_tokens=3),
            completion_tokens_details=NS(reasoning_tokens=2),
        ),
    )
    provider = SarvamProvider(model="sarvam-105b")
    provider._client = NS(chat=NS(completions=NS(create=AsyncMock(return_value=response))))
    result = await provider.complete([])
    assert any(
        isinstance(block, ThinkingBlock) and block.thinking == "opaque reasoning"
        for block in result.content
    )
    assert result.usage.cache_read_tokens == 3
    assert result.usage.reasoning_tokens == 2


def test_cached_input_is_not_charged_twice():
    budget = BudgetState()
    budget.record_usage(
        Usage(input_tokens=100, output_tokens=20, cache_read_tokens=80),
        Pricing(
            input_per_mtok=Decimal(10), output_per_mtok=Decimal(20), cache_read_per_mtok=Decimal(1)
        ),
    )
    assert budget.cost_used == Decimal("0.00068")


def test_history_split_retains_complete_tool_batch():
    messages = [Message(role=Role.USER, content=[TextBlock(text="goal")])]
    for index in range(2):
        messages.extend(
            [
                Message(
                    role=Role.ASSISTANT,
                    content=[ToolUseBlock(id=str(index), name="read", input={})],
                ),
                Message(
                    role=Role.USER,
                    content=[ToolResultBlock(tool_use_id=str(index), content="result")],
                ),
            ]
        )
    head, tail = split_history(messages)
    assert len(head) == 1
    assert isinstance(tail[0].content[0], ToolUseBlock)


async def test_cancel_interrupts_stalled_provider_and_closes_stream(tmp_path):
    closed = asyncio.Event()

    class Stalled(FakeGateway):
        async def stream(self, *args, **kwargs):
            try:
                await asyncio.Event().wait()
                yield MessageStop(StopReason.END_TURN)
            finally:
                closed.set()

    loop = make_loop(Stalled([]), make_host(tmp_path))
    run = asyncio.create_task(send(loop, "fix"))
    await asyncio.sleep(0.01)
    loop.cancel()
    result = await asyncio.wait_for(run, 1)
    assert result.termination_reason == TerminationReason.USER_INTERRUPT
    assert closed.is_set()


async def test_wallclock_bounds_provider_wait(tmp_path):
    class Stalled(FakeGateway):
        async def stream(self, *args, **kwargs):
            await asyncio.Event().wait()
            yield MessageStop(StopReason.END_TURN)

    loop = make_loop(Stalled([]), make_host(tmp_path))
    loop.budget.max_wallclock_s = 0.02
    result = await send(loop, "fix")
    assert result.termination_reason == TerminationReason.BUDGET_EXHAUSTED
    assert loop.budget.wallclock_used_s >= 0.02


async def test_error_flag_and_attempt_budget_survive_host(tmp_path):
    gateway = FakeGateway([tool_turn("read", {"path": "missing"}), text_turn("done")])
    loop = make_loop(gateway, make_host(tmp_path, ui=AutoApprover()))
    await send(loop, "read")
    results = [
        block
        for message in loop.conversation
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]
    assert results[0].is_error
    assert loop.budget.tool_calls_used == 1


async def test_mutation_precedes_following_read(tmp_path):
    host = make_host(tmp_path, ui=AutoApprover())
    loop = make_loop(FakeGateway([]), host)
    results = await loop._execute_tools(
        [
            ToolUseBlock(id="write", name="write", input={"path": "a.txt", "content": "new"}),
            ToolUseBlock(id="read", name="read", input={"path": "a.txt"}),
        ]
    )
    assert not results[1].is_error
    assert "new" in results[1].content


async def test_tool_limit_stops_later_calls(tmp_path):
    loop = make_loop(FakeGateway([]), make_host(tmp_path, ui=AutoApprover()))
    loop.budget.max_tool_calls = 1
    results = await loop._execute_tools(
        [
            ToolUseBlock(
                id=str(index), name="write", input={"path": f"{index}.txt", "content": "new"}
            )
            for index in range(2)
        ]
    )
    assert (tmp_path / "0.txt").exists()
    assert not (tmp_path / "1.txt").exists()
    assert results[1].is_error
    assert loop.budget.tool_calls_used == 1


async def test_sdk_stream_closes_if_consumer_stops_at_start():
    stream = SDKStream([])
    provider = SarvamProvider(model="sarvam-105b")
    provider._client = NS(chat=NS(completions=NS(create=AsyncMock(return_value=stream))))
    iterator = provider.stream([])
    await anext(iterator)
    await iterator.aclose()
    assert stream.closed


async def test_cancelled_tool_exchange_has_error_result(tmp_path):
    started = asyncio.Event()
    host = make_host(tmp_path)

    async def stalled(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    host.execute_result = stalled
    loop = make_loop(FakeGateway([tool_turn("read", {"path": "a.py"})]), host)
    run = asyncio.create_task(send(loop, "read"))
    await started.wait()
    loop.cancel()
    result = await asyncio.wait_for(run, 1)
    assert result.termination_reason == TerminationReason.USER_INTERRUPT
    blocks = [block for message in loop.conversation for block in message.content]
    assert any(
        isinstance(block, ToolResultBlock) and block.tool_use_id == "tu_1" and block.is_error
        for block in blocks
    )


async def test_child_inherits_tools_mode_extensions_and_accounts_usage(tmp_path):
    from kalash.core.config import KalashConfig, MemoryConfig
    from kalash.runtime.agent import build_agent
    from tests.unit.test_wiring import FakeProvider, final

    provider = FakeProvider([final("child findings")])
    agent, reason = build_agent(
        cwd=tmp_path,
        provider=provider,
        mode="plan",
        config=KalashConfig(memory=MemoryConfig(enabled=False)),
        persist=False,
        extensions=False,
        allowed_tools=frozenset({"read"}),
    )
    assert agent is not None, reason
    outcome = await agent.host.delegate(
        prompt="inspect", mode="build", allowed_tools=frozenset({"read", "write"})
    )
    assert outcome["status"] == "completed"
    assert {tool["name"] for tool in provider.requests[0]["tools"]} == {"read"}
    assert "Mode: PLAN" in provider.requests[0]["system"]
    assert "skills" not in provider.requests[0]["system"]
    assert agent.budget.tokens_used == 110
    assert agent.budget.tokens_reserved == 0
    assert not agent.history


async def test_capability_subset_is_enforced_at_dispatch(tmp_path):
    from kalash.core.events import EventBus
    from kalash.runtime.toolhost import ToolHost
    from kalash.tools.builtins import default_registry

    host = ToolHost(
        default_registry(), EventBus(), "s", tmp_path, capability_limit=frozenset({"fs.read"})
    )
    result = await host.execute_result("write", {"path": "a.py", "content": "bad"}, tool_use_id="x")
    assert result.is_error
    assert not (tmp_path / "a.py").exists()
    assert "write" not in {item["name"] for item in host.schemas()}


async def test_nested_instructions_block_first_write_and_survive_history_reduction(tmp_path):
    from kalash.core.events import EventBus
    from kalash.runtime.instructions import InstructionLoader
    from kalash.runtime.toolhost import ToolHost
    from kalash.tools.builtins import default_registry

    folder = tmp_path / "package"
    folder.mkdir()
    (folder / "AGENTS.md").write_text("Use exact integer arithmetic.")
    host = ToolHost(
        default_registry(),
        EventBus(),
        "s",
        tmp_path,
        instruction_loader=InstructionLoader(tmp_path),
    )
    result = await host.execute_result(
        "write", {"path": "package/a.py", "content": "x=1"}, tool_use_id="x"
    )
    assert result.is_error
    assert not (folder / "a.py").exists()
    assert "integer arithmetic" in result.content
    assert len(host.instruction_loader.nested) == 1
    result = await host.execute_result(
        "write", {"path": "package/a.py", "content": "x=1"}, tool_use_id="y"
    )
    assert not result.is_error
    assert (folder / "a.py").read_text() == "x=1"


async def test_compaction_is_charged_and_preserves_user_constraints(tmp_path):
    host = make_host(tmp_path)
    gateway = FakeGateway([text_turn("## Remaining work\nRun the checks.")])
    loop = make_loop(gateway, host)
    loop._conversation = [Message(Role.USER, [TextBlock(text="Never edit legacy/")])]
    for index in range(8):
        loop._conversation.extend(
            [
                Message(Role.ASSISTANT, [TextBlock(text=f"Observed src/{index}.py")]),
                Message(Role.USER, [TextBlock(text="Keep changes compatible.")]),
            ]
        )
    before = loop.budget.tokens_used
    await loop._ensure_context_fits("system", [], prompt_tokens=loop.assembler.context_window)
    assert loop.budget.tokens_used > before
    assert loop.budget.turns_used == 1
    assert loop._conversation[0].content[0].text == "Never edit legacy/"
    assert all(
        any(block.text == "Keep changes compatible." for block in message.content)
        for message in loop._conversation[1:-3]
    )
    assert "Remaining work" in loop._compacted_summary


async def test_sarvam_sdk_uses_v2_subscription_key_and_full_reasoning():
    import json

    import httpx
    import openai

    observed = []

    def server(request):
        observed.append(request)
        payload = json.loads(request.content)
        assert request.url.path == "/v2/chat/completions"
        assert request.headers["api-subscription-key"] == "test-only-key"
        assert payload["reasoning_effort"] == "max"
        assert payload["max_tokens"] == 32_768
        assert payload["model"] == "glm5.3"
        return httpx.Response(
            200,
            json={
                "id": "m",
                "object": "chat.completion",
                "created": 0,
                "model": "glm5.3",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
        )

    provider = SarvamProvider(model="glm5.3", api_key="test-only-key")
    client = provider._get_client()
    await client.close()
    provider._client = openai.AsyncOpenAI(
        api_key="test-only-key",
        base_url=provider._base_url,
        default_headers={"api-subscription-key": "test-only-key"},
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
    )
    try:
        result = await provider.complete([Message(Role.USER, [TextBlock(text="inspect")])])
        assert result.content[0].text == "done"
        assert provider.capabilities.context_window == 1_048_576
        assert provider.capabilities.max_output_tokens > 32_768
    finally:
        await provider.close()


async def test_stop_hook_blocks_completion_and_model_can_address_feedback(tmp_path):
    from kalash.core.errors import HookDeniedError
    from kalash.hooks.events import HookEvent

    class Hooks:
        blocked = False

        async def dispatch(self, payload):
            if payload.event == HookEvent.STOP and not self.blocked:
                self.blocked = True
                raise HookDeniedError("Verification is missing", recoverable=True)
            return []

    gateway = FakeGateway([text_turn("premature completion"), text_turn("addressed verification")])
    loop = make_loop(gateway, make_host(tmp_path))
    loop.hooks = Hooks()
    result = await send(loop, "do the work")
    assert result.termination_reason == TerminationReason.NO_TOOL_CALLS
    assert result.iterations == 2
    assert "premature completion" not in result.final_response
    assert "addressed verification" in result.final_response
    assert any(
        isinstance(block, TextBlock) and "Verification is missing" in block.text
        for message in loop.conversation
        for block in message.content
    )


async def test_loaded_skill_body_is_retained_after_compaction(tmp_path):
    from kalash.core.events import EventBus
    from kalash.runtime.agent import build_agent
    from tests.unit.test_wiring import FakeProvider

    directory = tmp_path / ".kalash" / "skills" / "review"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review work\n---\nKeep all edits inside src/.\n"
    )
    agent, reason = build_agent(
        cwd=tmp_path, provider=FakeProvider([]), persist=False, event_bus=EventBus()
    )
    assert agent is not None, reason
    outcome = await agent.host.execute_result("skill", {"name": "review"}, tool_use_id="s")
    assert not outcome.is_error
    body = agent.host.loaded_skills["review"]
    assembler = agent.loop.assembler
    assembled = await assembler.assemble(
        system_identity=agent.loop.system_prompt,
        tool_schemas=agent.host.schemas(),
        skills_catalog=list(agent.skills),
        kalash_md_chain=[],
        memory_blocks=[],
        recent_turns=[],
        current_message=Message(Role.USER, [TextBlock(text="continue")]),
        compacted_summary="Earlier history reduced.",
        active_skills=[body],
    )
    assert any(
        isinstance(block, TextBlock) and "Keep all edits inside src/." in block.text
        for message in assembled
        for block in message.content
    )
    assert "Keep all edits inside src/." not in assembled[0].content[0].text


async def test_network_retrieval_cannot_bypass_explicit_deny(tmp_path):
    from kalash.permissions.policy import DenyRule

    host = make_host(tmp_path)
    host.policy.deny_rules.append(DenyRule(pattern="fetch", reason="explicit denial"))
    result = await host.execute_result("fetch", {"url": "https://example.com"}, tool_use_id="f")
    assert result.is_error
    assert "explicit denial" in result.content


async def test_never_approval_mode_denies_instead_of_prompting():
    from kalash.core.events import EventBus
    from kalash.permissions.policy import Decision, PermissionPolicy, PolicyRequest, RiskClass

    policy = PermissionPolicy(EventBus(), approval_mode="never")
    outcome = await policy.evaluate(
        PolicyRequest(tool_name="shell", risk_class=RiskClass.DESTRUCTIVE)
    )
    assert outcome.decision is Decision.DENY
    policy.approval_mode = "untrusted"
    outcome = await policy.evaluate(PolicyRequest(tool_name="write", risk_class=RiskClass.WRITE))
    assert outcome.decision is Decision.ASK


async def test_memory_tools_use_owning_configuration(tmp_path):
    from kalash.core.config import KalashConfig, MemoryConfig

    host = make_host(tmp_path)
    host.config = KalashConfig(memory=MemoryConfig(enabled=False))
    result = await host.execute_result("recall", {"query": "anything"}, tool_use_id="memory")
    assert result.is_error
    assert "disabled" in result.content


def test_oversized_observation_retains_retrievable_body(tmp_path):
    from kalash.runtime.scratchpad import get_scratchpad

    host = make_host(tmp_path)
    body = "λ" * 50_000 + "\nimportant tail"
    rendered = host._bound_observation("mcp__large", body, 4096)
    assert len(rendered.encode("utf-8")) <= 4096
    assert "Full result: expand(" in rendered
    ref = rendered.split("expand(", 1)[1].split(",", 1)[0]
    stored = get_scratchpad(host.session_id).expand(ref, grep="important tail")
    assert stored.ok
    assert "important tail" in stored.content


def test_root_guidance_has_source_scope_and_precedence(tmp_path):
    from kalash.runtime.instructions import (
        _render_project_instructions,
        discover_project_instructions,
    )

    (tmp_path / "AGENTS.md").write_text("Use one convention.")
    (tmp_path / "KALASH.md").write_text("Use the native convention.")
    rendered = _render_project_instructions(discover_project_instructions(tmp_path))
    assert f"Scope: {tmp_path}" in rendered
    assert rendered.index("Use one convention.") < rendered.index("Use the native convention.")
    assert "current user's explicit request" in rendered
    assert "KALASH.md takes precedence over AGENTS.md" in rendered


async def test_stdio_eof_fails_pending_requests_immediately():
    from kalash.mcp.client import MCPClient, MCPConnectionError, MCPServerConfig

    client = MCPClient(MCPServerConfig(name="dead"))
    client._connected = True
    client._stdout = asyncio.StreamReader()
    client._stdout.feed_eof()
    future = asyncio.get_running_loop().create_future()
    client._pending[1] = future
    await client._read_loop()
    assert not client.is_connected
    assert not client._pending
    try:
        await future
    except MCPConnectionError:
        pass
    else:
        raise AssertionError("EOF left an unfinished request")


async def test_legacy_sse_waits_for_endpoint_then_initializes(monkeypatch):
    from kalash.mcp.client import MCPClient, MCPServerConfig, TransportType

    client = MCPClient(
        MCPServerConfig(name="legacy", transport=TransportType.SSE, url="https://example.com/sse")
    )
    http = NS(aclose=AsyncMock())
    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: http)

    async def read():
        client._post_endpoint = "https://example.com/messages"
        client._endpoint_ready.set()

    client._sse_read_loop = read
    client._send_request = AsyncMock(return_value={"protocolVersion": "2024-11-05"})
    client._send_notification = AsyncMock()
    await client._connect_sse()
    assert client._send_request.call_args.args[0] == "initialize"
    assert client._post_endpoint.endswith("/messages")
    client._send_notification.assert_awaited_once_with("notifications/initialized", {})
    await client.disconnect()


async def test_300_step_run_compacts_without_losing_constraints_or_tool_pairs(tmp_path):
    (tmp_path / "observations.txt").write_text("observation detail " * 250 + "\n")
    constraint = "Keep all changes inside src/ and preserve public APIs."

    class LongGateway(FakeGateway):
        def __init__(self):
            super().__init__([])
            self.steps = 0
            self.summaries = 0

        async def stream(self, messages, *, system=None, tools=None, **kwargs):
            if tools is None:
                self.summaries += 1
                events = text_turn("## Goal\nInspect observations.\n## Remaining work\nContinue.")
            else:
                assert any(
                    isinstance(block, TextBlock) and constraint in block.text
                    for message in messages
                    for block in message.content
                )
                pending = set()
                for message in messages:
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            assert block.id not in pending
                            pending.add(block.id)
                        elif isinstance(block, ToolResultBlock):
                            assert block.tool_use_id in pending
                            pending.remove(block.tool_use_id)
                assert not pending
                assert kwargs["max_tokens"] > 0
                self.steps += 1
                events = (
                    tool_turn("read", {"path": "observations.txt"}, call_id=f"read-{self.steps}")
                    if self.steps < 300
                    else text_turn("Inspection complete.")
                )
            for event in events:
                yield event

    gateway = LongGateway()
    loop = make_loop(gateway, make_host(tmp_path))
    loop.max_iterations = 350
    loop.budget.max_turns = 500
    loop.budget.max_tokens = 2_000_000
    loop.assembler.context_window = 32_000
    loop.max_output_tokens = 1024
    result = await send(loop, constraint)
    assert result.termination_reason is TerminationReason.NO_TOOL_CALLS
    assert gateway.steps == 300
    assert gateway.summaries > 1
    assert len(loop._conversation) < 50


async def test_hook_drains_large_stdout_and_stderr_with_bounded_retention():
    import shlex
    import sys
    import time

    from kalash.hooks.events import HookEvent, HookPayload
    from kalash.hooks.runner import HookConfig, HookRunner

    command = shlex.join(
        [sys.executable, "-c", "import sys; print('x'*200000); sys.stderr.write('y'*200000)"]
    )
    runner = HookRunner(engine=NS())
    result = await runner._execute_command(
        HookConfig(command=command, timeout_s=5),
        HookPayload(event=HookEvent.SESSION_START),
        time.time(),
    )
    assert result.exit_code == 0
    assert len(result.stdout.encode()) == 32_000
    assert len(result.stderr.encode()) == 32_000
