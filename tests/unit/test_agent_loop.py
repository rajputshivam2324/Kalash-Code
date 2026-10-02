"""End-to-end tests for the agent loop, tool host, and permission gate.

These are the tests that would have caught the integration gap: the loop was
structurally complete but had never executed, because nothing constructed it and
its ``ToolRegistry`` interface did not match the concrete registry. Nothing here
touches the network — a fake gateway drives real tools through the real gate.

The central assertion is the one the product is judged on: after a turn in which
the model calls ``write``, **a file exists on disk**.
"""

import json

import pytest

from kalash.core.budget import BudgetState
from kalash.core.events import EventBus
from kalash.models.normalize import (
    BlockDelta,
    BlockStart,
    BlockStop,
    MessageStart,
    MessageStop,
    Role,
    StopReason,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UsageUpdate,
)
from kalash.permissions.policy import (
    ConfirmationClass,
    Decision,
    PermissionPolicy,
    RiskClass,
)
from kalash.permissions.prompt import (
    ApprovalPrompt,
    ApprovalResponse,
    PromptContext,
    PromptResult,
)
from kalash.runtime.context import ContextAssembler
from kalash.runtime.loop import AgentLoop, TerminationReason
from kalash.runtime.prompt import build_system_prompt, discover_project_instructions
from kalash.runtime.scratchpad import reset_cache
from kalash.runtime.serialize import (
    deserialize_blocks,
    rehydrate_messages,
    serialize_blocks,
    split_system,
)
from kalash.runtime.toolhost import (
    ToolCategory,
    ToolHost,
    capabilities_for_mode,
    normalize_sandbox_mode,
)
from kalash.tools.builtins import default_registry

# --- fakes -----------------------------------------------------------------


class FakeProvider:
    name = "fake/model-1"


