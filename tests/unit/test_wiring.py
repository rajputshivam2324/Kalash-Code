"""Tests for the subsystems that existed but were never connected.

Each class here covers something that was real, tested-in-isolation code with
zero callers: subagent execution, skill discovery, OS sandbox selection, and
user-defined hooks.
"""

import json

import pytest

from kalash.models.normalize import (
    BlockDelta,
    BlockStart,
    BlockStop,
    MessageStart,
    MessageStop,
    StopReason,
    UsageUpdate,
)
from kalash.runtime.scratchpad import reset_cache
from kalash.tools.base import ToolContext


class FakeProvider:
    name = "fake/model-1"
    context_window = 200_000

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    async def stream(self, messages, *, system=None, tools=None, **kwargs):
        self.requests.append({"system": system, "tools": tools})
        events = self.turns.pop(0) if self.turns else [MessageStop(StopReason.END_TURN)]
        for event in events:
            yield event


class FakeResolution:
    def __init__(self, provider):
        self.ok = True
        self.provider = provider
        self.reason = ""


def final(text):
    return [
        MessageStart(id="m", model="fake/model-1"),
        BlockStart(index=0, block_type="text"),
        BlockDelta(index=0, delta=text),
        BlockStop(index=0),
        UsageUpdate(input_tokens=100, output_tokens=10),
        MessageStop(StopReason.END_TURN),
    ]


def tool_call(name, args, call_id="tu_1"):
    return [
        MessageStart(id="m", model="fake/model-1"),
        BlockStart(index=0, block_type="tool_use", tool_use_id=call_id, tool_name=name),
        BlockDelta(index=0, delta=json.dumps(args)),
        BlockStop(index=0),
        UsageUpdate(input_tokens=100, output_tokens=20),
        MessageStop(StopReason.TOOL_USE),
    ]


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


def install_provider(monkeypatch, turns):
    provider = FakeProvider(turns)
    monkeypatch.setattr(
        "kalash.models.resolve.build_provider", lambda *a, **k: FakeResolution(provider)
    )
    return provider


# --- subagents -------------------------------------------------------------


class TestSubagents:
    @pytest.mark.asyncio
    async def test_run_isolated_returns_only_the_result(self, workspace, monkeypatch):
        install_provider(monkeypatch, [final("the answer is 42")])
        from kalash.runtime.agent import run_isolated

        outcome = await run_isolated("investigate something", cwd=workspace)

        assert outcome["status"] == "completed"
        assert outcome["output"] == "the answer is 42"
        assert outcome["turns"] == 1

    @pytest.mark.asyncio
    async def test_child_does_not_persist_a_session(self, workspace, monkeypatch):
        install_provider(monkeypatch, [final("done")])
        from kalash.runtime.agent import run_isolated
        from kalash.runtime.session import SessionManager

        before = len(SessionManager().list_sessions(limit=50))
        await run_isolated("look at something", cwd=workspace)
        after = len(SessionManager().list_sessions(limit=50))

        # A child's transcript is deliberately discarded, so persisting it would
        # only create orphan rows.
        assert after == before

    @pytest.mark.asyncio
    async def test_task_tool_actually_delegates(self, workspace, monkeypatch):
        install_provider(monkeypatch, [final("child findings here")])
        from kalash.runtime.agent import build_agent
        from kalash.tools.task import TaskParams, TaskTool

        agent, reason = build_agent(cwd=workspace, persist=False, extensions=False)
        assert agent is not None, reason
        ctx = agent.host.build_context()
        env = await TaskTool().execute(
            TaskParams(prompt="find the bug", capabilities=["task.spawn"]), ctx
        )

        assert env.ok
        assert "child findings here" in env.content
        assert env.metadata["status"] == "completed"

    @pytest.mark.asyncio
    async def test_spawn_depth_is_enforced(self, workspace, monkeypatch):
        install_provider(monkeypatch, [final("x")])
        from kalash.tools.task import TaskParams, TaskTool

        ctx = ToolContext(
            session_id="s",
            run_id="r",
            cwd=workspace,
            writable_roots=(workspace,),
            capabilities=frozenset({"task.spawn"}),
            spawn_depth=3,
            max_spawn_depth=3,
        )
        env = await TaskTool().execute(TaskParams(prompt="go deeper"), ctx)

        assert env.ok is False
        assert env.error.code == "KALASH_SUBAGENT_DEPTH_EXCEEDED"

    def test_context_reports_spawn_capacity(self, workspace):
        allowed = ToolContext(
            session_id="s", run_id="r", cwd=workspace, spawn_depth=1, max_spawn_depth=3
        )
        blocked = ToolContext(
            session_id="s", run_id="r", cwd=workspace, spawn_depth=3, max_spawn_depth=3
        )
        assert allowed.can_spawn is True
        assert blocked.can_spawn is False

    @pytest.mark.asyncio
    async def test_capability_attenuation_is_enforced(self, workspace):
        from kalash.tools.task import TaskParams, TaskTool

        ctx = ToolContext(
            session_id="s",
            run_id="r",
            cwd=workspace,
            capabilities=frozenset({"fs.read"}),
        )
        env = await TaskTool().execute(TaskParams(prompt="x", capabilities=["fs.write"]), ctx)
        assert env.ok is False
        assert env.error.code == "KALASH_PERMISSION_DENIED"


