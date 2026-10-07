"""Connected accounting, recovery, completion and privacy regressions."""

import json
from decimal import Decimal

import pytest

from kalash.core.budget import BudgetState, Pricing
from kalash.core.config import ConfigError, KalashConfig, MemoryConfig, load_config
from kalash.core.privacy import StreamRedactor
from kalash.models.normalize import (
    BlockDelta,
    BlockMetadata,
    BlockStart,
    BlockStop,
    MessageStop,
    Role,
    StopReason,
    StreamError,
)
from kalash.models.providers.anthropic import _normalize_usage
from kalash.runtime.agent import build_agent
from kalash.runtime.completion import CompletionContract, CompletionStatus
from kalash.runtime.stream import StreamHandler
from tests.unit.test_agent_loop import FakeGateway, make_host, make_loop, send, text_turn


def test_cache_partitions_and_cost_are_consistent():
    usage = _normalize_usage(
        {
            "input_tokens": 100,
            "output_tokens": 10,
            "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 200,
        }
    )
    budget = BudgetState()
    budget.record_usage(
        usage,
        Pricing(
            input_per_mtok=Decimal(3),
            output_per_mtok=Decimal(15),
            cache_read_per_mtok=Decimal(".3"),
            cache_write_per_mtok=Decimal("3.75"),
        ),
    )
    assert usage.total_tokens == 1310
    assert budget.cost_used == Decimal(".0015")


async def test_failed_stream_preserves_partial_text_and_unknown_reservation(tmp_path):
    gateway = FakeGateway(
        [[BlockStart(0, "text"), BlockDelta(0, "visible partial"), StreamError("lost")]]
    )
    loop = make_loop(gateway, make_host(tmp_path))
    result = await send(loop, "probe")
    assert result.final_response == "visible partial"
    assert any(message.role == Role.ASSISTANT for message in loop.conversation)
    assert result.usage_unknown
    assert loop.budget.unknown_tokens_reserved > 0


def test_stream_identity_and_signature_are_validated():
    handler = StreamHandler()
    for event in [
        BlockStart(0, "tool_use"),
        BlockMetadata(0, tool_use_id="id", tool_name="read"),
        BlockDelta(0, "{}"),
        BlockStop(0),
        MessageStop(StopReason.TOOL_USE),
    ]:
        handler.feed(event)
    assert handler.result().error is None
    assert handler.result().tool_calls[0].name == "read"
    duplicate = StreamHandler()
    for index in (0, 1):
        duplicate.feed(BlockStart(index, "tool_use", "same", "read"))
        duplicate.feed(BlockStop(index))
    duplicate.feed(MessageStop(StopReason.TOOL_USE))
    assert duplicate.result().error.code == "invalid_tool_identity"
    thinking = StreamHandler()
    for event in [
        BlockStart(0, "thinking"),
        BlockMetadata(0, signature="signed"),
        BlockStop(0),
        MessageStop(StopReason.END_TURN),
    ]:
        thinking.feed(event)
    assert thinking.result().content[0].signature == "signed"


def test_boolean_and_user_ceiling_are_authoritative(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KALASH_HOME", str(home))
    (home / "settings.json").write_text(json.dumps({"budget": {"max_tokens": 100}}))
    (tmp_path / ".kalash").mkdir()
    (tmp_path / ".kalash/settings.json").write_text(json.dumps({"budget": {"max_tokens": None}}))
    monkeypatch.setenv("KALASH_PERMISSIONS__NETWORK", "false")
    config = load_config(tmp_path)
    assert config.permissions.network is False
    assert config.budget.max_tokens == 100
    (home / "settings.json").write_text("broken")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_split_secret_never_reaches_stream_consumer(monkeypatch):
    secret = "fixture-credential-with-random-tail"  # noqa: S105 - Protocol label or synthetic fixture, never a credential.
    monkeypatch.setenv("TEST_API_KEY", secret)
    redactor = StreamRedactor()
    output = [
        redactor.feed("text " + secret[:12]),
        redactor.feed(secret[12:] + " done"),
        redactor.feed("", final=True),
    ]
    assert secret not in "".join(output)
    assert secret[:12] not in "".join(output)
    assert "done" in "".join(output)


def test_credentials_authenticate_ciphertext_and_bind_provider(tmp_path, monkeypatch):
    from kalash.models.auth_store import _decrypt, load_auth, save_credential

    monkeypatch.setenv("KALASH_HOME", str(tmp_path))
    save_credential("fixture", "secret-fixture")
    raw = json.loads((tmp_path / "auth.json").read_text())["fixture"]
    assert raw.startswith("enc2:")
    assert load_auth()["fixture"] == "secret-fixture"
    assert (tmp_path / "auth.key").stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError, match="integrity"):
        _decrypt(raw, "different-provider")
    with pytest.raises(ValueError, match="integrity"):
        _decrypt(raw[:-5] + "abcde", "fixture")


