"""Connected offline regressions for session-one harness contracts."""

import asyncio
import json
import logging
import shutil
import subprocess
from decimal import Decimal
from types import SimpleNamespace as Namespace
from unittest.mock import AsyncMock

import pytest

from kalash.core.budget import BudgetState, Pricing
from kalash.core.config import ConfigError, KalashConfig, MemoryConfig, load_config
from kalash.core.privacy import StreamRedactor
from kalash.models.gateway import ModelGateway, RetryConfig
from kalash.models.normalize import Message, Role, TextBlock, ToolResultBlock, ToolUseBlock
from kalash.models.resolve import Resolution
from kalash.runtime.agent import build_agent
from kalash.runtime.journal import RuntimeJournal
from kalash.runtime.serialize import serialize_blocks
from kalash.runtime.session import SessionManager
from kalash.runtime.stream import StreamHandler
from kalash.sandbox.linux import LinuxSandbox
from kalash.sandbox.policy import SandboxMode, SandboxPolicy
from kalash.scheduler.daemon import SchedulerDaemon
from kalash.scheduler.manager import SchedulerManager
from kalash.sdk import KalashClient, KalashSession
from tests.unit.test_agent_loop import make_host, make_loop, send, text_turn
from tests.unit.test_harness_recovery import Provider
from tests.unit.test_provider_interruptions import wire_chunk, wire_provider, wire_usage


async def test_openai_sdk_late_identity_fragments_are_assembled():
    from kalash.models.providers.openai import OpenAIProvider

    wire, _ = wire_provider(
        [
            wire_chunk(delta={"tool_calls": [{"index": 0, "function": {"arguments": "{"}}]}),
            wire_chunk(
                delta={
                    "tool_calls": [
                        {"index": 0, "id": "call-", "function": {"name": "re", "arguments": "}"}}
                    ]
                }
            ),
            wire_chunk(
                delta={"tool_calls": [{"index": 0, "id": "one", "function": {"name": "ad"}}]},
                finish_reason="tool_calls",
            ),
            wire_chunk(usage=wire_usage(), choices=False),
        ]
    )
    provider = OpenAIProvider(api_key="fixture")
    provider._client = wire._client  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    handler = StreamHandler()
    try:
        async for event in provider.stream([]):
            handler.feed(event)
    finally:
        await provider.close()
    result = handler.result()
    assert result.error is None
    assert [(b.id, b.name, b.input) for b in result.tool_calls] == [("call-one", "read", {})]


async def test_anthropic_sdk_signature_and_cached_usage_survive_stream():
    import anthropic
    import httpx

    from kalash.models.providers.anthropic import AnthropicProvider
    from kalash.runtime.serialize import deserialize_blocks

    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_fixture",
                "type": "message",
                "role": "assistant",
                "model": "claude-fixture",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 1000,
                    "cache_creation_input_tokens": 200,
                },
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "reason"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "signed-fixture"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 10},
        },
        {"type": "message_stop"},
    ]
    payload = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    provider = AnthropicProvider(api_key="fixture")
    provider._client = anthropic.AsyncAnthropic(  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        api_key="fixture",
        max_retries=0,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, content=payload
                )
            )
        ),
    )
    handler = StreamHandler()
    try:
        async for event in provider.stream([]):
            handler.feed(event)
    finally:
        await provider.close()
    result = handler.result()
    assert result.error is None
    assert (result.input_tokens, result.output_tokens, result.cache_write_tokens) == (1100, 10, 200)
    restored = deserialize_blocks(serialize_blocks(result.content))
    assert restored[0].signature == "signed-fixture"