# --- skills ----------------------------------------------------------------


class TestSkills:
    def write_skill(self, workspace, name, description, body):
        directory = workspace / ".kalash" / "skills" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
            encoding="utf-8",
        )

    def test_catalog_reaches_the_agent(self, workspace, monkeypatch):
        self.write_skill(workspace, "deploy", "How to deploy this service", "Step 1...")
        install_provider(monkeypatch, [final("ok")])
        from kalash.runtime.agent import build_agent

        agent, _ = build_agent(cwd=workspace, interactive=False)

        assert any(s["name"] == "deploy" for s in agent.skills)
        assert any("deploy this service" in s["description"] for s in agent.skills)

    def test_catalog_excludes_bodies(self, workspace, monkeypatch):
        self.write_skill(workspace, "deploy", "desc", "SECRET_BODY_MARKER")
        install_provider(monkeypatch, [final("ok")])
        from kalash.runtime.agent import build_agent

        agent, _ = build_agent(cwd=workspace, interactive=False)
        # Progressive disclosure: the body must not be in the catalog.
        assert not any("SECRET_BODY_MARKER" in str(s) for s in agent.skills)

    @pytest.mark.asyncio
    async def test_skill_tool_loads_a_body_on_demand(self, workspace):
        self.write_skill(workspace, "deploy", "desc", "BODY_CONTENT_HERE")
        from kalash.tools.skill import SkillParams, SkillTool

        ctx = ToolContext(session_id="s", run_id="r", cwd=workspace)
        env = await SkillTool().execute(SkillParams(name="deploy"), ctx)

        assert env.ok
        assert "BODY_CONTENT_HERE" in env.content

    @pytest.mark.asyncio
    async def test_skill_tool_lists_when_no_name_given(self, workspace):
        self.write_skill(workspace, "alpha", "first", "b")
        self.write_skill(workspace, "beta", "second", "b")
        from kalash.tools.skill import SkillParams, SkillTool

        ctx = ToolContext(session_id="s", run_id="r", cwd=workspace)
        env = await SkillTool().execute(SkillParams(), ctx)

        assert env.ok
        assert "alpha" in env.content and "beta" in env.content
        assert env.metadata["count"] == 14

    @pytest.mark.asyncio
    async def test_unknown_skill_lists_alternatives(self, workspace):
        self.write_skill(workspace, "alpha", "first", "b")
        from kalash.tools.skill import SkillParams, SkillTool

        ctx = ToolContext(session_id="s", run_id="r", cwd=workspace)
        env = await SkillTool().execute(SkillParams(name="nope"), ctx)

        assert env.ok is False
        assert "alpha" in env.error.message

    @pytest.mark.asyncio
    async def test_bundled_skills_available_without_user_installation(self, workspace):
        from kalash.tools.skill import SkillParams, SkillTool

        ctx = ToolContext(session_id="s", run_id="r", cwd=workspace)
        env = await SkillTool().execute(SkillParams(), ctx)
        assert env.ok
        assert env.metadata["count"] == 12
        assert "pdf" in env.content


# --- sandbox ---------------------------------------------------------------


