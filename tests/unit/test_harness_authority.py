"""Authority and conditional-write regressions from the whole-harness audit."""

import hashlib
from dataclasses import replace

from kalash.permissions.classify import classify_tool_call
from kalash.permissions.policy import DenyRule
from kalash.tools.base import ToolContext
from kalash.tools.fs.edit import MultiEditParams, MultiEditTool
from kalash.tools.fs.write import WriteParams, WriteTool
from tests.unit.test_agent_loop import make_host


async def test_current_deny_overrides_session_approval(tmp_path):
    host = make_host(tmp_path)
    args = {"path": "file.txt", "content": "allowed before"}
    risk = classify_tool_call("write", args, cwd=tmp_path, writable_roots=(tmp_path,))
    host._grants.add(host._signature(risk, args))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    host.policy.deny_rules.append(DenyRule("write", "revoked"))
    assert "revoked" in await host._gate(risk, args)  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    assert not (tmp_path / "file.txt").exists()


def test_grants_bind_arguments_and_workspace(tmp_path):
    host = make_host(tmp_path)

    def signature(command):
        args = {"command": command}
        risk = classify_tool_call("shell", args, cwd=tmp_path, writable_roots=(tmp_path,))
        return host._signature(risk, args)  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.

    assert signature("git commit -m one") != signature("git commit -m two")
    args = {"path": "a.txt", "content": "one"}
    risk = classify_tool_call("write", args, cwd=tmp_path, writable_roots=(tmp_path,))
    assert host._signature(risk, args) != replace(host, cwd=tmp_path / "other")._signature(  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        risk, args
    )


async def test_pre_edit_change_rejects_stale_replacement(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("original")

    class Hooks:
        async def dispatch(self, payload):
            if payload.event.value == "PreFileEdit":
                path.write_text("intervening change")

    context = ToolContext(
        session_id="test",
        run_id="test",
        cwd=tmp_path,
        writable_roots=(tmp_path,),
        capabilities=frozenset({"fs.write"}),
        hooks=Hooks(),
    )
    result = await WriteTool().execute(
        WriteParams(
            path=str(path), content="replacement", digest=hashlib.sha256(b"original").hexdigest()
        ),
        context,
    )
    assert not result.ok
    assert result.error.code == "KALASH_TOOL_STALE_READ"
    assert path.read_text() == "intervening change"


async def test_multi_edit_rejects_introduced_ambiguity_atomically(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("A B")
    context = ToolContext(
        session_id="test",
        run_id="test",
        cwd=tmp_path,
        writable_roots=(tmp_path,),
        capabilities=frozenset({"fs.write"}),
    )
    result = await MultiEditTool().execute(
        MultiEditParams(
            path=str(path),
            digest=hashlib.sha256(b"A B").hexdigest(),
            edits=[{"old_str": "A", "new_str": "B"}, {"old_str": "B", "new_str": "C"}],
        ),
        context,
    )
    assert not result.ok
    assert path.read_text() == "A B"