async def test_google_same_name_calls_roundtrip_unique_ids_and_signatures():
    from google.genai import types

    from kalash.models.providers.google import GeminiProvider, _normalize_usage, _serialize_messages

    part = types.Part(
        function_call=types.FunctionCall(name="read", args={"path": "a"}),
        thought_signature=b"signed",
    )
    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(content=types.Content(parts=[part, part]), finish_reason="STOP")
        ]
    )
    provider = GeminiProvider(api_key="fixture")
    provider._client = Namespace(  # noqa: SLF001 - Intentional provider fault fixture.
        aio=Namespace(models=Namespace(generate_content=AsyncMock(return_value=response)))
    )
    result = await provider.complete([])
    calls = result.content
    from kalash.runtime.serialize import deserialize_blocks

    calls = deserialize_blocks(serialize_blocks(calls))
    assert len({b.id for b in calls}) == 2
    contents, _ = _serialize_messages(
        [
            Message(Role.ASSISTANT, calls),
            Message(Role.USER, [ToolResultBlock(b.id, "ok") for b in calls]),
        ]
    )
    assert [p.function_response.name for p in contents[1].parts] == ["read", "read"]
    assert [p.function_response.id for p in contents[1].parts] == [b.id for b in calls]
    assert all(p.thought_signature == b"signed" for p in contents[0].parts)
    usage = _normalize_usage(
        Namespace(
            prompt_token_count=100,
            cached_content_token_count=80,
            candidates_token_count=10,
            thoughts_token_count=20,
        )
    )
    assert (usage.total_tokens, usage.cache_read_tokens, usage.reasoning_tokens) == (130, 80, 20)


async def test_fallback_failure_reserves_unknown_and_uses_actual_pricing(tmp_path):
    class Broken(Provider):
        name = "primary/broken"
        pricing = Pricing(input_per_mtok=Decimal(1), output_per_mtok=Decimal(1))

        async def stream(self, *_args, **_kwargs):
            raise ValueError("400 invalid request")
            yield

    class Fallback(Provider):
        name = "fallback/actual"
        pricing = Pricing(input_per_mtok=Decimal(5), output_per_mtok=Decimal(10))

    gateway = ModelGateway(Broken(), [Fallback()], RetryConfig(max_attempts=1))
    loop = make_loop(gateway, make_host(tmp_path))
    result = await send(loop, "answer")
    assert result.model == "fallback/actual"
    assert loop.budget.tokens_used == 135
    assert loop.budget.cost_used == Decimal(".00075")
    assert loop.budget.unknown_usage_attempts == 1
    assert loop.budget.unknown_tokens_reserved > 0
    assert loop.budget.turns_used == 2


async def test_pre_event_adapter_error_reaches_explicit_fallback(tmp_path):
    from kalash.models.normalize import StreamError

    class ErrorEvent(Provider):
        async def stream(self, *_args, **_kwargs):
            yield StreamError("transport failed before response", recoverable=False)

    loop = make_loop(
        ModelGateway(ErrorEvent(), [Provider()], RetryConfig(max_attempts=1)), make_host(tmp_path)
    )
    result = await send(loop, "answer")
    assert result.final_response == "done"
    assert loop.budget.turns_used == 2
    assert loop.budget.unknown_usage_attempts == 1


async def test_retry_rechecks_remaining_budget_before_dispatch(tmp_path):
    class Broken(Provider):
        calls = 0

        async def stream(self, *_args, **_kwargs):
            self.calls += 1
            raise ValueError("500 temporarily unavailable")
            yield

    provider = Broken()
    loop = make_loop(
        ModelGateway(provider, retry_config=RetryConfig(max_attempts=3, initial_delay_s=0)),
        make_host(tmp_path),
    )
    loop.budget.max_turns = 1
    result = await send(loop, "answer")
    assert provider.calls == 1
    assert result.termination_reason.value == "budget_exhausted"
    assert loop.budget.unknown_usage_attempts == 1


async def test_sdk_stream_is_visible_before_provider_finishes_and_closes(tmp_path):
    gate = asyncio.Event()
    closed = asyncio.Event()

    class Paused(Provider):
        async def stream(self, *_args, **_kwargs):
            try:
                events = text_turn("early chunk ")
                for event in events[:3]:
                    yield event
                await gate.wait()
                for event in events[3:]:
                    yield event
            finally:
                closed.set()

    agent, reason = build_agent(
        cwd=tmp_path,
        provider=Paused(),
        persist=False,
        extensions=False,
        config=KalashConfig(memory=MemoryConfig(enabled=False)),
    )
    assert agent is not None, reason
    session = KalashSession(agent, KalashClient())
    stream = session.stream("answer")
    first = await asyncio.wait_for(anext(stream), 1)
    assert "early" in first.text
    assert not gate.is_set()
    await asyncio.wait_for(stream.aclose(), 1)
    assert closed.is_set()
    assert agent.budget.unknown_usage_attempts == 1