class TestSandboxManager:
    def test_status_is_reportable(self, workspace):
        from kalash.sandbox.manager import get_sandbox_manager

        status = get_sandbox_manager(workspace_root=workspace).status()
        assert status.platform
        assert status.backend
        assert isinstance(status.available, bool)
        assert status.describe()

    def test_wrap_degrades_instead_of_raising(self, workspace):
        from kalash.sandbox.manager import get_sandbox_manager

        manager = get_sandbox_manager(workspace_root=workspace, writable_roots=(workspace,))
        argv, wrapped = manager.wrap(["/bin/bash", "-c", "echo hi"])

        assert argv, "an unavailable sandbox must return a runnable argv"
        assert isinstance(wrapped, bool)
        if not wrapped:
            assert argv == ["/bin/bash", "-c", "echo hi"]

    def test_doctor_import_target_now_exists(self):
        # cli/doctor.py imported kalash.sandbox.manager, which did not exist.
        from kalash.sandbox.manager import SandboxManager

        assert SandboxManager is not None

    @pytest.mark.asyncio
    async def test_shell_reports_whether_it_was_sandboxed(self, workspace):
        from kalash.tools.shell import ShellParams, ShellTool

        ctx = ToolContext(session_id="s", run_id="r", cwd=workspace, writable_roots=(workspace,))
        env = await ShellTool().execute(ShellParams(command="echo sandbox-probe"), ctx)

        assert env.ok
        assert "sandbox-probe" in env.content
        assert "sandboxed" in env.metadata


# --- hooks -----------------------------------------------------------------


