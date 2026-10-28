"""How instructions, the skill list and the runtime snapshot reach the model (the real loop).

The system prompt never carries them (so it stays cache-stable); they ride in the user message as
logged ``<system-reminder>`` blocks, once per change.
"""

from __future__ import annotations

from pathlib import Path

from kalash.session.log import SessionLog
from tests.harness.scripted import call, say


def user_messages(h):  # type: ignore[no-untyped-def]
    return [m for m in h.session.messages() if m.role == "user"]


async def test_private_workspace_does_not_inherit_an_ancestor_project(make_harness, work: Path):
    (work.parent / ".git").mkdir()
    (work.parent / "AGENTS.md").write_text("ANCESTOR-INSTRUCTION must stay outside the test.")
    outside = work.parent / "outside.txt"
    h = await make_harness(
        [call("write", {"file_path": str(outside), "content": "outside"}), say("done")]
    )
    result = await h.turn("try writing outside the private workspace")
    assert h.agent.tool_ctx.root == work.resolve()
    assert not outside.exists()
    assert result.denials and "outside the workspace" in result.denials[0]
    assert "ANCESTOR-INSTRUCTION" not in h.adapter.requests[0].messages[0].text
    h.adapter.assert_exhausted()


async def test_first_message_carries_instructions_and_runtime_before_the_prompt(
    make_harness, work: Path
):
    (work / "AGENTS.md").write_text("Always run the linter.")
    h = await make_harness([say("ok")])
    await h.turn("add a feature")
    request = h.adapter.requests[0]
    first = request.messages[0]
    assert first.visible_text.strip() == "add a feature"
    assert first.text.index("Always run the linter.") < first.text.index("add a feature")
    assert "Current runtime context" in first.text and f'"{work}"' in first.text
    assert "Always run the linter." not in request.system, "instructions are not system text"
    assert str(work) not in request.system and "Date:" not in request.system


async def test_the_system_prompt_is_identical_whatever_changes_mid_session(
    make_harness, work: Path
):
    (work / "AGENTS.md").write_text("v1")
    h = await make_harness([say("one"), say("two"), say("three")])
    await h.turn("first")
    h.agent.set_mode("plan")
    (work / "AGENTS.md").write_text("v2")
    h.agent.context.refresh_instructions()
    await h.turn("second")
    h.agent.set_mode("auto")
    await h.turn("third")
    systems = {r.system for r in h.adapter.requests}
    assert len(systems) == 1, "the cached prefix must survive mode and instruction changes"
    tools = [[t.name for t in r.tools] for r in h.adapter.requests]
    assert tools[0] == tools[1] == tools[2], "the tool catalog is byte-stable across modes"


async def test_blocks_are_delivered_once_and_logged_as_digests(make_harness, work: Path):
    (work / "AGENTS.md").write_text("Rule.")
    h = await make_harness([say("one"), say("two")])
    await h.turn("first")
    context_events = [e for e in h.session.events if e.type == "context"]
    assert len(context_events) == 1 and set(context_events[0].data["digests"]) == {
        "instructions",
        "runtime",
    }
    await h.turn("second")
    last = h.adapter.requests[-1].messages[-1]
    assert last.text.strip() == "second", "nothing changed, so nothing is repeated"
    assert len([e for e in h.session.events if e.type == "context"]) == 1


async def test_a_mode_change_resends_only_the_runtime_snapshot_with_a_notice(
    make_harness, work: Path
):
    (work / "AGENTS.md").write_text("Rule.")
    h = await make_harness([say("one"), say("two")])
    await h.turn("first")
    h.agent.set_mode("auto")
    await h.turn("second")
    text = h.adapter.requests[-1].messages[-1].text
    assert "Permission mode: AUTO" in text and "from ASK to AUTO" in text
    assert "Rule." not in text, "instructions did not change"