async def test_sdk_model_override_routes_actual_provider_and_restores_defaults(
    tmp_path, monkeypatch
):
    class Selected(Provider):
        name = "other/chosen"
        closed = False

        async def close(self):
            self.closed = True

    selected = Selected()

    def resolver(provider, model):
        return Resolution(selected, provider, model)

    monkeypatch.setattr("kalash.models.resolve.build_provider", resolver)
    agent, reason = build_agent(
        cwd=tmp_path,
        provider=Provider(),
        persist=False,
        extensions=False,
        config=KalashConfig(memory=MemoryConfig(enabled=False)),
    )
    assert agent is not None, reason
    original = agent.loop.gateway
    session = KalashSession(agent, KalashClient())
    result = await session.complete("answer", model="other/chosen", tools=["read"])
    assert result.model == "other/chosen"
    assert selected.closed
    assert agent.loop.gateway is original
    assert agent.host.registry.get("write") is not None


async def test_journal_recovers_response_and_effect_receipt_once(tmp_path):
    manager = SessionManager()
    session = manager.create(str(tmp_path))
    from kalash.storage.repositories.sessions import SessionRepository

    journal = RuntimeJournal(SessionRepository(manager._ensure_engine()), session.id, "run")  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    await journal.append(
        "attempt_settled",
        {
            "attempt_id": "attempt",
            "content": serialize_blocks(
                [TextBlock("partial retained"), ToolUseBlock("call", "write", {})]
            ),
            "error": None,
        },
    )
    await journal.append(
        "effect_settled",
        {
            "effect_id": "effect",
            "call_id": "call",
            "tool": "write",
            "ok": True,
            "content": "written",
            "metadata": {"path": "a"},
        },
    )
    first = await journal.recover_messages()
    second = await journal.recover_messages()
    assert first == second
    assert len(first) == 2
    assert first[1].content[0].content == "written"
    assert not first[1].content[0].is_error
    assert len(await journal.repository.get_session_messages(session.id)) == 2
    assert manager.delete_session(session.id)
    assert manager.get_session(session.id) is None


async def test_recovery_restores_compaction_projection_plus_new_messages(tmp_path):
    manager = SessionManager()
    session = manager.create(str(tmp_path))
    from kalash.storage.repositories.sessions import SessionRepository

    journal = RuntimeJournal(SessionRepository(manager._ensure_engine()), session.id)  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    await journal.repository.append_turn(
        session_id=session.id, role="user", content=serialize_blocks([TextBlock("old bulk")])
    )
    await journal.append(
        "compaction",
        {
            "conversation": [
                {"role": "user", "content": serialize_blocks([TextBlock("retained constraint")])}
            ]
        },
        budget=BudgetState(),
        summary="summary",
    )
    await journal.repository.append_turn(
        session_id=session.id,
        role="assistant",
        content=serialize_blocks([TextBlock("new response")]),
    )
    history = await journal.recover_messages()
    assert [b.text for m in history for b in m.content] == ["retained constraint", "new response"]


async def test_hard_loss_restores_committed_visible_progress_and_reserves_once(tmp_path):
    manager = SessionManager()
    session = manager.create(str(tmp_path))
    from kalash.storage.repositories.sessions import SessionRepository

    journal = RuntimeJournal(SessionRepository(manager._ensure_engine()), session.id)  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    await journal.append(
        "attempt_started",
        {"attempt_id": "lost", "input_estimate": 10, "output_allowance": 20},
        budget=BudgetState(),
    )
    await journal.append("response_progress", {"attempt_id": "lost", "text": "already visible "})
    await journal.append("response_progress", {"attempt_id": "lost", "text": "partial text"})
    agent, reason = build_agent(
        cwd=tmp_path,
        provider=Provider(),
        extensions=False,
        session_id=session.id,
        resume=True,
        config=KalashConfig(memory=MemoryConfig(enabled=False)),
    )
    assert agent is not None, reason
    await agent.prepare()
    assert any(
        isinstance(b, TextBlock) and b.text == "already visible partial text"
        for m in agent.history
        for b in m.content
    )
    assert agent.budget.unknown_tokens_reserved == 30
    agent._resume_pending = True  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    await agent.prepare()
    assert agent.budget.unknown_tokens_reserved == 30
    await agent.close()


