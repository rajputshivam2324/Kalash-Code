"""Recover exact evidence and task state after lossy summaries and new sessions."""

from pathlib import Path

from kalash.llm.types import Usage
from kalash.session.log import SessionLog
from tests.contract.test_compaction import BIG, WINDOW
from tests.harness.scripted import call, say


async def test_exact_tool_evidence_is_retrievable_after_compaction(make_harness, work: Path):
    (work / "config.txt").write_text("database_path = /srv/app/ledger-v7.sqlite\n")
    h = await make_harness(
        [
            call("read", {"file_path": "config.txt"}),
            say(BIG, usage=Usage(9000, 100)),
            say("A deliberately lossy summary."),
            call("session_search", {"query": "ledger-v7.sqlite"}),
            say("recovered the original evidence"),
        ],
        window=WINDOW,
    )
    await h.turn("read the configuration")
    result = await h.turn("recover the exact database path")

    assert result.reason == "completed"
    retrieved = h.adapter.requests[-1].messages[-1]
    assert not retrieved.is_error and "database_path = /srv/app/ledger-v7.sqlite" in retrieved.text
    assert "seq=" in retrieved.text


async def test_session_note_survives_compaction_and_resume(make_harness):
    h = await make_harness(
        [
            call(
                "memory",
                {"action": "remember", "key": "constraint", "text": "Keep verifier.py unchanged."},
            ),
            say(BIG, usage=Usage(9000, 100)),
            say("Summary omitted the constraint."),
            say("continuing"),
        ],
        window=WINDOW,
    )
    await h.turn("fix the implementation; never change verifier.py")
    await h.turn("continue")
    checkpoint = h.adapter.requests[-1].messages[0]
    assert "Keep verifier.py unchanged." in checkpoint.text
    assert "never change verifier.py" in checkpoint.text
    assert any(e.type == "memory" for e in h.session.events)
    path = h.session.path
    await h.runtime.aclose()
    h2 = await make_harness(
        [call("memory", {"action": "recall", "key": "constraint"}), say("restored")],
        session=SessionLog.open(path),
    )
    await h2.turn("what must stay unchanged?")
    assert "Keep verifier.py unchanged." in h2.adapter.requests[-1].messages[-1].text


async def test_project_note_survives_new_session_and_forget_is_durable(make_harness):
    h = await make_harness(
        [
            call(
                "memory",
                {
                    "action": "remember",
                    "scope": "project",
                    "key": "tests",
                    "text": "Run pytest tests/contract.",
                },
            ),
            say("saved"),
        ]
    )
    await h.turn("remember the project test command")
    h2 = await make_harness(
        [
            call("memory", {"action": "recall", "scope": "project", "key": "tests"}),
            call("memory", {"action": "forget", "scope": "project", "key": "tests"}),
            say("forgotten"),
        ]
    )
    await h2.turn("recall then forget")
    assert "Run pytest tests/contract." in h2.adapter.requests[0].messages[0].text
    recalled = next(m for m in h2.session.messages() if m.role == "tool")
    assert not recalled.is_error and "Run pytest tests/contract." in recalled.text
    h3 = await make_harness(
        [call("memory", {"action": "recall", "scope": "project", "key": "tests"}), say("empty")]
    )
    await h3.turn("recall")
    assert "No matching notes" in h3.adapter.requests[-1].messages[-1].text


async def test_memory_snapshot_is_logged_and_never_presented_as_user_words(make_harness):
    h = await make_harness(
        [
            call("memory", {"action": "remember", "key": "plan", "text": "Check the API."}),
            say("one"),
            say("two"),
        ]
    )
    await h.turn("first")
    await h.turn("second")
    message = h.adapter.requests[-1].messages[-1]
    assert message.visible_text.strip() == "second"
    assert "Check the API." in message.text
    assert h.session.messages()[-2].to_dict() == message.to_dict()


async def test_archive_pages_reconstruct_exact_original_content(make_harness):
    text = "archive-needle:" + "abcdef0123456789" * 100
    h = await make_harness([say("saved")])
    await h.turn(text)
    tool = h.agent.registry.get("session_search")
    assert tool is not None
    seq = next(e.seq for e in h.session.events if e.type == "user")
    pages = []
    offset = 0
    while True:
        result = await tool.run(
            tool.parse({"start_seq": seq, "end_seq": seq, "offset": offset, "limit": 128}),
            h.agent.tool_ctx,
        )
        body, _, footer = result.text.rpartition("\n[characters ")
        pages.append(body)
        if "; end]" in footer:
            break
        offset += len(body)
    assert text in "".join(pages)


async def test_archive_query_surfaces_a_match_in_the_middle_of_a_large_message(make_harness):
    text = "unrelated sample\n" * 600 + "ARCHIVE_TOKEN=exact-value\n" + "more data\n" * 600
    h = await make_harness([say("saved")])
    await h.turn(text)
    tool = h.agent.registry.get("session_search")
    assert tool is not None

    result = await tool.run(tool.parse({"query": "ARCHIVE_TOKEN"}), h.agent.tool_ctx)

    assert "ARCHIVE_TOKEN=exact-value" in result.text
    assert len(result.text) < 1500
    assert "seq=" in result.text
    # The default search view must not remove access to the original full entry.
    full = await tool.run(
        tool.parse({"query": "ARCHIVE_TOKEN", "full_entries": True, "limit": 20000}),
        h.agent.tool_ctx,
    )
    assert text in full.text


async def test_project_memory_cannot_leak_to_another_project(make_harness, work: Path):
    from kalash.session.memory import SessionMemory

    h = await make_harness(
        [
            call(
                "memory",
                {
                    "action": "remember",
                    "scope": "project",
                    "key": "private",
                    "text": "project-specific-note",
                },
            ),
            say("saved"),
        ]
    )
    await h.turn("save")
    assert any(e.type == "memory" for e in h.session.events)
    other = work / "other-project"
    other.mkdir()
    log = SessionLog.create(other, other, "scripted/model")
    try:
        assert SessionMemory(log).notes() == []
    finally:
        log.close()
