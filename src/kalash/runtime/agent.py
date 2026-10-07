"""Assemble a session runtime; own setup, conversation continuity and cleanup."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kalash.core.budget import BudgetState
from kalash.core.config import KalashConfig, load_config
from kalash.core.events import EventBus, get_event_bus
from kalash.models.gateway import ModelGateway
from kalash.models.normalize import Message, Role, TextBlock
from kalash.permissions.console import build_approval_prompt, is_interactive
from kalash.permissions.policy import PermissionPolicy, ProtectedPath
from kalash.runtime.context import ContextAssembler
from kalash.runtime.delegation import run_isolated
from kalash.runtime.instructions import InstructionLoader, _render_project_instructions
from kalash.runtime.journal import RuntimeJournal
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

if TYPE_CHECKING:
    from collections.abc import Iterator

    from kalash.runtime.completion import CompletionContract

logger = logging.getLogger(__name__)

# Used only when a provider does not advertise its context window.
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
    config: KalashConfig = field(default_factory=KalashConfig)
    extensions: bool = True
    allowed_tools: frozenset[str] | None = None
    _prepared: bool = field(default=False, init=False)
    _resume_pending: bool = field(default=False, init=False)
    _prepare_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _mcp_manager: Any | None = field(default=None, init=False)
    history: list[Message] = field(default_factory=list)
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _owns_session: bool = field(default=False, init=False, repr=False)

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
        completion_contract: CompletionContract | None = None,
    ) -> LoopResult:
        """Run one agentic turn to completion."""
        async with self._send_lock:
            with self._session_owner():
                from kalash.core.ids import generate_id

                run_id = generate_id("run_")
                self.host.run_id = run_id
                with self.loop.event_bus.scope(self.session_id, run_id):
                    result = await self._send(
                        prompt, on_text_delta=on_text_delta, completion_contract=completion_contract
                    )
                    result.run_id = run_id
                    return result

    @contextmanager
    def _session_owner(self) -> Iterator[None]:
        """An OS-owned lock cannot expire while its writer is still alive."""
        if self.loop.session_repo is None or self._owns_session:
            yield
            return
        import hashlib

        from kalash.core.paths import kalash_home
        from kalash.core.secure_io import private_lock

        folder = kalash_home() / "session-locks"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / hashlib.sha256(self.session_id.encode()).hexdigest()
        try:
            with private_lock(path):
                self._owns_session = True
                try:
                    yield
                finally:
                    self._owns_session = False
        except BlockingIOError:
            raise RuntimeError(
                "This session is being written by another run; wait or create a separate session"
            ) from None

    async def _send(
        self,
        prompt: str,
        *,
        on_text_delta: Any | None,
        completion_contract: CompletionContract | None = None,
    ) -> LoopResult:
        # Refresh even an already-prepared object: another owner may have written
        # this session since its last send. The OS lock is held during refresh.
        if self.loop.session_repo is not None:
            self._resume_pending = True
        await self.prepare()
        before_tokens, before_cost = self.budget.tokens_used, self.budget.cost_used
        if self.loop.journal is not None:
            self.loop.journal.run_id = self.host.run_id
            self.host.run_id = self.loop.journal.run_id
            await self.loop.journal.append(
                "run_started", {"workspace": str(self.cwd)}, budget=self.budget
            )
        user_message = Message(role=Role.USER, content=[TextBlock(text=prompt)])
        await self.loop.persist_user_message(user_message)

        environment = detect_environment(self.cwd)

        memory_blocks: list[str] = []
        try:
            if self.config.memory.enabled:
                from kalash.memory.session import get_session_memory

                existing = "\n".join(self.instructions) + "\n".join(self.scratchpad_blocks())
                recalled = await get_session_memory(
                    self.session_id, self.cwd, self.config
                ).recall_blocks(
                    prompt,
                    limit=12,
                    existing_context=existing,
                    context_window=self.loop.assembler.context_window,
                )
                memory_blocks = recalled + memory_blocks
        except Exception as e:
            logger.error("memory recall failed", exc_info=e)

        result = await self.loop.run(
            user_message=user_message,
            system_identity=self.loop.system_prompt,
            skills_catalog=[dict(s) for s in self.skills],
            kalash_md_chain=list(self.instructions),
            memory_blocks=memory_blocks,
            recent_turns=self.history,
            on_text_delta=on_text_delta,
            environment=environment,
            completion_contract=completion_contract,
        )

        # Carry the accumulated conversation (including tool calls and their
        # results) into the next turn.
        self.history = self.loop.conversation
        result.call_tokens = self.budget.tokens_used - before_tokens
        result.call_cost_usd = self.budget.cost_used - before_cost
        if self.loop.journal is not None:
            await self.loop.journal.append(
                "run_settled",
                {"reason": result.termination_reason.value, "error": result.error},
                budget=self.budget,
                summary=self.loop._compacted_summary,  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
            )

        try:
            if self.config.memory.enabled:
                from kalash.memory.capture import capture_turn_memories

                await capture_turn_memories(
                    session_id=self.session_id,
                    cwd=self.cwd,
                    user_prompt=prompt,
                    conversation=self.loop.conversation,
                    turn_seq=self.loop.budget.turns_used,
                    config=self.config,
                )
        except Exception as e:
            logger.error("turn memory capture failed", exc_info=e)

        return result

    async def prepare(self) -> None:
        """Await optional setup exactly once before exposing its tools."""
        with self._session_owner():
            await self._prepare()

    async def _prepare(self) -> None:
        async with self._prepare_lock:
            if self._prepared and not self._resume_pending:
                return
            if self._resume_pending and self.loop.session_repo is not None:
                if self.loop.journal is not None:
                    self.history = await self.loop.journal.recover_messages()
                    self.loop._compacted_summary = await self.loop.journal.recover(self.budget)  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
                    rows = await self.loop.session_repo.engine.execute_read_async(
                        "SELECT m.content FROM messages m JOIN turns t ON m.turn_id=t.id "
                        "WHERE t.session_id=? AND m.content_type='compaction_summary' "
                        "ORDER BY t.seq DESC, m.seq DESC LIMIT 1",
                        (self.session_id,),
                    )
                    summaries = [row["content"] for row in rows]
                    if summaries:
                        self.loop._compacted_summary = summaries[-1]  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
                        self.loop.journal.summary = summaries[-1]
                    for pending in await self.loop.journal.unsettled():
                        if pending["kind"] == "attempt_started" and not pending.get(
                            "reservation_held"
                        ):
                            self.budget.unknown_usage_attempts += 1
                            self.budget.unknown_tokens_reserved += (
                                pending["input_estimate"] + pending["output_allowance"]
                            )
                            pricing = getattr(self.loop.gateway.primary, "pricing", None)
                            if pending.get("pricing"):
                                from decimal import Decimal

                                from kalash.core.budget import Pricing

                                pricing = Pricing(
                                    input_per_mtok=Decimal(pending["pricing"]["input"]),
                                    output_per_mtok=Decimal(pending["pricing"]["output"]),
                                )
                            if pricing is not None:
                                from decimal import Decimal

                                self.budget.unknown_cost_reserved += (
                                    Decimal(pending["input_estimate"]) * pricing.input_per_mtok
                                    + Decimal(pending["output_allowance"]) * pricing.output_per_mtok
                                ) / 1_000_000
                        notice = (
                            "Interrupted execution has an uncertain outcome. Inspect the "
                            "workspace before retrying effects."
                        )
                        self.history.append(Message(Role.USER, [TextBlock(notice)]))
                        await self.loop.journal.append(
                            "recovery_uncertain", pending, budget=self.budget
                        )
                else:
                    self.history = await load_history_async(self.loop.session_repo, self.session_id)
                self._resume_pending = False
            if self.extensions and not self._prepared:
                from kalash.mcp.load import wire_mcp_tools

                self._mcp_manager = await wire_mcp_tools(self.host.registry, cwd=self.cwd)
                if self.allowed_tools is not None:
                    for tool in self.host.registry.list_tools():
                        if tool.name not in self.allowed_tools:
                            self.host.registry.unregister(tool.name)
            self._prepared = True

    async def close(self) -> None:
        """Release session-owned subprocesses and connections."""
        from kalash.tools.shell import terminate_session_backgrounds

        await terminate_session_backgrounds(self.session_id)
        await self.loop.event_bus.close_presentation()
        if self._mcp_manager is not None:
            await self._mcp_manager.disconnect_all()
            self._mcp_manager = None
        close = getattr(self.loop.gateway, "close", None) or getattr(
            self.loop.gateway.primary, "close", None
        )
        if close is not None:
            await close()

    def cancel(self) -> None:
        self.loop.cancel()

    def clear_history(self) -> None:
        """Drop conversation history without ending the session."""
        self.history = []
        self.loop._compacted_summary = None  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.


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
    provider: Any | None = None,
    extensions: bool = True,
    allowed_tools: frozenset[str] | None = None,
) -> tuple[Agent | None, str]:
    """Assemble an agent.

    Returns ``(agent, "")`` on success or ``(None, reason)`` when a provider
    cannot be resolved, so callers can show the reason without exception
    handling on a routine configuration problem.
    """
    from kalash.models.resolve import Resolution, build_provider

    base = (cwd or Path.cwd()).resolve()
    settings = config or load_config(base)
    if resume and persist and session_id:
        try:
            original = SessionManager(settings).resume(session_id)
            base = Path(original.project_dir).resolve()
            if not base.is_dir():
                return None, "The session workspace no longer exists; restore it before resuming"
            if config is None:
                settings = load_config(base)
        except Exception as error:
            return None, f"Cannot resume session: {error}"
    if settings.permissions.approval not in {"on-request", "untrusted", "never"}:
        return None, "Supported approval modes are on-request, untrusted and never"
    if provider is None and provider_id is None and model_id is None:
        configured = settings.raw.get("model", {})
        explicit = isinstance(configured, dict) and "primary" in configured
        if explicit or settings.model.primary != KalashConfig().model.primary:
            if "/" not in settings.model.primary:
                return None, "model.primary must be provider/model"
            provider_id, model_id = settings.model.primary.split("/", 1)

    resolution = (
        Resolution(provider, provider_id, model_id)
        if provider is not None
        else build_provider(provider_id, model_id)
    )
    if not resolution.ok or resolution.provider is None:
        return None, resolution.reason or "no model provider configured"

    # The id the provider actually resolved to, which is what limits are keyed
    # on. The caller's model_id may be None (use the configured default).
    resolved_model: str = (
        model_id
        or getattr(resolution, "model_id", "")
        or getattr(resolution.provider, "model", "")
        or getattr(resolution.provider, "name", "")
        or ""
    )

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
        except Exception as exc:
            logger.exception("session persistence unavailable")
            return None, f"Cannot persist this session: {exc}"
    if not resolved_session_id:
        from kalash.core.ids import generate_id

        resolved_session_id = generate_id("ses_")

    # -- tools and the gate ------------------------------------------------
    sandbox_mode = normalize_sandbox_mode(settings.permissions.sandbox)
    registry = default_registry(include_memory=settings.memory.enabled)
    if allowed_tools is not None:
        for tool in registry.list_tools():
            if tool.name not in allowed_tools:
                registry.unregister(tool.name)

    policy = PermissionPolicy(
        event_bus=bus,
        sandbox_mode=sandbox_mode,
        approval_mode=settings.permissions.approval,
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

        hooks = build_hook_runner(base) if extensions else None
    except Exception:
        logger.error("hook discovery failed", exc_info=True)
        return None, "Hook discovery failed; repair or trust the configured hooks before starting"

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
        max_spawn_depth=settings.budget.max_spawn_depth,
        network_enabled=settings.permissions.network,
        config=settings,
    )

    # -- context -----------------------------------------------------------
    budget = BudgetState(
        max_tokens=settings.budget.max_tokens,
        max_cost=settings.budget.max_cost_usd,
        max_wallclock_s=settings.budget.max_wallclock_s,
        max_turns=settings.budget.max_turns,
        max_tool_calls=settings.budget.max_tool_calls,
        max_spawn_depth=settings.budget.max_spawn_depth,
    )
    caps = getattr(resolution.provider, "capabilities", None)
    window = getattr(caps, "context_window", None) or getattr(
        resolution.provider, "context_window", DEFAULT_CONTEXT_WINDOW
    )
    assembler = ContextAssembler(
        budget=budget,
        context_window=int(window or DEFAULT_CONTEXT_WINDOW),
        event_bus=bus,
    )

    instructions = discover_project_instructions(base)
    host.instruction_loader = InstructionLoader(base, set(instructions.sources))
    skills = _discover_skills(base) if extensions else ()

    system_prompt = build_system_prompt(mode=host.mode)

    fallbacks = []
    # Only explicitly configured fallbacks authorize another data destination.
    raw_model = settings.raw.get("model", {})
    if provider is None and isinstance(raw_model, dict) and "fallback" in raw_model:
        for selection in settings.model.fallback:
            if "/" not in selection:
                return None, "model.fallback entries must be provider/model"
            fallback_id, fallback_model = selection.split("/", 1)
            fallback = build_provider(fallback_id, fallback_model)
            if not fallback.ok or fallback.provider is None:
                return None, f"Cannot configure fallback {selection}: {fallback.reason}"
            fallbacks.append(fallback.provider)
    journal = RuntimeJournal(repo, resolved_session_id) if repo is not None else None
    host.journal = journal
    loop = AgentLoop(
        gateway=ModelGateway(primary=resolution.provider, fallbacks=fallbacks),
        tool_registry=host,
        session_repo=repo,
        event_bus=bus,
        budget=budget,
        assembler=assembler,
        session_id=resolved_session_id,
        system_prompt=system_prompt,
        model_id=resolved_model,
        provider_id=resolved_provider_id,
        max_iterations=max_iterations,
        hooks=hooks,
        journal=journal,
        temperature=settings.model.temperature,
        max_output_tokens=int(
            settings.model.max_output_tokens
            or getattr(resolution.provider, "default_output_tokens", 8192)
            or 8192
        ),
    )
    host.cancel_event = loop.cancel_event

    # Durable resume is awaited under session ownership in prepare(). Avoid
    # loading the full legacy transcript before the saved projection is selected.

    agent = Agent(
        loop=loop,
        host=host,
        session_id=resolved_session_id,
        cwd=base,
        instructions=(_render_project_instructions(instructions),) if instructions.chain else (),
        skills=skills,
        mode=host.mode,
        history=history,
        config=settings,
        extensions=extensions,
        allowed_tools=allowed_tools,
    )
    agent._resume_pending = resume and repo is not None  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    loop.context_data_provider = agent.scratchpad_blocks

    async def delegate(**kwargs: Any) -> dict[str, Any]:
        child_mode = kwargs.pop("mode", agent.host.mode)
        outcome = await run_isolated(
            cwd=agent.cwd,
            parent_budget=agent.budget,
            parent_host=agent.host,
            config=agent.config,
            provider=agent.loop.gateway.primary,
            spawn_depth=agent.host.spawn_depth,
            max_spawn_depth=agent.host.max_spawn_depth,
            mode=child_mode,
            extensions=agent.extensions,
            **kwargs,
        )
        usage = outcome.get("usage")
        if usage is not None:
            agent.loop.gateway._accumulate_usage(usage)  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        return outcome

    host.delegate = delegate
    return agent, ""


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


def _load_history(repo: SessionRepository, session_id: str) -> list[Message]:
    """Rebuild conversation history for a resumed session."""
    import asyncio

    async def fetch() -> list[dict[str, Any]]:
        return await repo.get_session_messages(session_id)

    try:
        rows = asyncio.run(fetch()) if not _in_loop() else []
    except Exception as e:
        raise RuntimeError(f"Could not resume session {session_id}: {e}") from e
    return rehydrate_messages(rows)


async def load_history_async(repo: SessionRepository, session_id: str) -> list[Message]:
    """Async variant, for callers already inside an event loop."""
    rows = await repo.get_session_messages(session_id)
    return rehydrate_messages(rows)


def _in_loop() -> bool:
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True