async def test_once_grant_is_consumed_by_only_one_competing_request(tmp_path):
    from kalash.permissions.grants import GrantLifetime, GrantStore
    from kalash.storage.engine import StorageEngine

    engine = StorageEngine(tmp_path / "grants.db")
    await engine.initialize()
    store = GrantStore(engine, "session")
    grant = await store.create(tool_pattern="shell", lifetime=GrantLifetime.ONCE)
    claimed = await asyncio.gather(store.consume(grant.id), store.consume(grant.id))
    assert sum(claimed) == 1


async def test_journal_receipt_failure_blocks_second_mutation(tmp_path):
    from kalash.runtime.journal import JournalError
    from tests.unit.test_agent_loop import AutoApprover, tool_turn

    events = tool_turn("write", {"path": "first.txt", "content": "first"}, call_id="first")
    from kalash.models.normalize import BlockDelta, BlockStart, BlockStop

    second = [
        BlockStart(1, "tool_use", "second", "write"),
        BlockDelta(1, json.dumps({"path": "second.txt", "content": "second"})),
        BlockStop(1),
    ]
    events[-1:-1] = second

    class ToolProvider(Provider):
        async def stream(self, *_args, **_kwargs):
            for event in events:
                yield event

    agent, reason = build_agent(
        cwd=tmp_path,
        provider=ToolProvider(),
        extensions=False,
        config=KalashConfig(memory=MemoryConfig(enabled=False)),
    )
    assert agent is not None, reason
    from kalash.permissions.prompt import ApprovalPrompt

    agent.host.approval = ApprovalPrompt(
        event_bus=agent.host.event_bus, ui=AutoApprover(), non_interactive=False
    )
    original = agent.loop.journal.append

    async def failing(kind, *args, **kwargs):
        if kind == "effect_settled":
            raise JournalError("injected receipt failure")
        return await original(kind, *args, **kwargs)

    agent.loop.journal.append = failing
    result = await agent.send("write two files")
    assert result.error
    assert (tmp_path / "first.txt").exists()
    assert not (tmp_path / "second.txt").exists()
    await agent.close()


async def test_manual_scheduler_trigger_connects_real_session_and_daemon(tmp_path, monkeypatch):  # noqa: ARG001 - Preserve callback/keyword compatibility.
    manager = SchedulerManager()
    job = manager.add(name="connected", schedule="0 0 * * *", prompt="answer")
    triggered = manager.trigger(job.id)
    assert SessionManager().get_session(triggered.session_id) is not None
    row = manager._engine.execute_read(  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        "SELECT status FROM schedule_runs WHERE schedule_id=?", (job.id,)
    )[0]
    assert row["status"] == "pending"
    actual_build = build_agent

    def offline_build(**kwargs):
        return actual_build(**kwargs, provider=Provider(), extensions=False)

    monkeypatch.setattr("kalash.runtime.agent.build_agent", offline_build)
    daemon = SchedulerDaemon(manager._engine)  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    assert await daemon._try_acquire_lease()  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    await daemon._process_due_schedules()  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    row = manager._engine.execute_read(  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        "SELECT * FROM schedule_runs WHERE schedule_id=?", (job.id,)
    )[0]
    assert row["status"] == "stopped_unverified"
    assert row["tokens_used"] > 0
    assert SessionManager().get_transcript(triggered.session_id)
    assert manager.get_logs(job_id=job.id)[0].status == "stopped_unverified"
    assert manager.delete(job.id)


async def test_scheduler_lease_has_one_winner_and_detects_lost_ownership(tmp_path):
    from kalash.storage.engine import StorageEngine

    engine = StorageEngine(tmp_path / "scheduler.db")
    await engine.initialize()
    one, two = SchedulerDaemon(engine), SchedulerDaemon(engine)
    won = await asyncio.gather(one._try_acquire_lease(), two._try_acquire_lease())  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    assert sum(won) == 1
    owner, competitor = (one, two) if won[0] else (two, one)
    await engine.execute_write("UPDATE scheduler_lock SET leader_id=?", (competitor._leader_id,))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    await owner._renew_lease()  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    assert not owner._leader  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.


@pytest.mark.parametrize(
    ("section", "value"),
    [
        ("permissions", "oops"),
        ("budget", []),
        ("budget", {"max_cost_usd": "NaN"}),
        ("budget", {"max_cost_usd": "bad"}),
    ],
)
def test_invalid_authority_settings_fail_closed(tmp_path, section, value):
    with pytest.raises(ConfigError):
        load_config(tmp_path, {section: value})


