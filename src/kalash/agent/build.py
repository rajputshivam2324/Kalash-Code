"""Assemble a runnable agent: adapter, session, tools, permissions, MCP, prompt."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from kalash.agent.context_blocks import ContextBlocks
from kalash.agent.events import AgentEvent, Notice, ToolOutput
from kalash.agent.loop import Agent
from kalash.agent.prompt import build_system_prompt
from kalash.agent.task import SubagentOutcome, TaskTool
from kalash.config import Permissions, Settings
from kalash.hooks import HookRunner
from kalash.llm.http import Adapter
from kalash.llm.providers import (
    ModelRef,
    ProviderError,
    ProviderSpec,
    all_specs,
    build_adapter,
    model_info,
    model_problem,
    parse_model,
)
from kalash.llm.retry import RetryPolicy
from kalash.llm.types import Usage
from kalash.mcp import MCPManager, start_servers
from kalash.paths import kalash_home, project_root
from kalash.permissions import Approver, PermissionEngine
from kalash.session.log import SessionLog
from kalash.session.memory import SessionMemory
from kalash.skills import discover_skills
from kalash.tools.base import Registry, ToolContext
from kalash.tools.bash import BashTool, JobKillTool, JobOutputTool
from kalash.tools.fs import EditTool, GlobTool, GrepTool, ReadTool, WriteTool
from kalash.tools.jobs import JobManager
from kalash.tools.memory import MemoryTool, SessionSearchTool
from kalash.tools.misc import SkillTool, TodoTool, WebFetchTool
from kalash.tools.observe import ObservationTracker
from kalash.tools.plan import ExitPlanModeTool

log = logging.getLogger("kalash.build")

EXPLORE_TOOLS = {
    "read",
    "glob",
    "grep",
    "web_fetch",
    "skill",
    "bash",
    "job_output",
    "memory",
    "session_search",
}


@dataclass
class Runtime:
    agent: Agent
    session: SessionLog
    settings: Settings
    ref: ModelRef
    jobs: JobManager
    mcp: MCPManager | None
    warnings: list[str] = field(default_factory=list)
    _owns_adapter: bool = True

    async def aclose(self, *, keep_session: bool = False, keep_adapter: bool = False) -> None:
        await self.jobs.shutdown()
        if self.mcp is not None:
            await self.mcp.close()
        if self._owns_adapter and not keep_adapter:
            await self.agent.adapter.aclose()
        if not keep_session:
            self.session.close()


def resolve_model(
    settings: Settings, specs: dict[str, ProviderSpec], explicit: str | None
) -> ModelRef:
    from kalash.credentials import get_selected_model

    value = explicit or settings.model or get_selected_model()
    if not value:
        raise ProviderError(
            "no model selected: pass -m provider/model, set `model` in settings, or run "
            "`kalash model`"
        )
    return parse_model(value, specs)


async def create_runtime(
    *,
    cwd: Path,
    settings: Settings,
    model: str | None = None,
    session: SessionLog | None = None,
    approver: Approver | None = None,
    emit: Callable[[AgentEvent], None] | None = None,
    adapter: Adapter | None = None,
    with_mcp: bool = True,
    parent: str | None = None,
    sandbox_mode: str | None = None,
    tool_names: set[str] | None = None,
    subagent: bool = False,
    transport: object | None = None,
) -> Runtime:
    cwd = cwd.resolve()
    root = project_root(cwd).resolve()
    specs = all_specs(settings.providers)
    ref = (
        ModelRef(adapter.provider, adapter.model)
        if adapter
        else resolve_model(settings, specs, model)
    )
    spec = specs.get(ref.provider)
    owns_adapter = adapter is None
    if adapter is None:
        adapter = build_adapter(
            ref,
            specs,
            context_window=settings.context_window,
            idle_timeout=settings.stream_idle_timeout,
            transport=transport,  # type: ignore[arg-type]
        )
    pricing = settings.pricing.get(str(ref))
    info = model_info(spec, ref.model, window=settings.context_window) if spec else None
    warnings: list[str] = []
    problem = model_problem(ref.provider, ref.model)
    if problem is not None:
        warnings.append(problem[1])
    created_session = session is None
    try:
        if session is None:
            session = SessionLog.create(root, cwd, str(ref), parent=parent)
        mode = sandbox_mode or settings.permissions.sandbox
        home = kalash_home()
        session_tmp = home / "tmp" / session.id
        spill = home / "spill" / session.id
        jobs = JobManager(spill / "jobs")
        writable = [
            p for raw in settings.sandbox.writable_roots if (p := Path(raw).expanduser()).exists()
        ]
        writable = [p.resolve() for p in writable]
        ctx = ToolContext(
            cwd=cwd,
            root=root,
            session_id=session.id,
            observations=ObservationTracker(),
            jobs=jobs,
            sandbox_mode=mode,
            network=settings.sandbox.network,
            writable_roots=writable,
            spill_dir=spill,
            extras={"session_tmp": session_tmp, "pass_env": settings.sandbox.pass_env},
        )
        permissions = PermissionEngine(
            mode=mode,
            approval=settings.permissions.approval,
            root=root,
            writable_roots=writable,
            session_tmp=session_tmp,
            network=settings.sandbox.network,
            deny_rules=settings.permissions.deny,
            ask_rules=settings.permissions.ask,
            approver=approver,
        )
        skills = discover_skills(root)
        memory = SessionMemory(session)
        tools = [
            ReadTool(),
            WriteTool(),
            EditTool(),
            GlobTool(),
            GrepTool(),
            BashTool(),
            JobOutputTool(),
            JobKillTool(),
            TodoTool(),
            WebFetchTool(),
            ExitPlanModeTool(),
            MemoryTool(memory),
            SessionSearchTool(session),
        ]
        if skills:
            tools.append(SkillTool(skills))
        registry = Registry(tools)
        manager: MCPManager | None = None
        if with_mcp and settings.mcp_servers:
            manager = await start_servers(settings.mcp_servers, set(registry.names()))
            warnings.extend(manager.warnings)
            for tool in manager.tools:
                registry.add(tool)
        holder: dict[str, Agent] = {}

        if not subagent:

            async def spawn(
                kind: str, prompt: str, description: str, progress: Callable[[str], None]
            ) -> SubagentOutcome:
                return await _run_subagent(
                    holder["agent"],
                    kind,
                    prompt,
                    description,
                    progress,
                    cwd=cwd,
                    settings=settings,
                )

            registry.add(TaskTool(spawn))
        if tool_names is not None:
            registry = registry.subset(tool_names)
        system = build_system_prompt(
            tool_names=set(registry.names()),
            extra_instructions=settings.instructions,
            subagent=subagent,
        )
        context = ContextBlocks(
            root=root,
            cwd=cwd,
            skills=skills if "skill" in registry.names() else {},
            network=settings.sandbox.network,
        )
        hooks = HookRunner(settings.hooks, cwd, session.id)
        agent = Agent(
            adapter=adapter,
            session=session,
            registry=registry,
            settings=settings,
            tool_ctx=ctx,
            permissions=permissions,
            system_prompt=system,
            emit=emit,
            hooks=hooks if hooks.has_any() and not subagent else None,
            retry=RetryPolicy(),
            default_max_tokens=(info.max_output if info else None),
            reasoning_effort=settings.reasoning_effort,
            pricing=pricing,
            info=info,
            context=context,
            memory=memory,
        )
        holder["agent"] = agent
        ctx.extras["usage_sink"] = lambda usage, cost, why: session.append(
            "usage", usage=usage.to_dict(), cost=cost, purpose=why
        )
        for line in warnings:
            agent.emit(Notice(line, "warning"))
        if agent.effort_notice:
            agent.emit(Notice(agent.effort_notice, "warning"))
        return Runtime(agent, session, settings, ref, jobs, manager, warnings, owns_adapter)
    except BaseException:
        if session is not None and created_session:
            session.close()
        if owns_adapter:
            await adapter.aclose()
        raise


async def _run_subagent(
    parent: Agent,
    kind: str,
    prompt: str,
    description: str,
    progress: Callable[[str], None],
    *,
    cwd: Path,
    settings: Settings,
) -> SubagentOutcome:
    explore = kind == "explore"
    # Modes can change after startup. Delegation must use the live policy rather than
    # the settings that originally created the parent (which may still allow writes).
    permissions = parent.permissions
    child_settings = settings.model_copy(
        update={
            "permissions": Permissions.model_validate(
                {
                    "sandbox": "read-only" if explore else permissions.mode,
                    "approval": permissions.approval,
                    "deny": list(permissions.deny_rules),
                    "ask": list(permissions.ask_rules),
                }
            )
        }
    )

    def relay(event: AgentEvent) -> None:
        from kalash.agent.events import ToolRunning

        if isinstance(event, ToolRunning):
            progress(f"  > {event.summary}\n")
        elif isinstance(event, ToolOutput):
            return

    runtime = await create_runtime(
        cwd=cwd,
        settings=child_settings,
        approver=permissions.approver,
        emit=relay,
        adapter=parent.adapter,
        with_mcp=False,
        parent=parent.session.id,
        subagent=True,
        tool_names=EXPLORE_TOOLS
        if explore
        else {
            n
            for n in parent.registry.names()
            if n not in {"task", "todo_write"} and not n.startswith("mcp__")
        },
        model=str(parent.model_label),
    )
    try:
        result = await runtime.agent.run_turn(prompt)
    finally:
        await runtime.aclose()
    usage = result.usage if result.usage.input_tokens is not None else Usage()
    return SubagentOutcome(result.text, result.reason, usage, result.cost, result.error)