class FakeGateway:
    """Yields scripted turns. Each turn is a list of stream events."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.primary = FakeProvider()
        self.calls: list[dict] = []

    async def stream(self, messages, *, system=None, tools=None, **kwargs):
        self.calls.append({"messages": messages, "system": system, "tools": tools, **kwargs})
        events = self.turns.pop(0) if self.turns else [MessageStop(StopReason.END_TURN)]
        for event in events:
            yield event


def tool_turn(name: str, args: dict, *, call_id: str = "tu_1"):
    """A turn where the model emits one tool call."""
    return [
        MessageStart(id="msg_1", model="fake/model-1"),
        BlockStart(index=0, block_type="tool_use", tool_use_id=call_id, tool_name=name),
        BlockDelta(index=0, delta=json.dumps(args)),
        BlockStop(index=0),
        UsageUpdate(input_tokens=100, output_tokens=20),
        MessageStop(StopReason.TOOL_USE),
    ]


def text_turn(text: str):
    """A turn where the model just answers."""
    return [
        MessageStart(id="msg_2", model="fake/model-1"),
        BlockStart(index=0, block_type="text"),
        BlockDelta(index=0, delta=text),
        BlockStop(index=0),
        UsageUpdate(input_tokens=120, output_tokens=15),
        MessageStop(StopReason.END_TURN),
    ]


class AutoApprover:
    """UI adapter that always answers with a fixed response."""

    def __init__(self, response=ApprovalResponse.ALLOW_ONCE):
        self.response = response
        self.seen: list[PromptContext] = []

    async def show_approval_prompt(self, context: PromptContext) -> PromptResult:
        self.seen.append(context)
        return PromptResult(response=self.response)

    async def show_info(self, message: str) -> None:
        return None


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KALASH_HOME", str(tmp_path / "home"))
    reset_cache()
    yield
    reset_cache()


def make_host(tmp_path, *, mode="build", ui=None, sandbox="workspace-write"):
    bus = EventBus()
    policy = PermissionPolicy(event_bus=bus, sandbox_mode=normalize_sandbox_mode(sandbox))
    approval = ApprovalPrompt(event_bus=bus, ui=ui, non_interactive=ui is None)
    return ToolHost(
        registry=default_registry(),
        event_bus=bus,
        session_id="ses_it",
        cwd=tmp_path,
        sandbox_mode=sandbox,
        mode=mode,
        policy=policy,
        approval=approval,
    )


def make_loop(gateway, host):
    budget = BudgetState(max_tokens=100_000)
    return AgentLoop(
        gateway=gateway,
        tool_registry=host,
        session_repo=None,
        event_bus=host.event_bus,
        budget=budget,
        assembler=ContextAssembler(budget=budget, context_window=200_000),
        session_id="ses_it",
        system_prompt=build_system_prompt(),
    )


async def send(loop, text):
    from kalash.models.normalize import Message

    return await loop.run(
        user_message=Message(role=Role.USER, content=[TextBlock(text=text)]),
        system_identity=loop.system_prompt,
    )


# --- the headline test -----------------------------------------------------


class TestAgentActuallyActs:
    @pytest.mark.asyncio
    async def test_model_tool_call_creates_a_real_file(self, tmp_path):
        target = tmp_path / "app" / "index.js"
        gateway = FakeGateway(
            [
                tool_turn(
                    "write",
                    {
                        "path": str(target),
                        "content": "console.log('hello');\n",
                        "create_dirs": True,
                    },
                ),
                text_turn("Created app/index.js."),
            ]
        )
        host = make_host(tmp_path, ui=AutoApprover())
        loop = make_loop(gateway, host)

        result = await send(loop, "create a node entrypoint")

        assert target.exists(), "the agent must actually write to disk"
        assert target.read_text() == "console.log('hello');\n"
        assert result.termination_reason is TerminationReason.NO_TOOL_CALLS
        assert result.final_response == "Created app/index.js."

    @pytest.mark.asyncio
    async def test_tools_are_sent_to_the_provider(self, tmp_path):
        gateway = FakeGateway([text_turn("hi")])
        loop = make_loop(gateway, make_host(tmp_path))
        await send(loop, "hello")

        sent = gateway.calls[0]["tools"]
        assert sent, "tools= must be populated; an empty list is the original bug"
        names = {t["name"] for t in sent}
        assert {"read", "write", "edit", "shell"} <= names
        # Anthropic requires input_schema; the OpenAI provider reads it too.
        assert all("input_schema" in t for t in sent)

    @pytest.mark.asyncio
    async def test_system_prompt_reaches_the_provider(self, tmp_path):
        gateway = FakeGateway([text_turn("hi")])
        loop = make_loop(gateway, make_host(tmp_path))
        await send(loop, "hello")

        system = gateway.calls[0]["system"]
        # Assert on the load-bearing property rather than exact wording: the
        # model must be told it operates on a real filesystem. Its absence is
        # what produced "As a text-only AI I don't have access to your
        # filesystem" while sitting in the user's repository.
        assert system
        assert "filesystem" in system.lower()
        assert "<cache_breakpoint/>" not in system

    @pytest.mark.asyncio
    async def test_tool_result_is_fed_back_and_loop_continues(self, tmp_path):
        (tmp_path / "a.txt").write_text("contents\n")
        gateway = FakeGateway(
            [
                tool_turn("read", {"path": str(tmp_path / "a.txt")}),
                text_turn("done"),
            ]
        )
        loop = make_loop(gateway, make_host(tmp_path, ui=AutoApprover()))
        await send(loop, "read it")

        # Second request must carry the assistant tool_use and the tool_result.
        second = gateway.calls[1]["messages"]
        assert any(isinstance(b, ToolUseBlock) for m in second for b in m.content)
        assert any(isinstance(b, ToolResultBlock) for m in second for b in m.content)

    @pytest.mark.asyncio
    async def test_history_carries_across_turns(self, tmp_path):
        gateway = FakeGateway([text_turn("first"), text_turn("second")])
        loop = make_loop(gateway, make_host(tmp_path))

        await send(loop, "one")
        carried = loop.conversation
        assert len(carried) == 2

        from kalash.models.normalize import Message

        await loop.run(
            user_message=Message(role=Role.USER, content=[TextBlock(text="two")]),
            system_identity=loop.system_prompt,
            recent_turns=carried,
        )
        assert len(gateway.calls[1]["messages"]) >= 3


# --- the gate --------------------------------------------------------------


class TestPermissionGate:
    @pytest.mark.asyncio
    async def test_plan_mode_refuses_writes_and_withholds_tools(self, tmp_path):
        host = make_host(tmp_path, mode="plan", ui=AutoApprover())
        names = {s["name"] for s in host.schemas()}
        assert "read" in names
        assert "write" not in names and "shell" not in names

        out = await host.execute(
            "write", {"path": str(tmp_path / "x"), "content": "y"}, tool_use_id="t1"
        )
        assert out.startswith("REFUSED")
        assert "plan mode" in out
        assert not (tmp_path / "x").exists()

    @pytest.mark.asyncio
    async def test_ordinary_workspace_writes_do_not_prompt(self, tmp_path):
        """Documented design: workspace-write grants filesystem.write.

        Prompting on every in-workspace edit would make the agent unusable, so
        the gate reserves prompts for boundary crossings and destructive acts.
        """
        ui = AutoApprover(ApprovalResponse.DENY)
        host = make_host(tmp_path, ui=ui)
        target = tmp_path / "ok.txt"
        await host.execute("write", {"path": str(target), "content": "x"}, tool_use_id="t1")
        assert target.exists()
        assert ui.seen == []

    @pytest.mark.asyncio
    async def test_user_denial_blocks_a_destructive_command(self, tmp_path):
        ui = AutoApprover(ApprovalResponse.DENY)
        host = make_host(tmp_path, ui=ui)
        victim = tmp_path / "victim.txt"
        victim.write_text("keep me")

        out = await host.execute("shell", {"command": f"rm -rf {victim}"}, tool_use_id="t1")
        assert out.startswith("REFUSED by the user")
        assert victim.exists(), "a denied command must not have run"

    @pytest.mark.asyncio
    async def test_no_approval_channel_denies_rather_than_hangs(self, tmp_path):
        host = make_host(tmp_path, ui=None)  # non-interactive
        victim = tmp_path / "victim.txt"
        victim.write_text("keep me")

        out = await host.execute("shell", {"command": f"rm -rf {victim}"}, tool_use_id="t1")
        assert out.startswith("REFUSED")
        assert victim.exists()

    @pytest.mark.asyncio
    async def test_reads_are_not_gated(self, tmp_path):
        (tmp_path / "r.txt").write_text("visible\n")
        host = make_host(tmp_path, ui=None)  # would deny any prompt
        out = await host.execute("read", {"path": str(tmp_path / "r.txt")}, tool_use_id="t")
        assert "visible" in out

    @pytest.mark.asyncio
    async def test_session_grant_stops_re_asking(self, tmp_path):
        ui = AutoApprover(ApprovalResponse.ALLOW_SESSION)
        host = make_host(tmp_path, ui=ui)
        for index in range(3):
            target = tmp_path / f"f{index}.txt"
            target.write_text("x")
            await host.execute("shell", {"command": f"rm -rf {target}"}, tool_use_id=f"t{index}")
        assert len(ui.seen) == 1, "an allow-session grant must not re-prompt"

    @pytest.mark.asyncio
    async def test_shell_grant_is_scoped_to_the_program(self, tmp_path):
        ui = AutoApprover(ApprovalResponse.ALLOW_SESSION)
        host = make_host(tmp_path, ui=ui)
        await host.execute("shell", {"command": "chmod 777 somefile"}, tool_use_id="t1")
        await host.execute("shell", {"command": "rm -rf /tmp/whatever"}, tool_use_id="t2")
        # Approving `chmod` for the session must not also approve `rm -rf`.
        assert len(ui.seen) == 2

    @pytest.mark.asyncio
    async def test_protected_path_is_refused_even_when_approved(self, tmp_path):
        host = make_host(tmp_path, ui=AutoApprover(ApprovalResponse.ALLOW_ALWAYS))
        out = await host.execute(
            "write",
            {"path": str(tmp_path / ".env"), "content": "SECRET=1"},
            tool_use_id="t1",
        )
        assert "protected" in out.lower()
        assert not (tmp_path / ".env").exists()

    @pytest.mark.asyncio
    async def test_write_outside_workspace_is_refused(self, tmp_path):
        host = make_host(tmp_path, ui=AutoApprover())
        outside = tmp_path.parent / "escaped.txt"
        out = await host.execute("write", {"path": str(outside), "content": "x"}, tool_use_id="t1")
        assert "REFUSED" in out or "DENIED" in out.upper()
        assert not outside.exists()

    @pytest.mark.asyncio
    async def test_dangerous_command_reaches_the_prompt_with_context(self, tmp_path):
        ui = AutoApprover(ApprovalResponse.DENY)
        host = make_host(tmp_path, ui=ui)
        await host.execute("shell", {"command": "git push --force origin main"}, tool_use_id="t1")
        assert ui.seen, "a force push must prompt"
        context = ui.seen[0]
        assert context.risk_class == RiskClass.WRITE_REMOTE.value
        assert ConfirmationClass.DESTRUCTIVE_GIT.value in context.confirmation_classes
        assert context.reversibility == "irreversible"


class TestPolicyStages:
    @pytest.mark.asyncio
    async def test_confirmation_class_escalates_a_stage_4_allow(self):
        """Stage 6 must run even when workspace-write already allowed the write."""
        from kalash.permissions.policy import PolicyRequest

        policy = PermissionPolicy(event_bus=EventBus(), sandbox_mode="workspace_write")
        plain = await policy.evaluate(
            PolicyRequest(tool_name="write", risk_class=RiskClass.WRITE, paths=["a.txt"])
        )
        assert plain.decision is Decision.ALLOW

        flagged = await policy.evaluate(
            PolicyRequest(
                tool_name="write",
                risk_class=RiskClass.WRITE,
                paths=["/etc/hosts"],
                confirmation_classes=[ConfirmationClass.BOUNDARY_CROSSING],
            )
        )
        assert flagged.decision is Decision.ASK

    @pytest.mark.asyncio
    async def test_read_only_sandbox_denies_writes(self):
        from kalash.permissions.policy import PolicyRequest

        policy = PermissionPolicy(event_bus=EventBus(), sandbox_mode="read_only")
        result = await policy.evaluate(
            PolicyRequest(tool_name="write", risk_class=RiskClass.WRITE, paths=["a"])
        )
        assert result.decision is Decision.DENY

    def test_hyphenated_config_mode_is_normalized(self):
        # Config writes "workspace-write"; the policy matches "workspace_write".
        assert normalize_sandbox_mode("workspace-write") == "workspace_write"
        assert "fs.write" in capabilities_for_mode("workspace-write")
        assert "fs.write" not in capabilities_for_mode("read-only")


class TestToolHostMechanics:
    def test_categories_come_from_declared_side_effects(self, tmp_path):
        host = make_host(tmp_path)
        assert host.category("read") is ToolCategory.READ
        assert host.category("write") is ToolCategory.WRITE
        assert host.category("shell") is ToolCategory.EXEC
        assert host.category("nonexistent") is ToolCategory.WRITE

    def test_context_is_populated(self, tmp_path):
        ctx = make_host(tmp_path).build_context()
        assert ctx.writable_roots, "empty roots would make fs tools fail closed"
        assert tmp_path.resolve() in ctx.writable_roots
        assert "fs.write" in ctx.capabilities

    def test_read_only_mode_has_no_writable_roots(self, tmp_path):
        ctx = make_host(tmp_path, sandbox="read-only").build_context()
        assert ctx.writable_roots == ()

    def test_large_output_is_deferred_to_the_scratchpad(self, tmp_path):
        from kalash.permissions.classify import classify_tool_call

        host = make_host(tmp_path)
        risk = classify_tool_call("shell", {"command": "npm run build"})
        big = "compiling module\n" * 4000

        deferred = host._maybe_defer("shell", big, risk)  # noqa: SLF001

        assert deferred is not None
        assert "expand(#x1)" in deferred
        assert len(deferred) < len(big) / 10

    def test_small_output_is_left_inline(self, tmp_path):
        from kalash.permissions.classify import classify_tool_call

        host = make_host(tmp_path)
        risk = classify_tool_call("shell", {"command": "echo hi"})
        assert host._maybe_defer("shell", "hi\n", risk) is None  # noqa: SLF001

    def test_reads_are_never_deferred(self, tmp_path):
        from kalash.permissions.classify import classify_tool_call

        # A truncated read would break the exact-match edit that follows it.
        host = make_host(tmp_path)
        risk = classify_tool_call("read", {"path": "a.py"})
        assert host._maybe_defer("read", "x\n" * 9000, risk) is None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_tool_error_is_returned_not_raised(self, tmp_path):
        host = make_host(tmp_path, ui=AutoApprover())
        out = await host.execute("read", {"path": str(tmp_path / "missing")}, tool_use_id="t")
        assert out.startswith("ERROR")

    @pytest.mark.asyncio
    async def test_unknown_tool_is_reported(self, tmp_path):
        host = make_host(tmp_path, ui=AutoApprover())
        out = await host.execute("no_such_tool", {}, tool_use_id="t")
        assert "ERROR" in out or "REFUSED" in out


# --- serialization / resume ------------------------------------------------


class TestSerialization:
    def test_round_trip_preserves_tool_pairing(self):
        blocks = [
            TextBlock(text="calling a tool"),
            ToolUseBlock(id="tu_9", name="read", input={"path": "a.py"}),
        ]
        restored = deserialize_blocks(serialize_blocks(blocks))
        assert len(restored) == 2
        assert isinstance(restored[1], ToolUseBlock)
        assert restored[1].id == "tu_9"
        assert restored[1].input == {"path": "a.py"}

    def test_tool_result_round_trip(self):
        blocks = [ToolResultBlock(tool_use_id="tu_1", content="output", is_error=True)]
        restored = deserialize_blocks(serialize_blocks(blocks))
        assert isinstance(restored[0], ToolResultBlock)
        assert restored[0].is_error is True

    def test_legacy_plain_text_rows_still_load(self):
        assert deserialize_blocks("just text")[0].text == "just text"

    def test_rehydrate_orders_and_merges_roles(self):
        rows = [
            {"role": "user", "content": serialize_blocks([TextBlock(text="hi")])},
            {"role": "assistant", "content": serialize_blocks([TextBlock(text="hello")])},
            {"role": "user", "content": serialize_blocks([TextBlock(text="more")])},
            {"role": "user", "content": serialize_blocks([TextBlock(text="and more")])},
        ]
        messages = rehydrate_messages(rows)
        assert [m.role for m in messages] == [Role.USER, Role.ASSISTANT, Role.USER]
        assert len(messages[2].content) == 2

    def test_rehydrate_marks_unknown_tool_outcomes(self):
        rows = [
            {
                "role": "assistant",
                "content": serialize_blocks(
                    [
                        TextBlock(text="working"),
                        ToolUseBlock(id="tu_orphan", name="read", input={}),
                    ]
                ),
            },
        ]
        messages = rehydrate_messages(rows)
        assert isinstance(messages[0].content[-1], ToolUseBlock)
        outcome = messages[1].content[0]
        assert isinstance(outcome, ToolResultBlock)
        assert outcome.tool_use_id == "tu_orphan" and outcome.is_error
        assert "Outcome unknown" in outcome.content

    def test_rehydrate_keeps_answered_tool_calls(self):
        rows = [
            {
                "role": "assistant",
                "content": serialize_blocks(
                    [
                        ToolUseBlock(id="tu_1", name="read", input={}),
                    ]
                ),
            },
            {
                "role": "user",
                "content": serialize_blocks(
                    [
                        ToolResultBlock(tool_use_id="tu_1", content="ok"),
                    ]
                ),
            },
        ]
        messages = rehydrate_messages(rows)
        assert any(isinstance(b, ToolUseBlock) for m in messages for b in m.content)

    def test_rehydrate_skips_system_rows(self):
        rows = [{"role": "system", "content": serialize_blocks([TextBlock(text="x")])}]
        assert rehydrate_messages(rows) == []

    def test_split_system_extracts_and_drops_breakpoints(self):
        from kalash.models.normalize import Message

        messages = [
            Message(role=Role.SYSTEM, content=[TextBlock(text="contract")]),
            Message(role=Role.SYSTEM, content=[TextBlock(text="<cache_breakpoint/>")]),
            Message(role=Role.USER, content=[TextBlock(text="hi")]),
        ]
        system, conversation = split_system(messages)
        assert system == "contract"
        assert len(conversation) == 1
        assert conversation[0].role is Role.USER


class TestProjectInstructions:
    def test_nearest_file_wins_by_being_last(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALASH_HOME", str(tmp_path / "home"))
        root = tmp_path / "proj"
        nested = root / "pkg" / "sub"
        nested.mkdir(parents=True)
        (root / "KALASH.md").write_text("use npm")
        (nested / "KALASH.md").write_text("use pnpm here")

        found = discover_project_instructions(nested)
        assert len(found.chain) == 2
        assert found.chain[-1] == "use pnpm here"

    def test_agents_md_is_also_honoured(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALASH_HOME", str(tmp_path / "home"))
        (tmp_path / "AGENTS.md").write_text("house style")
        found = discover_project_instructions(tmp_path)
        assert found.chain == ("house style",)

    def test_absent_files_are_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALASH_HOME", str(tmp_path / "home"))
        assert discover_project_instructions(tmp_path).chain == ()