def test_corrupt_auth_storage_cannot_be_overwritten(tmp_path):  # noqa: ARG001 - Preserve callback/keyword compatibility.
    from kalash.core.paths import kalash_home
    from kalash.models.auth_store import save_credential

    path = kalash_home() / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("malformed existing storage")
    with pytest.raises(ValueError, match="Credential storage"):
        save_credential("fixture", "random-fixture-credential")
    assert path.read_text() == "malformed existing storage"


def test_oversized_and_incomplete_private_key_streams_are_withheld():
    redactor = StreamRedactor()
    output = redactor.feed("before -----BEGIN PRIVATE KEY-----\n" + "A" * 9000)
    output += redactor.feed("\nsecret-body \n-----END PRIVATE KEY----- after ")
    output += redactor.feed("", final=True)
    assert "secret-body" not in output
    assert "A" * 100 not in output
    assert "after" in output
    redactor = StreamRedactor()
    output = redactor.feed("-----BEGIN PRIVATE KEY-----\npartial") + redactor.feed("", final=True)
    assert "partial" not in output


def test_logging_scrubs_formatted_messages_and_exception_text(monkeypatch):
    from kalash.core.logging import _RedactingFormatter

    secret = "fixture-logging-secret-value"  # noqa: S105 - Protocol label or synthetic fixture, never a credential.
    monkeypatch.setenv("TEST_API_KEY", secret)
    try:
        raise ValueError(secret)
    except ValueError:
        import sys

        record = logging.LogRecord("test", 40, __file__, 1, "value %s", (secret,), sys.exc_info())
    output = _RedactingFormatter(logging.Formatter("%(message)s")).format(record)
    assert secret not in output


def test_cache_writes_and_reasoning_are_priced_once_including_zero_rates():
    from kalash.core.budget import Usage

    budget = BudgetState()
    budget.record_usage(
        Usage(input_tokens=100, output_tokens=30, cache_write_tokens=20, reasoning_tokens=20),
        Pricing(
            input_per_mtok=Decimal(1), output_per_mtok=Decimal(2), reasoning_per_mtok=Decimal(0)
        ),
    )
    assert budget.cost_used == Decimal(".00014")
    assert budget.tokens_used == 150


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Bubblewrap unavailable")
def test_linux_shell_protects_existing_secret_and_git_hooks(tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("protected-value")
    hooks = tmp_path / ".git/hooks"
    hooks.mkdir(parents=True)
    sandbox = LinuxSandbox(SandboxPolicy(workspace_root=tmp_path))
    read = subprocess.run(
        sandbox.wrap_command(["/bin/cat", str(secret)], cwd=str(tmp_path)), capture_output=True
    )
    assert read.returncode == 0 or b"Permission denied" in read.stderr
    assert b"protected-value" not in read.stdout
    write = subprocess.run(
        sandbox.wrap_command(
            ["/bin/sh", "-c", "echo changed > .env; echo hook > .git/hooks/new-hook"],
            cwd=str(tmp_path),
        ),
        capture_output=True,
    )
    assert write.returncode != 0
    assert secret.read_text() == "protected-value"
    assert not (hooks / "new-hook").exists()
    readonly = LinuxSandbox(SandboxPolicy(workspace_root=tmp_path, mode=SandboxMode.READ_ONLY))
    visible = tmp_path / "visible"
    visible.write_text("readable")
    read = subprocess.run(
        readonly.wrap_command(["/bin/cat", str(visible)], cwd=str(tmp_path)), capture_output=True
    )
    assert read.returncode == 0
    assert read.stdout == b"readable"


def test_generic_process_host_allowlist_does_not_become_unrestricted_network(tmp_path):
    from kalash.sandbox.macos import _generate_seatbelt_profile
    from kalash.sandbox.policy import NetworkPolicy

    policy = SandboxPolicy(
        workspace_root=tmp_path,
        network_policy=NetworkPolicy.ALLOW_LISTED,
        allowed_hosts=["allowed.invalid"],
    )
    if shutil.which("bwrap"):
        assert "--unshare-net" in LinuxSandbox(policy).wrap_command(
            ["/bin/true"], network=True, cwd=str(tmp_path)
        )
    profile = _generate_seatbelt_profile(
        writable_roots=[tmp_path], allowed_hosts=["allowed.invalid"]
    )
    assert "(deny network*)" in profile
    assert "(allow network" not in profile