class Provider:
    name = "fixture/model"
    from kalash.models.normalize import ModelCapabilities

    capabilities = ModelCapabilities(context_window=32768, max_output_tokens=1024)
    pricing = Pricing()

    async def stream(self, *_args, **_kwargs):
        for event in text_turn("done"):
            yield event

    async def close(self):
        pass


async def test_resume_restores_workspace_accounting_and_summary(tmp_path):
    first, other = tmp_path / "first", tmp_path / "other"
    first.mkdir()
    other.mkdir()
    config = KalashConfig(memory=MemoryConfig(enabled=False))
    agent, reason = build_agent(cwd=first, provider=Provider(), config=config, extensions=False)
    assert agent is not None, reason
    result = await agent.send("probe")
    assert result.completion_status is CompletionStatus.STOPPED
    await agent.loop.session_repo.append_turn(
        session_id=agent.session_id,
        role="system",
        content="retained summary",
        content_type="compaction_summary",
    )
    resumed, reason = build_agent(
        cwd=other,
        provider=Provider(),
        config=config,
        extensions=False,
        resume=True,
        session_id=agent.session_id,
    )
    assert resumed is not None, reason
    await resumed.prepare()
    assert resumed.cwd == first
    assert resumed.budget.tokens_used == agent.budget.tokens_used > 0
    assert resumed.loop._compacted_summary == "retained summary"  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    await resumed.close()
    await agent.close()


async def test_completion_requires_deliverables(tmp_path):
    loop = make_loop(FakeGateway([text_turn("all done")]), make_host(tmp_path))
    from kalash.models.normalize import Message, TextBlock

    result = await loop.run(
        user_message=Message(Role.USER, [TextBlock("build")]),
        system_identity="test",
        completion_contract=CompletionContract(required_files=("missing.py",)),
    )
    assert result.completion_status is CompletionStatus.PARTIAL
    assert "Missing" in result.error


async def test_valid_contract_and_changed_artifact(tmp_path):
    from kalash.runtime.completion import verify_contract
    from kalash.runtime.toolhost import ToolExecutionResult

    path = tmp_path / "app.py"
    path.write_text("pass")
    host = make_host(tmp_path)

    async def check(*_args, **_kwargs):
        return ToolExecutionResult("passed", False, {"exit_code": 0})

    host.execute_result = check
    contract = CompletionContract(required_files=("app.py",), checks=("fixture-check",))
    assert (await verify_contract(contract, host))[0]

    async def changed(*_args, **_kwargs):
        path.write_text("changed after check")
        return await check()

    host.execute_result = changed
    assert not (await verify_contract(contract, host))[0]


async def test_session_writer_lock_refuses_second_owner(tmp_path):
    config = KalashConfig(memory=MemoryConfig(enabled=False))
    agent, _ = build_agent(cwd=tmp_path, provider=Provider(), config=config, extensions=False)
    resumed, _ = build_agent(
        cwd=tmp_path,
        provider=Provider(),
        config=config,
        extensions=False,
        resume=True,
        session_id=agent.session_id,
    )
    with agent._session_owner(), pytest.raises(RuntimeError, match="another run"):  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        await resumed.send("must not execute")
    assert resumed.budget.turns_used == 0
    await agent.close()
    await resumed.close()
