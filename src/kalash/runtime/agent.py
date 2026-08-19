"""Agent assembly — one call to get a working agent.

Everything needed to run an agent turn existed in this codebase and none of it
was connected. The model gateway worked. The tools worked. The loop was
structurally complete. The permission policy was correct. But no single place
constructed them together, so the only thing that ever ran was a bare text
stream with no tools attached.

:func:`build_agent` is that place. It resolves a provider, opens or resumes a
session, registers the built-in tools, wires the permission gate to a terminal
approval prompt, builds the system prompt and the ``KALASH.md`` chain, and hands
back an :class:`Agent` whose :meth:`Agent.send` runs a real multi-turn agentic
loop.

:class:`Agent` also owns conversation continuity. ``AgentLoop.run`` is
per-request and returns a result; the agent keeps the accumulated history and
feeds it back in, which is what makes turn two able to see turn one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kalash.core.budget import BudgetState
from kalash.core.config import KalashConfig, load_config
from kalash.core.events import EventBus, get_event_bus
from kalash.models.gateway import ModelGateway
from kalash.models.normalize import Message, Role, TextBlock
from kalash.permissions.console import build_approval_prompt, is_interactive
from kalash.permissions.policy import PermissionPolicy, ProtectedPath
from kalash.runtime.context import ContextAssembler
from kalash.runtime.loop import AgentLoop, LoopResult
from kalash.runtime.prompt import (
    build_system_prompt,
    detect_environment,
    discover_project_instructions,
)
from kalash.runtime.scratchpad import get_scratchpad
from kalash.runtime.serialize import rehydrate_messages
from kalash.runtime.session import SessionManager
from kalash.runtime.toolhost import ToolHost, normalize_sandbox_mode
from kalash.sandbox.policy import DEFAULT_PROTECTED_PATHS
from kalash.storage.repositories.sessions import SessionRepository
from kalash.tools.builtins import default_registry

logger = logging.getLogger(__name__)

# Conservative default. A model with a smaller window still works; the assembler
# applies pressure compression against whatever it is told.
DEFAULT_CONTEXT_WINDOW = 200_000


@dataclass
class Agent:
    """A ready-to-run agent with conversation continuity."""

    loop: AgentLoop
    host: ToolHost
    session_id: str
    cwd: Path
    instructions: tuple[str, ...] = ()
    skills: tuple[dict[str, str], ...] = ()
    mode: str = "build"
    history: list[Message] = field(default_factory=list)

    @property
    def budget(self) -> BudgetState:
        return self.loop.budget

    def set_mode(self, mode: str) -> None:
        """Switch between build and plan.

        The mode changes both the prompt and the tools actually offered, so plan
        mode is enforced by the runtime rather than requested of the model.
        """
        normalized = "plan" if mode.strip().lower() == "plan" else "build"
        self.mode = normalized
        self.host.mode = normalized
        self.loop.system_prompt = build_system_prompt(mode=normalized)

    def scratchpad_blocks(self) -> list[str]:
        """Durable working state for the context assembler's memory slot.

        The plan comes first and is never dropped. An agent that forgets its own
        checklist mid-build restarts work it already finished, and the user loses
        the only visible record of progress.
        """
        from kalash.tools.todo import render_task_list

        pad = get_scratchpad(self.session_id)
        candidates = (
            render_task_list(self.session_id),
            pad.render_notes(),
            pad.render_index(),
        )
        return [text for text in candidates if text]

    def plan(self) -> str:
        """Current plan, rendered. Empty when none has been recorded."""
        from kalash.tools.todo import render_task_list

        return render_task_list(self.session_id)

    async def send(
        self,
        prompt: str,
        *,
        on_text_delta: Any | None = None,
    ) -> LoopResult:
        """Run one agentic turn to completion."""
        user_message = Message(role=Role.USER, content=[TextBlock(text=prompt)])
        await self.loop.persist_user_message(user_message)

        environment = detect_environment(self.cwd)

        memory_blocks = list(self.scratchpad_blocks())
        try:
            from kalash.core.config import load_config

            if load_config().memory.enabled:
                from kalash.memory.session import get_session_memory

                existing = "\n".join(self.instructions) + "\n".join(
                    self.scratchpad_blocks()
                )
                recalled = await get_session_memory(
                    self.session_id, self.cwd
                ).recall_blocks(prompt, limit=12, existing_context=existing)
                memory_blocks = recalled + memory_blocks
        except Exception:
            logger.debug("memory recall skipped", exc_info=True)

        result = await self.loop.run(
            user_message=user_message,
            system_identity=self.loop.system_prompt,
            skills_catalog=[dict(s) for s in self.skills],
            kalash_md_chain=list(self.instructions),
            memory_blocks=memory_blocks,
            recent_turns=self.history,
            on_text_delta=on_text_delta,
            environment=environment,
        )

        # Carry the accumulated conversation (including tool calls and their
        # results) into the next turn.
        self.history = self.loop.conversation

        try:
            from kalash.core.config import load_config

            if load_config().memory.enabled:
                from kalash.memory.capture import capture_turn_memories

                await capture_turn_memories(
                    session_id=self.session_id,
                    cwd=self.cwd,
                    user_prompt=prompt,
                    conversation=self.loop.conversation,
                    turn_seq=self.loop.budget.turns_used,
                )
        except Exception:
            logger.debug("turn memory capture skipped", exc_info=True)

        return result

    def cancel(self) -> None:
        self.loop.cancel()

    def clear_history(self) -> None:
        """Drop conversation history without ending the session."""
        self.history = []


def build_agent(
    *,
    cwd: Path | None = None,
    provider_id: str | None = None,
    model_id: str | None = None,
    mode: str = "build",
    session_id: str | None = None,
    resume: bool = False,
    config: KalashConfig | None = None,
    event_bus: EventBus | None = None,
    interactive: bool | None = None,
    persist: bool = True,
    max_iterations: int = 50,
) -> tuple[Agent | None, str]:
    """Assemble an agent.

    Returns ``(agent, "")`` on success or ``(None, reason)`` when a provider
    cannot be resolved, so callers can show the reason without exception
    handling on a routine configuration problem.
    """
    from kalash.models.resolve import build_provider

    resolution = build_provider(provider_id, model_id)
    if not resolution.ok or resolution.provider is None:
        return None, resolution.reason or "no model provider configured"

    # The id the provider actually resolved to, which is what limits are keyed
    # on. The caller's model_id may be None (use the configured default).
    resolved_model = (
        model_id
        or getattr(resolution, "model_id", "")
        or getattr(resolution.provider, "model", "")
        or getattr(resolution.provider, "name", "")
    )

    base = (cwd or Path.cwd()).resolve()
    settings = config or load_config()
    bus = event_bus or get_event_bus()

    # -- session -----------------------------------------------------------
    manager = SessionManager(settings)
    repo: SessionRepository | None = None
    history: list[Message] = []
    resolved_session_id = session_id or ""
    grant_store = None

    if persist:
        try:
            if resume and session_id:
                session = manager.resume(session_id)
                resolved_session_id = session.id
            else:
                session = manager.create(str(base))
                resolved_session_id = session.id
            engine = manager._ensure_engine()  # noqa: SLF001
            repo = SessionRepository(engine)
            from kalash.permissions.grants import GrantStore

            grant_store = GrantStore(engine=engine, session_id=resolved_session_id)
        except Exception:
            # Storage problems must not stop the agent from working; a session
            # that cannot be persisted is still a usable session.
            logger.warning("session persistence unavailable", exc_info=True)
            repo = None
            grant_store = None
            resolved_session_id = resolved_session_id or "ses_ephemeral"
    if not resolved_session_id:
        resolved_session_id = "ses_ephemeral"

    # -- tools and the gate ------------------------------------------------
    sandbox_mode = normalize_sandbox_mode(settings.permissions.sandbox)
    registry = default_registry(include_memory=settings.memory.enabled)

    policy = PermissionPolicy(
        event_bus=bus,
        sandbox_mode=sandbox_mode,
        protected_paths=[
            ProtectedPath(pattern=p, reason="protected by default (I-010)")
            for p in DEFAULT_PROTECTED_PATHS
        ],
        _grant_store=grant_store,
    )
    approval = build_approval_prompt(
        bus,
        force_non_interactive=(not is_interactive()) if interactive is None else (not interactive),
    )

    hooks = None
    try:
        from kalash.hooks.load import build_hook_runner

        hooks = build_hook_runner(base)
    except Exception:
        logger.debug("hook discovery failed", exc_info=True)

    try:
        from kalash.mcp.load import wire_mcp_tools_sync

        wire_mcp_tools_sync(registry, cwd=base)
    except Exception:
        logger.debug("MCP tool wiring skipped", exc_info=True)

    resolved_provider_id = getattr(resolution, "provider_id", None) or ""

    host = ToolHost(
        registry=registry,
        event_bus=bus,
        session_id=resolved_session_id,
        cwd=base,
        run_id=resolved_session_id,
        sandbox_mode=sandbox_mode,
        mode="plan" if mode.strip().lower() == "plan" else "build",
        policy=policy,
        approval=approval,
        hooks=hooks,
        model_id=resolved_model,
        provider_id=resolved_provider_id,
    )

    # -- context -----------------------------------------------------------
    budget = BudgetState(
        max_tokens=settings.budget.max_tokens if hasattr(settings, "budget") else 500_000,
    )
    window = getattr(resolution.provider, "context_window", None) or DEFAULT_CONTEXT_WINDOW
    assembler = ContextAssembler(
        budget=budget,
        context_window=int(window),
        event_bus=bus,
    )

    instructions = discover_project_instructions(base)
    skills = _discover_skills(base)

    # Pick the prompt tier the model can afford. A throughput-limited endpoint
    # that cannot fit the full prompt plus tools plus a reply gets the compact
    # tier rather than a rejected request.
    system_prompt = _fit_prompt(
        resolved_model, host.mode, instructions, provider_id=resolved_provider_id
    )
    compact_prompt = build_system_prompt(
        mode=host.mode, project_instructions=instructions, compact=True
    )

    # Measured, not guessed: the tool-profile decision depends on how much of
    # the input budget the prompt has already claimed.
    from kalash.core.budget import estimate_tokens

    host.reserved_prompt_tokens = estimate_tokens(system_prompt, mode="prose") + sum(
        estimate_tokens(text, mode="prose") for text in instructions.chain
    )

    loop = AgentLoop(
        gateway=ModelGateway(primary=resolution.provider),
        tool_registry=host,
        session_repo=repo,
        event_bus=bus,
        budget=budget,
        assembler=assembler,
        session_id=resolved_session_id,
        system_prompt=system_prompt,
        compact_system_prompt=compact_prompt,
        model_id=resolved_model,
        provider_id=resolved_provider_id,
        max_iterations=max_iterations,
        hooks=hooks,
    )
    host.cancel_event = loop.cancel_event

    # -- resume ------------------------------------------------------------
    if resume and repo is not None:
        history = _load_history(repo, resolved_session_id)

    agent = Agent(
        loop=loop,
        host=host,
        session_id=resolved_session_id,
        cwd=base,
        instructions=instructions.chain,
        skills=skills,
        mode=host.mode,
        history=history,
    )
    return agent, ""


def _fit_prompt(
    model_id: str,
    mode: str,
    instructions: Any,
    *,
    provider_id: str | None = None,
) -> str:
    """Choose the largest prompt tier that leaves room for tools and a reply.

    Without this a 3.9k-token prompt plus a 2.9k schema block plus a 2k reply
    reservation exceeds an 8k-per-minute allowance, and every request fails —
    the same class of failure as the original hardcoded output cap, one layer up.
    """
    from kalash.core.budget import estimate_tokens
    from kalash.models.limits import TARGET_OUTPUT_TOKENS, input_budget
    from kalash.tools.schema import MINIMAL_TOOLS

    full = build_system_prompt(mode=mode, project_instructions=instructions)
    budget = input_budget(model_id, provider_id=provider_id)

    # Room the smallest usable tool set needs, so a prompt cannot crowd out the
    # ability to act.
    floor_for_tools = 110 * len(MINIMAL_TOOLS)
    affordable = budget - TARGET_OUTPUT_TOKENS - floor_for_tools

    if estimate_tokens(full, mode="prose") <= affordable:
        return full

    compact = build_system_prompt(
        mode=mode, project_instructions=instructions, compact=True
    )
    logger.info(
        "using the compact prompt tier for %s (budget %d tokens)", model_id, budget
    )
    return compact


def _discover_skills(cwd: Path) -> tuple[dict[str, str], ...]:
    """Build the skills catalog: names and descriptions only.

    Bodies are never loaded here. The agent pulls one in with the ``skill`` tool
    when it decides a skill is relevant, which is what keeps a large skill
    library nearly free.
    """
    try:
        from kalash.skills.loader import SkillLoader

        loader = SkillLoader(cwd)
        entries = loader.discover_now()
    except Exception:
        logger.debug("skill discovery failed", exc_info=True)
        return ()

    return tuple(
        {"name": entry.metadata.name, "description": entry.metadata.description}
        for entry in sorted(entries.values(), key=lambda e: e.metadata.name)
    )


async def run_isolated(
    prompt: str,
    *,
    cwd: Path | None = None,
    mode: str = "build",
    model_id: str | None = None,
    max_turns: int = 25,
    max_tokens: int = 100_000,
    spawn_depth: int = 0,
    max_spawn_depth: int = 3,
    context_files: tuple[str, ...] = (),
    agent_instructions: str | None = None,
) -> dict[str, Any]:
    """Run a child agent in an isolated context and return only its result.

    This is what makes delegation worthwhile: the child reads whatever it needs
    in its own context window and the parent pays only for the summary. The
    parent never sees the child's transcript (I-028), which is the whole point —
    a subagent that leaked its transcript back would cost more than doing the
    work inline.

    Returns a dict with ``output``, ``usage``, ``turns``, and ``status``.
    """
    base = (cwd or Path.cwd()).resolve()

    # A child never persists a session: its transcript is deliberately
    # discarded, so writing one would only create orphan rows.
    agent, reason = build_agent(
        cwd=base,
        model_id=model_id,
        mode=mode,
        interactive=False,
        persist=False,
        max_iterations=max_turns,
    )
    if agent is None:
        return {"output": "", "status": "failed", "error": reason, "usage": None}

    from kalash.runtime.bootstrap import prepare_agent

    await prepare_agent(agent)

    if agent_instructions:
        agent.loop.system_prompt = (
            f"{agent.loop.system_prompt.rstrip()}\n\n{agent_instructions.strip()}"
        )

    agent.loop.budget.max_tokens = max_tokens
    agent.loop.budget.max_turns = max_turns
    agent.host.spawn_depth = spawn_depth + 1
    agent.host.max_spawn_depth = max_spawn_depth

    brief = prompt
    if context_files:
        listed = "\n".join(f"- {path}" for path in context_files)
        brief = f"{prompt}\n\nRelevant paths:\n{listed}"

    result = await agent.send(brief)

    from kalash.core.budget import Usage

    usage = Usage(
        input_tokens=agent.loop.budget.tokens_used,
        output_tokens=0,
        source="estimated",
    )
    status = "completed" if result.error is None else "failed"
    return {
        "output": result.final_response,
        "status": status,
        "error": result.error,
        "usage": usage,
        "turns": result.iterations,
        "termination": result.termination_reason.value,
    }


def _load_history(repo: SessionRepository, session_id: str) -> list[Message]:
    """Rebuild conversation history for a resumed session."""
    import asyncio

    async def fetch() -> list[dict[str, Any]]:
        return await repo.get_session_messages(session_id)

    try:
        rows = asyncio.run(fetch()) if not _in_loop() else []
    except Exception:
        logger.warning("could not load session history", exc_info=True)
        return []
    return rehydrate_messages(rows)


async def load_history_async(repo: SessionRepository, session_id: str) -> list[Message]:
    """Async variant, for callers already inside an event loop."""
    try:
        rows = await repo.get_session_messages(session_id)
    except Exception:
        logger.warning("could not load session history", exc_info=True)
        return []
    return rehydrate_messages(rows)


def _in_loop() -> bool:
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True