async def test_resuming_does_not_repeat_blocks_but_does_replace_changed_ones(
    make_harness, work: Path
):
    (work / "AGENTS.md").write_text("Original rule.")
    h1 = await make_harness([say("one")])
    await h1.turn("first")
    path = h1.session.path
    await h1.runtime.aclose()

    h2 = await make_harness([say("two")], session=SessionLog.open(path))
    await h2.turn("second")
    assert h2.adapter.requests[0].messages[-1].text.strip() == "second"
    await h2.runtime.aclose()

    (work / "AGENTS.md").write_text("Changed rule.")
    h3 = await make_harness([say("three")], session=SessionLog.open(path))
    await h3.turn("third")
    last = h3.adapter.requests[0].messages[-1].text
    assert "replaces all earlier workspace instruction baselines" in last
    assert "Changed rule." in last and "Original rule." not in last


async def test_compaction_carries_the_blocks_into_the_checkpoint(make_harness, work: Path):
    from kalash.llm.types import Usage

    (work / "AGENTS.md").write_text("Never touch generated files.")
    big = "lorem ipsum dolor " * 600
    h = await make_harness(
        [
            say(big, usage=Usage(input_tokens=9000, output_tokens=100)),
            say("SUMMARY of the work so far", usage=Usage(500, 100)),
            say("final answer"),
        ],
        window=10_000,
    )
    await h.turn(big)
    await h.turn("next question")
    request = h.adapter.requests[2]
    checkpoint = request.messages[0]
    assert "SUMMARY of the work so far" in checkpoint.text
    assert "Never touch generated files." in checkpoint.text
    assert "Current runtime context" in checkpoint.text
    assert "Never touch" not in checkpoint.visible_text
    assert request.messages[1].text.strip() == "next question", "blocks were not sent twice"


async def test_reading_a_file_under_a_nested_instruction_file_announces_it(
    make_harness, work: Path
):
    (work / "web").mkdir()
    (work / "web" / "AGENTS.md").write_text("In web/, use TypeScript only.")
    (work / "web" / "app.ts").write_text("export {}\n")
    h = await make_harness(
        [
            call("read", {"file_path": "web/app.ts"}),
            call("read", {"file_path": "web/app.ts"}),
            say("ok"),
        ]
    )
    await h.turn("look at the web app")
    results = [m.text for m in h.session.messages() if m.role == "tool"]
    assert "Additional instructions from: web/AGENTS.md" in results[0]
    assert "In web/, use TypeScript only." in results[0]
    assert "Additional instructions" not in results[1], "announced once"


async def test_a_failed_read_does_not_load_nested_instructions(make_harness, work: Path):
    (work / "web").mkdir()
    (work / "web" / "AGENTS.md").write_text("Web rule.")
    h = await make_harness([call("read", {"file_path": "web/missing.ts"}), say("ok")])
    await h.turn("look")
    results = [m.text for m in h.session.messages() if m.role == "tool"]
    assert "Additional instructions" not in results[0]


async def test_switching_models_tells_the_new_one_who_wrote_the_earlier_turns(
    make_harness, work: Path
):
    h1 = await make_harness([say("one")])
    await h1.turn("first")
    path = h1.session.path
    await h1.runtime.aclose()
    log = SessionLog.open(path)
    log.append("model", model="other/earlier-model")  # as if another model had been in use
    h2 = await make_harness([say("two")], session=log)
    await h2.turn("second")
    text = h2.adapter.requests[0].messages[-1].text
    assert "earlier assistant turns were generated by other/earlier-model" in text
    assert "this session continues with scripted/model" in text


async def test_a_subagent_gets_its_own_runtime_context_but_not_the_parents_history(
    make_harness, work: Path
):
    (work / "AGENTS.md").write_text("Shared rule.")
    h = await make_harness(
        [
            call("task", {"description": "look", "prompt": "Find X", "agent": "explore"}),
            say("child report"),
            say("parent done"),
        ]
    )
    await h.turn("delegate")
    child = h.adapter.requests[1].messages[0]
    assert child.visible_text.strip() == "Find X"
    assert "Shared rule." in child.text and "Permission mode: READ-ONLY" in child.text