class TestHooks:
    def write_hook(self, workspace, payload, *, directory=".kiro/hooks"):
        target = workspace / directory
        target.mkdir(parents=True, exist_ok=True)
        (target / "h.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_hooks_are_discovered(self, workspace):
        self.write_hook(
            workspace,
            {
                "version": "v1",
                "hooks": [
                    {
                        "name": "lint",
                        "trigger": "PostToolUse",
                        "matcher": "write|edit",
                        "action": {"type": "command", "command": "echo linted"},
                    }
                ],
            },
        )
        from kalash.hooks.load import discover_hooks

        found = discover_hooks(workspace)
        assert len(found) == 1
        assert found[0].name == "lint"
        assert found[0].command == "echo linted"
        assert found[0].matcher == "write|edit"

    def test_both_hook_directories_are_read(self, workspace):
        self.write_hook(
            workspace,
            {
                "version": "v1",
                "hooks": [
                    {
                        "name": "a",
                        "trigger": "Stop",
                        "action": {"type": "command", "command": "echo a"},
                    }
                ],
            },
            directory=".kiro/hooks",
        )
        self.write_hook(
            workspace,
            {
                "version": "v1",
                "hooks": [
                    {
                        "name": "b",
                        "trigger": "Stop",
                        "action": {"type": "command", "command": "echo b"},
                    }
                ],
            },
            directory=".kalash/hooks",
        )

        from kalash.hooks.load import discover_hooks

        names = {h.name for h in discover_hooks(workspace)}
        assert names == {"a", "b"}

    def test_malformed_hook_file_is_skipped(self, workspace):
        target = workspace / ".kiro" / "hooks"
        target.mkdir(parents=True)
        (target / "bad.json").write_text("{not json", encoding="utf-8")
        (target / "ok.json").write_text(
            json.dumps(
                {
                    "version": "v1",
                    "hooks": [
                        {
                            "name": "good",
                            "trigger": "Stop",
                            "action": {"type": "command", "command": "echo ok"},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        from kalash.hooks.load import discover_hooks

        found = discover_hooks(workspace)
        assert [h.name for h in found] == ["good"]

    def test_unknown_trigger_is_skipped(self, workspace):
        self.write_hook(
            workspace,
            {
                "version": "v1",
                "hooks": [
                    {
                        "name": "x",
                        "trigger": "NotARealTrigger",
                        "action": {"type": "command", "command": "echo x"},
                    }
                ],
            },
        )
        from kalash.hooks.load import discover_hooks

        assert discover_hooks(workspace) == []

    def test_no_hooks_means_no_runner(self, workspace):
        from kalash.hooks.load import build_hook_runner

        # None keeps a no-op dispatch off the hot path.
        assert build_hook_runner(workspace) is None

    @pytest.mark.asyncio
    async def test_pre_tool_hook_block_refuses_the_call(self, workspace):
        from kalash.core.events import EventBus
        from kalash.hooks.events import HookEvent
        from kalash.hooks.runner import HookResult
        from kalash.runtime.toolhost import ToolHost
        from kalash.tools.builtins import default_registry

        class BlockingRunner:
            async def dispatch(self, payload, *, chain_id=None):
                if payload.event is HookEvent.PRE_TOOL_USE:
                    return [
                        HookResult(
                            hook_id="h",
                            event=payload.event,
                            exit_code=2,
                            stderr="writes to that path are not allowed here",
                            blocked=True,
                        )
                    ]
                return []

        host = ToolHost(
            registry=default_registry(),
            event_bus=EventBus(),
            session_id="s",
            cwd=workspace,
            hooks=BlockingRunner(),
        )
        target = workspace / "blocked.txt"
        out = await host.execute("write", {"path": str(target), "content": "x"}, tool_use_id="t")

        assert out.startswith("REFUSED by a PreToolUse hook")
        assert "not allowed here" in out
        assert "do not retry" in out
        assert not target.exists()

    @pytest.mark.asyncio
    async def test_post_tool_hook_failure_does_not_break_the_turn(self, workspace):
        from kalash.core.events import EventBus
        from kalash.runtime.toolhost import ToolHost
        from kalash.tools.builtins import default_registry

        class ExplodingRunner:
            async def dispatch(self, payload, *, chain_id=None):
                from kalash.hooks.events import HookEvent

                if payload.event is HookEvent.POST_TOOL_USE:
                    raise RuntimeError("hook blew up")
                return []

        host = ToolHost(
            registry=default_registry(),
            event_bus=EventBus(),
            session_id="s",
            cwd=workspace,
            hooks=ExplodingRunner(),
        )
        target = workspace / "written.txt"
        out = await host.execute("write", {"path": str(target), "content": "x"}, tool_use_id="t")

        assert target.exists(), "a PostToolUse failure must not undo the work"
        assert "REFUSED" not in out


# --- regressions the eval suite caught -------------------------------------


class TestSandboxCorrectness:
    """The sandbox must not silently relocate the command.

    `wrap_command` bound the writable roots but never passed `--chdir`, so the
    process started at `/` inside the new mount namespace. `ls -1` returned
    `bin dev etc lib …` instead of the workspace, and every relative path
    resolved against the sandbox root. The command still exited 0, so nothing
    surfaced as an error — three capability evals failed before this was found.
    """

    @pytest.mark.asyncio
    async def test_shell_runs_in_the_workspace(self, workspace):
        from kalash.tools.shell import ShellParams, ShellTool

        (workspace / "marker.txt").write_text("here\n")
        ctx = ToolContext(session_id="s", run_id="r", cwd=workspace, writable_roots=(workspace,))
        env = await ShellTool().execute(ShellParams(command="ls -1"), ctx)

        assert env.ok
        assert "marker.txt" in env.content
        # The sandbox root contents must not leak in.
        assert "lib64" not in env.content

    @pytest.mark.asyncio
    async def test_shell_can_write_in_the_workspace(self, workspace):
        from kalash.tools.shell import ShellParams, ShellTool

        ctx = ToolContext(session_id="s", run_id="r", cwd=workspace, writable_roots=(workspace,))
        env = await ShellTool().execute(ShellParams(command="printf 'written\\n' > out.txt"), ctx)

        assert env.ok, env.error.message if env.error else ""
        assert (workspace / "out.txt").read_text() == "written\n"

    @pytest.mark.asyncio
    async def test_relative_paths_resolve_against_the_workspace(self, workspace):
        from kalash.tools.shell import ShellParams, ShellTool

        (workspace / "sub").mkdir()
        (workspace / "sub" / "deep.txt").write_text("found\n")
        ctx = ToolContext(session_id="s", run_id="r", cwd=workspace, writable_roots=(workspace,))
        env = await ShellTool().execute(ShellParams(command="cat sub/deep.txt"), ctx)

        assert env.ok
        assert "found" in env.content

    @pytest.mark.asyncio
    async def test_shell_rejects_cwd_outside_writable_roots(self, workspace, tmp_path):
        from kalash.tools.shell import ShellParams, ShellTool

        outside = tmp_path / "outside"
        outside.mkdir()
        ctx = ToolContext(session_id="s", run_id="r", cwd=workspace, writable_roots=(workspace,))
        env = await ShellTool().execute(ShellParams(command="echo hi", cwd=str(outside)), ctx)

        assert not env.ok
        assert "outside allowed roots" in env.error.message  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_shell_timeout_terminates_without_crashing(self, workspace):
        from kalash.tools.shell import ShellParams, ShellTool

        ctx = ToolContext(session_id="s", run_id="r", cwd=workspace, writable_roots=(workspace,))
        env = await ShellTool().execute(ShellParams(command="sleep 30", timeout=0.3), ctx)

        assert not env.ok
        assert env.error is not None
        assert env.error.code == "KALASH_TOOL_TIMEOUT"

    @pytest.mark.asyncio
    async def test_shell_cancels_in_flight_command(self, workspace):
        import asyncio

        from kalash.tools.shell import ShellParams, ShellTool

        cancel = asyncio.Event()
        ctx = ToolContext(
            session_id="s",
            run_id="r",
            cwd=workspace,
            writable_roots=(workspace,),
            cancel_event=cancel,
        )

        async def cancel_soon() -> None:
            await asyncio.sleep(0.2)
            cancel.set()

        asyncio.create_task(cancel_soon())
        env = await ShellTool().execute(ShellParams(command="sleep 30", timeout=30), ctx)

        assert not env.ok
        assert env.error is not None
        assert env.error.code == "KALASH_CANCELLED"

    def test_tmpfs_does_not_mask_a_workspace_under_tmp(self, tmp_path):
        """`--tmpfs /tmp` after the binds hid workspaces beneath /tmp."""
        import shutil
        import sys

        if not sys.platform.startswith("linux"):
            pytest.skip("linux-only sandbox backend")
        if shutil.which("bwrap") is None:
            pytest.skip("bwrap not installed")

        from kalash.sandbox.linux import LinuxSandbox
        from kalash.sandbox.policy import SandboxMode, SandboxPolicy

        # tmp_path is under /tmp on Linux, which is the case that regressed.
        assert str(tmp_path).startswith("/tmp")
        policy = SandboxPolicy(mode=SandboxMode.WORKSPACE_WRITE, workspace_root=tmp_path)
        argv = LinuxSandbox(policy=policy).wrap_command(
            ["/bin/bash", "-c", "true"], cwd=str(tmp_path)
        )

        assert "--tmpfs" not in argv, "a tmpfs on /tmp would hide the workspace"
        assert "--chdir" in argv
        assert argv[argv.index("--chdir") + 1] == str(tmp_path)

    def test_wrap_refuses_a_missing_working_directory(self, tmp_path):
        """Skipping --chdir would silently put the process back at /."""
        import shutil
        import sys

        if not sys.platform.startswith("linux"):
            pytest.skip("linux-only sandbox backend")
        if shutil.which("bwrap") is None:
            pytest.skip("bwrap not installed")

        from kalash.sandbox.linux import LinuxSandbox
        from kalash.sandbox.policy import SandboxMode, SandboxPolicy

        policy = SandboxPolicy(mode=SandboxMode.WORKSPACE_WRITE, workspace_root=tmp_path)
        with pytest.raises(RuntimeError, match="does not exist"):
            LinuxSandbox(policy=policy).wrap_command(
                ["/bin/bash", "-c", "true"], cwd=str(tmp_path / "gone")
            )

    def test_wrap_declines_a_backend_that_cannot_set_cwd(self, workspace):
        """Running in the wrong directory is worse than not sandboxing."""
        from kalash.sandbox.manager import get_sandbox_manager

        manager = get_sandbox_manager(workspace_root=workspace, writable_roots=(workspace,))

        class NoCwd:
            def wrap_command(self, cmd):  # no cwd parameter
                return ["wrapped", *cmd]

        manager._backend = NoCwd()  # noqa: SLF001
        argv, wrapped = manager.wrap(["/bin/bash", "-c", "true"], cwd=str(workspace))
        assert wrapped is False
        assert argv == ["/bin/bash", "-c", "true"]


class TestProtectedPathMatching:
    """Protected patterns are relative; the paths reaching the check are absolute.

    Matching only the full path meant `.git/config` never matched, so repository
    trust configuration was writable. Every path suffix is now tested.
    """

    @pytest.mark.parametrize(
        "relative",
        [
            ".env",
            ".env.production",
            ".git/config",
            ".git/hooks/pre-commit",
            "nested/deep/.env",
            "certs/server.pem",
            "secrets/id_rsa",
            "config/credentials.json",
        ],
    )
    def test_protected_relative_patterns_match_absolute_paths(self, workspace, relative):
        from kalash.tools.fs import _is_protected

        assert _is_protected(workspace / relative), f"{relative} must be protected"

    @pytest.mark.parametrize(
        "relative", ["src/main.py", "README.md", "package.json", "envs/dev.yaml"]
    )
    def test_ordinary_files_are_not_protected(self, workspace, relative):
        from kalash.tools.fs import _is_protected

        assert not _is_protected(workspace / relative)

    @pytest.mark.asyncio
    async def test_git_config_write_is_refused(self, workspace):
        from kalash.core.events import EventBus
        from kalash.runtime.toolhost import ToolHost
        from kalash.tools.builtins import default_registry

        host = ToolHost(
            registry=default_registry(),
            event_bus=EventBus(),
            session_id="s",
            cwd=workspace,
        )
        target = workspace / ".git" / "config"
        out = await host.execute(
            "write",
            {"path": str(target), "content": "[core]\n", "create_dirs": True},
            tool_use_id="t",
        )

        assert "protected" in out.lower()
        assert not target.exists()
