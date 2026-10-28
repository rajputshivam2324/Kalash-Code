"""Delegation preserves the parent's current permission boundary."""

from pathlib import Path

import pytest

from tests.harness.scripted import call, say


@pytest.mark.parametrize("mode", ["read-only", "plan"])
async def test_general_child_inherits_the_current_read_only_mode(make_harness, work: Path, mode):
    h = await make_harness(
        [
            call("task", {"description": "check project", "prompt": "inspect the project"}),
            call("write", {"file_path": "child.txt", "content": "must not write"}),
            say("write refused"),
            say("finished inspection"),
        ]
    )
    h.agent.set_mode(mode)
    result = await h.turn("inspect without changing files")

    assert result.reason == "completed"
    assert not (work / "child.txt").exists()
    child_result = h.adapter.requests[2].messages[-1]
    assert child_result.is_error and "read-only" in child_result.text
    assert "Permission mode: READ-ONLY" in h.adapter.requests[1].messages[0].text


async def test_general_child_inherits_disabled_approval_prompts(make_harness, work: Path):
    approvals = []

    async def approve(request):
        approvals.append(request)
        return "once"

    (work / ".kalash").mkdir()
    h = await make_harness(
        [
            call("task", {"description": "configure project", "prompt": "write config"}),
            call("write", {"file_path": ".kalash/child.txt", "content": "must not write"}),
            say("approval disabled"),
            say("reported"),
        ],
        approver=approve,
    )
    h.agent.set_mode("auto")
    await h.turn("delegate")

    assert approvals == []
    assert not (work / ".kalash" / "child.txt").exists()
    child_result = h.adapter.requests[2].messages[-1]
    assert child_result.is_error and "prompts are disabled" in child_result.text
