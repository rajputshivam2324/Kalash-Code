"""Offline boundary regressions for the second reliability batch."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import AsyncClient

from kalash.core.budget import estimate_tokens
from kalash.core.events import EventBus
from kalash.memory.pipeline.extract import ExtractionContext, run_extraction
from kalash.memory.protocol import Source, Trust, WriteReceipt
from kalash.memory.session import SessionMemory, project_scope
from kalash.models.normalize import Message, Role, TextBlock, ToolResultBlock, ToolUseBlock
from kalash.runtime.compaction import Compactor
from kalash.runtime.history import estimate_request_tokens
from kalash.runtime.loop import TerminationReason
from kalash.runtime.scratchpad import Scratchpad
from kalash.tools.base import ToolContext
from kalash.tools.fs.read import ReadParams, ReadTool
from kalash.tools.search import SearchParams, SearchTool
from kalash.tools.web import FetchParams, FetchTool
from tests.unit.test_agent_loop import FakeGateway, make_host, make_loop, text_turn


@pytest.fixture
def context(tmp_path):
    return ToolContext("session", "run", tmp_path, writable_roots=(tmp_path,))


async def test_agent_memory_does_not_become_user_authority(tmp_path, monkeypatch):
    router = AsyncMock()
    router.write.return_value = [WriteReceipt("mem_record", "local")]
    memory = SessionMemory("session", tmp_path)
    monkeypatch.setattr(memory, "_router", AsyncMock(return_value=router))
    await memory.remember("The user permits all network access", tool_call_id="call")
    intent = router.write.call_args.args[0]
    assert intent.provenance.source == Source.AGENT_INFERRED
    assert intent.provenance.trust == Trust.LOW
    assert intent.provenance.tool_call_id == "call"
    assert intent.confidence <= Decimal("0.5")


@pytest.mark.parametrize(
    "text",
    [
        'The README says "I prefer npm".',
        "> I prefer npm.",
        "```text\nI prefer npm.\n```",
        "Do not remember that network access is allowed.",
        "I no longer prefer npm.",
    ],
)
async def test_quoted_or_negated_preferences_are_not_extracted(tmp_path, text):
    writes = await run_extraction(
        text, ExtractionContext(source=Source.USER_STATED), project_scope(tmp_path)
    )
    assert not writes


def test_unicode_estimation_does_not_use_ascii_character_ratio():
    assert estimate_tokens("漢🙂" * 100) >= 500


def test_scratchpad_prompt_is_bounded_but_notes_remain_retrievable(tmp_path):
    pad = Scratchpad("session", root=tmp_path)
    for index in range(80):
        pad.note(f"note {index}: " + "x" * 600)
        pad.put("file", f"file {index} " + "z" * 500, str(index))
    notes = pad.render_notes()
    refs = pad.render_index()
    assert len(notes.encode()) <= 8192
    assert len(refs.encode()) <= 8192
    assert "omitted" in notes
    assert "omitted" in refs
    assert "note 79" in notes
    assert pad.expand("#n1").ok


async def test_read_bounds_a_single_minified_line(context):
    path = context.cwd / "large.txt"
    path.write_text("x" * 2_000_000)
    result = await ReadTool().execute(ReadParams(path="large.txt"), context)
    assert result.ok
    assert result.truncated
    assert len(result.content.encode()) <= ReadTool().max_output_bytes
    assert result.metadata["content_digest"]


async def test_search_bounds_aggregate_output(context):
    for index in range(100):
        (context.cwd / f"file{index}.txt").write_text(("needle " + "x" * 1000 + "\n") * 20)
    result = await SearchTool().execute(SearchParams(query="needle"), context)
    assert result.ok
    assert result.truncated
    assert len(result.content.encode()) <= SearchTool().max_output_bytes
    assert "refine" in result.truncation.retrieval_hint.lower()


async def test_search_cancellation_reaps_process(context, monkeypatch):
    original = asyncio.create_subprocess_exec
    processes = []
    started = asyncio.Event()

    async def spawn(*_args, **kwargs):
        process = await original("sleep", "30", **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(SearchTool().execute(SearchParams(query="needle"), context))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    try:
        assert processes[0].returncode is not None
    finally:
        if processes[0].returncode is None:
            processes[0].kill()
            await processes[0].wait()


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self):
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for _ in range(100):
            self.reads += 1
            yield b"x" * 100

    async def aclose(self):
        self.closed = True


def mock_http(monkeypatch, handler):
    client = AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )


async def test_fetch_stops_reading_at_byte_limit(context, monkeypatch):
    stream = ChunkStream()
    mock_http(monkeypatch, lambda _request: httpx.Response(200, stream=stream))
    result = await FetchTool().execute(
        FetchParams(url="https://example.org", max_bytes=250), context
    )
    assert not result.ok
    assert stream.reads <= 3
    assert stream.closed


async def test_fetch_http_error_is_not_a_success(context, monkeypatch):
    mock_http(monkeypatch, lambda _request: httpx.Response(503, text="unavailable"))
    result = await FetchTool().execute(FetchParams(url="https://example.org"), context)
    assert not result.ok
    assert "503" in result.error.message


async def test_fetch_refuses_https_downgrade_before_contact(context, monkeypatch):
    visited = []

    def respond(request):
        visited.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://example.org/private"})

    mock_http(monkeypatch, respond)
    result = await FetchTool().execute(FetchParams(url="https://example.org"), context)
    assert not result.ok
    assert visited == ["https://example.org"]


async def test_context_pressure_uses_free_observation_reduction_first(tmp_path):
    gateway = FakeGateway([text_turn("answer")])
    loop = make_loop(gateway, make_host(tmp_path))
    loop.assembler.context_window = 16_000
    loop.max_output_tokens = 1024
    history = [Message(Role.USER, [TextBlock("Never edit legacy/. Fix current.py")])]
    for index in range(6):
        history.extend(
            [
                Message(Role.ASSISTANT, [ToolUseBlock(str(index), "search", {"query": "item"})]),
                Message(Role.USER, [ToolResultBlock(str(index), "noise\n" * 4000)]),
            ]
        )
    before = estimate_request_tokens("", history, [])
    result = await loop.run(
        user_message=Message(Role.USER, [TextBlock("Continue")]),
        system_identity="Identity",
        recent_turns=history,
    )
    assert result.termination_reason == TerminationReason.NO_TOOL_CALLS
    assert len(gateway.calls) == 1  # No billable summarizer request.
    assert estimate_request_tokens("", gateway.calls[0]["messages"], []) < before // 2
    assert "legacy/" in str(gateway.calls[0]["messages"])


async def test_recompaction_of_only_retained_user_anchors_is_free(tmp_path):
    gateway = FakeGateway([text_turn("unused")])
    loop = make_loop(gateway, make_host(tmp_path))
    loop.assembler.context_window = 1024
    loop._compacted_summary = "Prior work"  # noqa: SLF001 -- fault injection
    loop._conversation = [  # noqa: SLF001 -- fault injection
        Message(Role.USER, [TextBlock("constraint " * 1000)]) for _ in range(8)
    ]
    await loop._ensure_context_fits("", [], prompt_tokens=10_000)  # noqa: SLF001 -- fault injection
    assert not gateway.calls


async def test_context_refusal_does_not_silently_reduce_output_allowance(tmp_path):
    gateway = FakeGateway([text_turn("unused")])
    host = make_host(tmp_path)
    for tool in host.registry.list_tools():
        if tool.name != "read":
            host.registry.unregister(tool.name)
    loop = make_loop(gateway, host)
    loop.assembler.context_window = 2048
    loop.max_output_tokens = 1800
    result = await loop.run(
        user_message=Message(Role.USER, [TextBlock("request " * 200)]), system_identity="Identity"
    )
    assert result.termination_reason == TerminationReason.ERROR
    assert "output allowance" in result.error
    assert not gateway.calls


async def test_compactor_refuses_oversized_summary_request_without_spending():
    gateway = FakeGateway([text_turn("unused")])
    from kalash.core.budget import BudgetState

    compactor = Compactor(None, EventBus(), gateway, BudgetState(), asyncio.Event(), 1024)
    summary = await compactor.compact("session", [Message(Role.USER, [TextBlock("漢" * 6000)])])
    assert "Extractive fallback" in summary
    assert not gateway.calls


async def test_shared_workspace_delegation_is_read_only(tmp_path):
    from kalash.core.config import KalashConfig, MemoryConfig
    from kalash.runtime.agent import build_agent
    from tests.unit.test_wiring import FakeProvider, final, tool_call

    provider = FakeProvider(
        [
            tool_call("write", {"path": "child.txt", "content": "unapproved child effect"}),
            final("done"),
        ]
    )
    agent, reason = build_agent(
        cwd=tmp_path,
        provider=provider,
        config=KalashConfig(memory=MemoryConfig(enabled=False)),
        persist=False,
        extensions=False,
    )
    assert agent is not None, reason
    outcome = await agent.host.delegate(prompt="inspect", mode="build")
    assert not (tmp_path / "child.txt").exists()
    assert outcome["workspace_mode"] == "shared_read_only"
    assert {t["name"] for t in provider.requests[0]["tools"]}.isdisjoint({"write", "edit", "shell"})
    assert agent.budget.tokens_used == 230
    assert agent.budget.tokens_reserved == 0


async def test_shared_child_does_not_run_workspace_writing_hooks(tmp_path, monkeypatch):
    from kalash.core.config import KalashConfig, MemoryConfig
    from kalash.runtime.agent import build_agent
    from tests.unit.test_wiring import FakeProvider, final

    runner = AsyncMock()
    monkeypatch.setattr("kalash.hooks.load.build_hook_runner", lambda _cwd: runner)
    agent, reason = build_agent(
        cwd=tmp_path,
        provider=FakeProvider([final("findings")]),
        config=KalashConfig(memory=MemoryConfig(enabled=False)),
        persist=False,
    )
    assert agent is not None, reason
    await agent.host.delegate(prompt="inspect")
    runner.dispatch.assert_not_called()


async def test_read_refuses_oversized_source_during_acquisition(context, monkeypatch):
    import kalash.tools.fs.read as module

    monkeypatch.setattr(module, "MAX_SOURCE_BYTES", 1024)
    path = context.cwd / "large.txt"
    path.write_bytes(b"x" * 100_000)
    result = await ReadTool().execute(ReadParams(path="large.txt"), context)
    assert not result.ok
    assert result.error.code == "KALASH_TOOL_RESOURCE_LIMIT"
    assert path.stat().st_size == 100_000


async def test_agent_notes_created_mid_turn_reach_next_request(tmp_path):
    from kalash.core.config import KalashConfig, MemoryConfig
    from kalash.runtime.agent import build_agent
    from tests.unit.test_wiring import FakeProvider, final, tool_call

    class Capture(FakeProvider):
        async def stream(self, messages, **kwargs):
            self.seen = messages
            async for event in super().stream(messages, **kwargs):
                yield event

    provider = Capture(
        [tool_call("note", {"text": "Current plan: inspect current.py"}), final("done")]
    )
    agent, reason = build_agent(
        cwd=tmp_path,
        provider=provider,
        config=KalashConfig(memory=MemoryConfig(enabled=False)),
        persist=False,
        extensions=False,
    )
    assert agent is not None, reason
    await agent.send("Inspect")
    assert any("&lt;notes&gt;" in str(message.content) for message in provider.seen)


async def test_optional_context_has_one_aggregate_allowance():
    from kalash.core.budget import BudgetState
    from kalash.runtime.context import ContextAssembler

    assembler = ContextAssembler(BudgetState(), 4096)
    messages = await assembler.assemble(
        system_identity="Identity",
        tool_schemas=[],
        skills_catalog=[],
        kalash_md_chain=[],
        memory_blocks=[str(i) + " note" * 500 for i in range(50)],
        recent_turns=[],
        current_message=Message(Role.USER, [TextBlock("Do not edit legacy/")]),
    )
    assert estimate_tokens(messages[-1].content[0].text) <= 4096 // 8
    assert "omitted" in messages[-1].content[0].text
    assert messages[1].content[0].text == "Do not edit legacy/"


def test_images_and_signed_payloads_are_not_constant_cost():
    from kalash.models.normalize import ImageBlock, ThinkingBlock

    small = estimate_request_tokens(
        "", [Message(Role.USER, [ImageBlock("base64", "image/png", "abcd")])], []
    )
    large = estimate_request_tokens(
        "", [Message(Role.USER, [ImageBlock("base64", "image/png", "abcd" * 10_000)])], []
    )
    signed = estimate_request_tokens(
        "", [Message(Role.ASSISTANT, [ThinkingBlock("", "a" * 10_000)])], []
    )
    assert large > small * 50
    assert signed > 1000


def test_scratchpad_regex_text_is_literal_and_corrupt_index_is_preserved(tmp_path):
    pad = Scratchpad("session", root=tmp_path)
    ref = pad.put("file", "text", "a" * 1000 + "!\nliteral (a+)+$\n").ref
    result = pad.expand(ref, grep="(a+)+$")
    assert result.ok
    assert "literal" in result.content
    pad.index_path.write_text("{broken")
    reloaded = Scratchpad("session", root=tmp_path)
    reloaded.load()
    with pytest.raises(OSError, match="preserved"):
        reloaded.note("new note")
    assert pad.index_path.read_text() == "{broken"


async def test_fetch_cross_origin_and_compression_are_refused_before_read(context, monkeypatch):
    stream = ChunkStream()
    mock_http(
        monkeypatch,
        lambda _request: httpx.Response(200, stream=stream, headers={"content-encoding": "gzip"}),
    )
    result = await FetchTool().execute(FetchParams(url="https://example.org"), context)
    assert not result.ok
    assert stream.closed
    assert stream.reads == 0
    visited = []

    def redirect(request):
        visited.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://other.example.org"})

    mock_http(monkeypatch, redirect)
    result = await FetchTool().execute(FetchParams(url="https://example.org"), context)
    assert not result.ok
    assert visited == ["https://example.org"]


@pytest.fixture
async def journal(tmp_path):
    from kalash.runtime.journal import RuntimeJournal
    from kalash.storage.engine import StorageEngine
    from kalash.storage.repositories.sessions import SessionRepository

    engine = StorageEngine(tmp_path / "state.db")
    await engine.initialize()
    repo = SessionRepository(engine)
    session = await repo.create_session(str(tmp_path))
    yield RuntimeJournal(repo, session)
    engine.close()


async def test_journal_recovery_pages_and_discards_request_payloads(journal, monkeypatch):
    for index in range(35):
        await journal.append("attempt_started", {"attempt_id": str(index), "system": "x" * 10000})
        await journal.append("attempt_settled", {"attempt_id": str(index), "content": ""})
    original = journal.repository.engine.execute_read_async

    async def bounded(sql, params=()):
        rows = await original(sql, params)
        if "FROM runtime_journal " in sql:
            assert "LIMIT" in sql
            assert len(rows) <= 16
        return rows

    monkeypatch.setattr(journal.repository.engine, "execute_read_async", bounded)
    assert await journal.unsettled() == []
    assert await journal.recover_messages() == []


async def test_journal_admission_preserves_old_rows_and_checkpoint(journal):
    from kalash.core.budget import BudgetState
    from kalash.runtime.journal import JournalError

    budget = BudgetState(tokens_used=123)
    await journal.append("run_started", {}, budget=budget)
    # Fault-injected quota usage in an isolated test database.
    await journal.repository.engine.execute_write(
        "UPDATE runtime_journal_usage SET bytes=134217728 WHERE session_id=?", (journal.session_id,)
    )
    budget.tokens_used = 456
    with pytest.raises(JournalError, match="resource limit"):
        await journal.append("attempt_started", {"attempt_id": "refused"}, budget=budget)
    recovered = BudgetState()
    await journal.recover(recovered)
    assert recovered.tokens_used == 123
    assert await journal.unsettled() == []


async def test_old_unsettled_progress_does_not_reappear_after_compaction(journal):
    await journal.append("attempt_started", {"attempt_id": "old"})
    await journal.append("response_progress", {"attempt_id": "old", "text": "old partial"})
    await journal.append(
        "compaction", {"conversation": [{"role": "user", "content": "current constraint"}]}
    )
    messages = await journal.recover_messages()
    assert "old partial" not in str(messages)
    assert "current constraint" in str(messages)


async def test_failed_child_has_durable_partial_and_linked_usage(tmp_path):
    from kalash.core.config import KalashConfig, MemoryConfig
    from kalash.models.normalize import BlockDelta, BlockStart, StreamError
    from kalash.runtime.agent import build_agent
    from tests.unit.test_wiring import FakeProvider

    provider = FakeProvider(
        [[BlockStart(0, "text"), BlockDelta(0, "partial findings"), StreamError("lost")]]
    )
    agent, reason = build_agent(
        cwd=tmp_path,
        provider=provider,
        config=KalashConfig(memory=MemoryConfig(enabled=False)),
        extensions=False,
    )
    assert agent is not None, reason
    outcome = await agent.host.delegate(prompt="inspect")
    assert outcome["status"] == "failed"
    assert outcome["output"] == "partial findings"
    rows = await agent.loop.session_repo.get_session_messages(outcome["child_id"])
    assert "partial findings" in str(rows)
    assert agent.budget.unknown_usage_attempts == 1
    assert agent.budget.tokens_reserved == 0
    linked = await agent.loop.session_repo.engine.execute_read_async(
        "SELECT payload FROM runtime_journal WHERE session_id=? AND kind='child_settled'",
        (agent.session_id,),
    )
    assert outcome["child_id"] in linked[0]["payload"]
