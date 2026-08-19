"""The agent turn loop.

Main loop: assemble → stream → parse tool calls → execute → append → repeat.
Terminates on: no tool calls, budget exhausted, user interrupt, or Stop hook.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from kalash.core.budget import BudgetState, Usage
from kalash.core.events import Event, EventBus, EventType
from kalash.core.ids import generate_id
from kalash.models.gateway import ModelGateway
from kalash.models.normalize import (
    ContentBlock,
    Message,
    Role,
    StopReason,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from kalash.storage.repositories.sessions import SessionRepository

from kalash.models.diagnose import diagnose, explain
from kalash.models.limits import (
    MIN_USABLE_OUTPUT,
    ConstraintCache,
    IterationBudget,
    binding_constraint,
    fit_request_size,
    iteration_budget,
    resolve_output_tokens,
    supports_tools,
    tpm_allowance,
)
from kalash.runtime.shrink import ShrinkTier, next_tier, tier_flags

from .context import ContextAssembler
from .compaction import COMPACTION_THRESHOLD, Compactor
from .serialize import serialize_blocks, split_system as _split_system
from .stream import StreamHandler, StreamResult
from .toolhost import ToolCategory, ToolHostProtocol

logger = logging.getLogger(__name__)

__all__ = [
    "AgentLoop",
    "LoopResult",
    "TerminationReason",
    "ToolCategory",
    "ToolHostProtocol",
]


# ---------------------------------------------------------------------------
# Termination reasons
# ---------------------------------------------------------------------------


class TerminationReason(StrEnum):
    """Why the loop stopped."""

    NO_TOOL_CALLS = "no_tool_calls"
    BUDGET_EXHAUSTED = "budget_exhausted"
    USER_INTERRUPT = "user_interrupt"
    STOP_HOOK = "stop_hook"
    MAX_ITERATIONS = "max_iterations"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Loop result
# ---------------------------------------------------------------------------


@dataclass
class LoopResult:
    """Outcome of a complete agent loop run."""

    termination_reason: TerminationReason
    iterations: int = 0
    total_tokens: int = 0
    final_response: str = ""
    error: str | None = None
    synthesized: bool = False
    """True when the final reply came from a dedicated synthesis pass."""


# ---------------------------------------------------------------------------
# Tool registry protocol
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


@dataclass
class AgentLoop:
    """The core agent turn loop.

    Orchestrates: context assembly → model streaming → tool dispatch → repeat.

    ``tool_registry`` is a :class:`~kalash.runtime.toolhost.ToolHostProtocol` —
    in practice a :class:`~kalash.runtime.toolhost.ToolHost`, which applies the
    permission gate before anything executes.
    """

    gateway: ModelGateway
    tool_registry: ToolHostProtocol
    session_repo: SessionRepository | None
    event_bus: EventBus
    budget: BudgetState
    assembler: ContextAssembler
    hooks: Any | None = None

    # Configuration
    session_id: str = ""
    system_prompt: str = ""
    compact_system_prompt: str = ""
    """Compact prompt tier, used when a TPM-bound model rejects the full prompt."""
    model_id: str = ""
    provider_id: str = ""
    """Kalash provider slug for throughput defaults when the model is unknown."""
    """Used to resolve per-model output caps, throughput ceilings, and whether
    the model accepts a tools array at all."""

    max_iterations: int = 50
    read_concurrency: int = 5
    max_output_tokens: int = 8192

    # Internal state
    _running: bool = field(init=False, default=False)
    _cancelled: bool = field(init=False, default=False)
    _iteration: int = field(init=False, default=0)
    _conversation: list[Message] = field(init=False, default_factory=list)
    _read_semaphore: asyncio.Semaphore = field(init=False, default=None)  # type: ignore[assignment]
    _write_lock: asyncio.Lock = field(init=False, default=None)  # type: ignore[assignment]
    _turn_seq: int = field(init=False, default=-1)
    _retried_output_cap: bool = field(init=False, default=False)
    _shrink_tier: ShrinkTier = field(init=False, default=ShrinkTier.NORMAL)
    _compacted_summary: str | None = field(init=False, default=None)
    _response_parts: list[str] = field(init=False, default_factory=list)
    _cancel_event: asyncio.Event = field(init=False)
    _iteration_budget: IterationBudget | None = field(init=False, default=None)
    _task_message: Message | None = field(init=False, default=None)
    _run_memory_blocks: list[str] = field(init=False, default_factory=list)
    _had_tool_work: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        self._read_semaphore = asyncio.Semaphore(self.read_concurrency)
        self._write_lock = asyncio.Lock()
        self._cancel_event = asyncio.Event()

    @property
    def cancel_event(self) -> asyncio.Event:
        """Signalled when :meth:`cancel` is called."""
        return self._cancel_event

    @staticmethod
    def _estimate_request_tokens_static(
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
    ) -> int:
        return _estimate_request_tokens(system, messages, tools)

    async def _next_turn_seq(self) -> int:
        """Allocate a turn sequence unique within the session.

        Seeded once from storage so a resumed session continues after the turns
        already recorded rather than colliding with them.
        """
        if self._turn_seq < 0 and self.session_repo is not None:
            self._turn_seq = await self.session_repo.next_turn_seq(self.session_id)
        else:
            self._turn_seq = max(self._turn_seq, 0) + 1
        return self._turn_seq

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        *,
        user_message: Message,
        system_identity: str,
        skills_catalog: list[dict[str, str]] | None = None,
        kalash_md_chain: list[str] | None = None,
        memory_blocks: list[str] | None = None,
        compacted_summary: str | None = None,
        recent_turns: list[Message] | None = None,
        environment: dict[str, str] | None = None,
        on_text_delta: Any | None = None,
    ) -> LoopResult:
        """Run the agent loop until termination.

        Args:
            user_message: The current user message to process.
            system_identity: The system prompt/identity text.
            skills_catalog: Available skills (name + description).
            kalash_md_chain: KALASH.md hierarchy contents.
            memory_blocks: Retrieved memory for this turn.
            compacted_summary: Compacted history summary.
            recent_turns: Recent conversation turns.
            on_text_delta: Callback for streaming text output.

        Returns:
            LoopResult describing how/why the loop terminated.
        """
        self._running = True
        self._cancelled = False
        self._iteration = 0
        self._response_parts = []
        self._retried_output_cap = False
        self._shrink_tier = ShrinkTier.NORMAL
        self._iteration_budget = iteration_budget(
            self.model_id, provider_id=self.provider_id or None
        )
        self._task_message = user_message
        self._run_memory_blocks = list(memory_blocks or [])
        self._had_tool_work = False
        if compacted_summary:
            self._compacted_summary = compacted_summary

        # Seed conversation with the user message
        self._conversation = list(recent_turns or [])
        self._conversation.append(user_message)

        if self.hooks:
            from kalash.hooks.events import HookEvent, SessionStartPayload
            try:
                project_dir = ""
                if hasattr(self.tool_registry, "cwd"):
                    project_dir = str(self.tool_registry.cwd)
                await self.hooks.dispatch(SessionStartPayload(
                    event=HookEvent.SESSION_START,
                    session_id=self.session_id,
                    project_dir=project_dir,
                ))
            except Exception:
                pass

        try:
            while self._running and self._iteration < self.max_iterations:
                self._iteration += 1

                # Check budget before each iteration
                ceiling = self.budget.check_ceiling()
                if ceiling is not None:
                    return await self._finish(
                        TerminationReason.BUDGET_EXHAUSTED,
                        error=f"budget exhausted: {ceiling.value}",
                    )

                stream_result = await self._stream_turn(
                    system_identity=system_identity,
                    skills_catalog=skills_catalog,
                    kalash_md_chain=kalash_md_chain,
                    memory_blocks=memory_blocks,
                    environment=environment,
                    on_text_delta=on_text_delta,
                )

                if stream_result is None or stream_result.error is not None:
                    err = (
                        explain(stream_result.error.error, self.model_id)
                        if stream_result and stream_result.error
                        else "The model request failed without a response."
                    )
                    return await self._finish(TerminationReason.ERROR, error=err)

                self.budget.turns_used += 1

                usage = Usage(
                    input_tokens=stream_result.input_tokens,
                    output_tokens=stream_result.output_tokens,
                    cache_read_tokens=stream_result.cache_read_tokens,
                    cache_write_tokens=stream_result.cache_write_tokens,
                )
                self.budget.record_usage(usage)

                if stream_result.text_content:
                    self._response_parts.append(stream_result.text_content)

                assistant_msg = Message(
                    role=Role.ASSISTANT,
                    content=stream_result.content,
                )
                self._conversation.append(assistant_msg)
                await self._persist_turn(assistant_msg, usage)

                if not stream_result.has_tool_calls:
                    await self.event_bus.emit(Event(
                        type=EventType.TURN_COMPLETE,
                        session_id=self.session_id,
                        data={"iteration": self._iteration},
                    ))
                    return await self._finish(
                        TerminationReason.NO_TOOL_CALLS,
                        final=stream_result.text_content,
                    )

                tool_results = await self._execute_tools(stream_result.tool_calls)
                self._had_tool_work = True

                tool_result_msg = Message(
                    role=Role.USER,
                    content=tool_results,
                )
                self._conversation.append(tool_result_msg)
                await self._persist_tool_results(tool_result_msg)

                if self._should_rollup():
                    self._rollup_for_next_iteration()

                if self._cancelled:
                    return await self._finish(TerminationReason.USER_INTERRUPT)

            if (
                self._iteration_budget.phased
                and self._had_tool_work
            ):
                final = await self._synthesize_final_answer(
                    on_text_delta=on_text_delta,
                )
                if final:
                    return await self._finish(
                        TerminationReason.MAX_ITERATIONS,
                        final=final,
                        synthesized=True,
                    )

            return await self._finish(TerminationReason.MAX_ITERATIONS)

        except asyncio.CancelledError:
            return await self._finish(TerminationReason.USER_INTERRUPT)
        except Exception as exc:
            logger.exception("Agent loop error")
            return await self._finish(TerminationReason.ERROR, error=str(exc))
        finally:
            self._running = False

    async def _finish(
        self,
        reason: TerminationReason,
        *,
        final: str = "",
        error: str | None = None,
        synthesized: bool = False,
    ) -> LoopResult:
        if self.hooks:
            from kalash.hooks.events import HookEvent, SessionEndPayload
            try:
                await self.hooks.dispatch(SessionEndPayload(
                    event=HookEvent.SESSION_END,
                    session_id=self.session_id,
                    reason=reason.value,
                    duration_ms=0,
                ))
            except Exception:
                pass
        
        combined = "\n\n".join(part for part in self._response_parts if part)
        if final and final not in combined:
            combined = f"{combined}\n\n{final}".strip() if combined else final
        return LoopResult(
            termination_reason=reason,
            iterations=self._iteration,
            total_tokens=self.budget.tokens_used,
            final_response=combined or final,
            error=error,
            synthesized=synthesized,
        )

    async def _emit_context_usage(self, prompt_tokens: int) -> None:
        window = max(self.assembler.context_window, 1)
        ratio = prompt_tokens / window
        event_type = EventType.BUDGET_WARNING
        if ratio >= COMPACTION_THRESHOLD:
            event_type = EventType.BUDGET_SOFT_LIMIT
        if ratio >= 0.95:
            event_type = EventType.BUDGET_EXCEEDED
        await self.event_bus.emit(Event(
            type=event_type,
            session_id=self.session_id,
            data={
                "prompt_tokens": prompt_tokens,
                "context_window": window,
                "fill_ratio": round(ratio, 3),
            },
        ))

    async def _stream_turn(
        self,
        *,
        system_identity: str,
        skills_catalog: list[dict[str, str]] | None,
        kalash_md_chain: list[str] | None,
        memory_blocks: list[str] | None,
        environment: dict[str, str] | None,
        on_text_delta: Any | None,
    ) -> StreamResult | None:
        """Assemble, preflight against provider limits, stream, and retry on failure."""
        provider = self.provider_id or None
        shrink_tier = self._shrink_tier
        tool_schemas: list[dict[str, Any]] = []
        stream_result: StreamResult | None = None

        for stream_attempt in range(6):
            (
                system_text,
                messages,
                tool_schemas,
                output_tokens,
                prompt_tokens,
            ) = await self._assemble_for_tier(
                shrink_tier,
                system_identity=system_identity,
                skills_catalog=skills_catalog,
                kalash_md_chain=kalash_md_chain,
                memory_blocks=memory_blocks,
                environment=environment,
            )

            allowance = tpm_allowance(self.model_id, provider_id=provider)
            if allowance is not None and prompt_tokens + output_tokens > allowance:
                advanced = next_tier(shrink_tier)
                if advanced is not None:
                    shrink_tier = advanced
                    self._shrink_tier = shrink_tier
                    logger.info(
                        "preflight over TPM allowance (%d > %d), shrink tier %s",
                        prompt_tokens + output_tokens,
                        allowance,
                        shrink_tier.name,
                    )
                    continue

            await self._emit_context_usage(prompt_tokens)

            binding = binding_constraint(
                self.model_id, prompt_tokens, provider_id=provider
            )
            await self.event_bus.emit(Event(
                type=EventType.TURN_START,
                session_id=self.session_id,
                data={
                    "iteration": self._iteration,
                    "react_step": self._iteration,
                    "prompt_tokens": prompt_tokens,
                    "context_window": self.assembler.context_window,
                    "fill_ratio": round(
                        prompt_tokens / max(self.assembler.context_window, 1), 3
                    ),
                    "binding_constraint": binding,
                    "shrink_tier": shrink_tier.name,
                    "tpm_allowance": allowance,
                    "iteration_output_cap": (
                        self._iteration_budget.max_output_tokens
                        if self._iteration_budget
                        else None
                    ),
                    "phased": (
                        self._iteration_budget.phased
                        if self._iteration_budget
                        else False
                    ),
                },
            ))

            stream_result = await self._stream_response(
                messages,
                system=system_text,
                tools=tool_schemas,
                max_output_tokens=output_tokens,
                on_text_delta=on_text_delta,
            )

            if stream_result.error is None:
                self._shrink_tier = shrink_tier
                return stream_result

            finding = diagnose(stream_result.error.error, self.model_id)
            self._record_observed_limits(finding)

            if finding.adjust_max_output and not self._retried_output_cap:
                self._retried_output_cap = True
                self.max_output_tokens = finding.adjust_max_output
                if finding.adjust_max_output:
                    ConstraintCache.record_max_output(
                        self.model_id, finding.adjust_max_output
                    )
                logger.info(
                    "retrying with max_output=%s (%s)",
                    finding.adjust_max_output,
                    finding.summary,
                )
                continue

            if finding.reduce_input:
                advanced = next_tier(shrink_tier)
                if advanced is not None and stream_attempt < 5:
                    shrink_tier = advanced
                    self._shrink_tier = shrink_tier
                    logger.info(
                        "retrying after throughput error at tier %s (%s)",
                        shrink_tier.name,
                        finding.summary,
                    )
                    continue

            if finding.kind == "invalid_tool" and stream_attempt < 5:
                names = ", ".join(sorted(t["name"] for t in tool_schemas)) or "(none)"
                self._conversation.append(Message(
                    role=Role.USER,
                    content=[TextBlock(
                        text=f"{finding.remedy} Available tools: {names}."
                    )],
                ))
                logger.info("retrying after invalid tool call (%s)", finding.summary)
                continue

            return stream_result

        return stream_result

    def _record_observed_limits(self, finding: Any) -> None:
        from kalash.models.diagnose import Diagnosis

        if not isinstance(finding, Diagnosis):
            return
        if finding.tpm_limit is not None:
            ConstraintCache.record_tpm(self.model_id, finding.tpm_limit)
        if finding.adjust_max_output is not None:
            ConstraintCache.record_max_output(
                self.model_id, finding.adjust_max_output
            )

    async def _assemble_for_tier(
        self,
        tier: ShrinkTier,
        *,
        system_identity: str,
        skills_catalog: list[dict[str, str]] | None,
        kalash_md_chain: list[str] | None,
        memory_blocks: list[str] | None,
        environment: dict[str, str] | None,
    ) -> tuple[str, list[Message], list[dict[str, Any]], int, int]:
        """Build a request at ``tier``, escalating until it fits TPM."""
        provider = self.provider_id or None
        flags = tier_flags(tier)
        identity = system_identity or self.system_prompt
        if flags.compact_prompt and self.compact_system_prompt:
            identity = self.compact_system_prompt

        active_memory = [] if flags.drop_memory else list(memory_blocks or [])
        active_kalash = [] if flags.drop_project_context else list(kalash_md_chain or [])
        active_skills = None if flags.drop_project_context else skills_catalog

        prompt_tokens = 0
        tool_schemas: list[dict[str, Any]] = []
        system_text = ""
        messages: list[Message] = []
        output_tokens = MIN_USABLE_OUTPUT
        working_tier = tier

        for _ in range(int(ShrinkTier.DROP_PROJECT_CONTEXT) - int(tier) + 2):
            flags = tier_flags(working_tier)
            if flags.compact_prompt and self.compact_system_prompt:
                identity = self.compact_system_prompt
            active_memory = [] if flags.drop_memory else list(memory_blocks or [])
            active_kalash = (
                [] if flags.drop_project_context else list(kalash_md_chain or [])
            )
            active_skills = None if flags.drop_project_context else skills_catalog

            tool_schemas = self.tool_registry.schemas(
                prompt_tokens=prompt_tokens or None,
                force_profile=flags.force_tool_profile,
            )
            if not supports_tools(self.model_id):
                tool_schemas = []

            compacted = await self._ensure_context_fits(identity, tool_schemas)
            assembled = await self.assembler.assemble(
                system_identity=identity,
                tool_schemas=[],
                skills_catalog=active_skills or [],
                environment=environment,
                kalash_md_chain=active_kalash,
                memory_blocks=active_memory,
                compacted_summary=compacted,
                recent_turns=self._conversation[:-1],
                current_message=self._conversation[-1],
                estimated_prompt_tokens=prompt_tokens or 0,
            )
            system_text, messages = _split_system(assembled)
            prompt_tokens = _estimate_request_tokens(
                system_text, messages, tool_schemas
            )
            per_iter_out = (
                self._iteration_budget.max_output_tokens
                if self._iteration_budget
                else self.max_output_tokens
            )
            output_tokens, needs_shrink = fit_request_size(
                self.model_id,
                prompt_tokens,
                min(self.max_output_tokens, per_iter_out),
                provider_id=provider,
            )
            if not needs_shrink:
                break
            nxt = next_tier(working_tier)
            if nxt is None:
                break
            working_tier = nxt

        self._shrink_tier = working_tier
        return system_text, messages, tool_schemas, output_tokens, prompt_tokens

    def _conversation_token_estimate(self) -> int:
        return _estimate_request_tokens("", self._conversation, [])

    def _should_rollup(self) -> bool:
        budget = self._iteration_budget
        if budget is None or not budget.phased:
            return False
        return self._conversation_token_estimate() > budget.max_carry_tokens

    def _rollup_for_next_iteration(self) -> None:
        """Fold prior turns into the compaction summary; keep the active exchange."""
        budget = self._iteration_budget
        if budget is None or len(self._conversation) <= 2:
            return

        prior = self._conversation[:-2]
        if prior:
            rolled = self._extractive_summary(prior)
            if self._compacted_summary:
                self._compacted_summary = f"{self._compacted_summary}\n\n{rolled}"
            else:
                self._compacted_summary = rolled

        anchor = self._task_message or self._conversation[0]
        tail = self._conversation[-2:]
        self._conversation = [anchor, *tail]
        self._shrink_tier = ShrinkTier.NORMAL
        logger.info(
            "rolled up context for iteration %d (carry limit %d tokens)",
            self._iteration,
            budget.max_carry_tokens if budget else 0,
        )

    @staticmethod
    def _message_text(message: Message) -> str:
        parts: list[str] = []
        for block in message.content:
            text = getattr(block, "text", None) or getattr(block, "content", None)
            if text:
                parts.append(str(text))
        return "\n".join(parts)

    def _build_work_context(self) -> str:
        sections: list[str] = []
        if self._compacted_summary:
            sections.append(f"### Prior steps\n{self._compacted_summary}")
        if self._run_memory_blocks:
            sections.append(
                "### Working memory\n" + "\n\n".join(self._run_memory_blocks)
            )
        if self._response_parts:
            sections.append(
                "### Assistant notes\n"
                + "\n\n".join(self._response_parts[-5:])
            )
        return "\n\n".join(sections) or "(no durable state recorded)"

    async def _synthesize_final_answer(
        self,
        *,
        on_text_delta: Any | None,
    ) -> str:
        """One no-tools pass that turns multi-iteration work into a final reply."""
        if self._task_message is None:
            return ""

        goal = self._message_text(self._task_message)
        work = self._build_work_context()
        budget = self._iteration_budget
        output_tokens = (
            budget.max_output_tokens if budget else MIN_USABLE_OUTPUT
        )

        synthesis_prompt = (
            "You completed work across several tool-using steps. Write one "
            "complete, user-facing final answer.\n\n"
            f"## Original request\n{goal}\n\n"
            f"## Work completed\n{work}\n\n"
            "## Instructions\n"
            "- Synthesize everything into a clear final answer\n"
            "- Do not mention tools, iterations, token limits, or internal process\n"
            "- If work is incomplete, say what was done and what remains"
        )

        await self.event_bus.emit(Event(
            type=EventType.TURN_START,
            session_id=self.session_id,
            data={
                "iteration": self._iteration,
                "phase": "synthesis",
                "max_output_tokens": output_tokens,
            },
        ))

        stream_result = await self._stream_response(
            [Message(role=Role.USER, content=[TextBlock(text=synthesis_prompt)])],
            system=(
                "You produce polished final answers for the user based on "
                "work already completed. Be direct and complete."
            ),
            tools=[],
            max_output_tokens=output_tokens,
            on_text_delta=on_text_delta,
        )

        if stream_result.error is not None:
            logger.warning(
                "synthesis pass failed: %s",
                explain(stream_result.error.error, self.model_id),
            )
            return ""

        usage = Usage(
            input_tokens=stream_result.input_tokens,
            output_tokens=stream_result.output_tokens,
            cache_read_tokens=stream_result.cache_read_tokens,
            cache_write_tokens=stream_result.cache_write_tokens,
        )
        self.budget.record_usage(usage)

        if stream_result.text_content:
            self._response_parts.append(stream_result.text_content)

        await self.event_bus.emit(Event(
            type=EventType.TURN_COMPLETE,
            session_id=self.session_id,
            data={"iteration": self._iteration, "phase": "synthesis"},
        ))
        return stream_result.text_content or ""

    async def _ensure_context_fits(
        self,
        system_identity: str,
        tool_schemas: list[dict[str, Any]],
    ) -> str | None:
        """Compact or trim history when the prompt exceeds the context window."""
        ratio = _estimate_request_tokens(
            system_identity, self._conversation, tool_schemas
        ) / max(self.assembler.context_window, 1)

        if ratio < COMPACTION_THRESHOLD:
            return self._compacted_summary

        if len(self._conversation) <= 3:
            if ratio >= COMPACTION_THRESHOLD:
                self._compacted_summary = self._extractive_summary(self._conversation)
                self._conversation = self._trim_oversized_results(self._conversation)
                logger.info(
                    "trimmed oversized tool results (fill was %.0f%%)", ratio * 100
                )
            return self._compacted_summary

        # Keep the latest user message and the most recent assistant/tool turns.
        head = self._conversation[:-3]
        tail = self._conversation[-3:]

        if self.session_repo is not None:
            compactor = Compactor(
                gateway=self.gateway,
                session_repo=self.session_repo,
                event_bus=self.event_bus,
                budget=self.budget,
            )
            summary = await compactor.compact(self.session_id, head)
            self._compacted_summary = summary
        else:
            self._compacted_summary = self._extractive_summary(head)

        self._conversation = tail
        logger.info(
            "compacted %d messages (fill was %.0f%%)",
            len(head),
            ratio * 100,
        )
        return self._compacted_summary

    @staticmethod
    def _extractive_summary(messages: list[Message]) -> str:
        """Fallback compaction without a summarizer model."""
        lines: list[str] = []
        for msg in messages[-12:]:
            text = "".join(
                getattr(block, "text", "") or getattr(block, "content", "")
                for block in msg.content
                if hasattr(block, "text") or hasattr(block, "content")
            )
            if text:
                snippet = text[:400].replace("\n", " ")
                lines.append(f"[{msg.role}] {snippet}")
        omitted = max(0, len(messages) - 12)
        prefix = f"[{omitted} earlier messages omitted]\n" if omitted else ""
        return prefix + "\n".join(lines)

    @staticmethod
    def _trim_oversized_results(
        messages: list[Message], *, max_chars: int = 12_000
    ) -> list[Message]:
        """Truncate huge tool-result blocks when history is too short to split."""
        trimmed: list[Message] = []
        for msg in messages:
            new_blocks: list[ContentBlock] = []
            for block in msg.content:
                if isinstance(block, ToolResultBlock) and len(block.content) > max_chars:
                    new_blocks.append(
                        ToolResultBlock(
                            tool_use_id=block.tool_use_id,
                            content=block.content[:max_chars]
                            + f"\n… [{len(block.content) - max_chars} chars truncated]",
                            is_error=block.is_error,
                        )
                    )
                else:
                    new_blocks.append(block)
            trimmed.append(Message(role=msg.role, content=new_blocks))
        return trimmed

    def cancel(self) -> None:
        """Signal the loop to stop after the current iteration."""
        self._cancelled = True
        self._cancel_event.set()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def conversation(self) -> list[Message]:
        """The full conversation after a run, including tool calls and results.

        The caller needs this to carry history into the next turn; without it
        each turn would start blank and the agent could not see its own prior
        work — which is exactly how the TUI behaved.
        """
        return list(self._conversation)

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def _stream_response(
        self,
        messages: list[Message],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        max_output_tokens: int | None = None,
        on_text_delta: Any | None = None,
    ) -> StreamResult:
        """Stream a response from the model gateway.

        ``ModelGateway.stream`` is an async generator, so it is iterated
        directly. Awaiting it — as this previously did — raises
        ``TypeError: object async_generator can't be used in 'await' expression``
        on the very first turn.
        """
        handler = StreamHandler(on_text_delta=on_text_delta)

        async for event in self.gateway.stream(
            messages,
            system=system or None,
            tools=tools or None,
            max_tokens=max_output_tokens or self.max_output_tokens,
        ):
            handler.feed(event)
            if handler.is_complete:
                break

        return handler.result()

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tools(
        self, tool_calls: list[ToolUseBlock]
    ) -> list[ContentBlock]:
        """Execute tool calls with appropriate concurrency.

        READ tools run concurrently (semaphore-limited).
        WRITE/EXEC tools run serialized.
        """
        results: list[ContentBlock] = []

        # Partition by category
        reads: list[ToolUseBlock] = []
        serialized: list[ToolUseBlock] = []

        for call in tool_calls:
            cat = self.tool_registry.category(call.name)
            if cat == ToolCategory.READ:
                reads.append(call)
            else:
                serialized.append(call)

        # Execute reads concurrently
        if reads:
            read_tasks = [
                self._execute_single_tool_read(call) for call in reads
            ]
            read_results = await asyncio.gather(*read_tasks, return_exceptions=True)
            for call, result in zip(reads, read_results):
                if isinstance(result, Exception):
                    results.append(ToolResultBlock(
                        tool_use_id=call.id,
                        content=f"Error: {result}",
                        is_error=True,
                    ))
                else:
                    assert isinstance(result, ToolResultBlock)
                    results.append(result)

        # Execute writes/execs serialized
        for call in serialized:
            if self._cancelled:
                results.append(ToolResultBlock(
                    tool_use_id=call.id,
                    content="Cancelled by user.",
                    is_error=True,
                ))
                continue
            result = await self._execute_single_tool_write(call)
            results.append(result)

        # Every tool_use block must have a matching tool_result or the provider
        # rejects the next request. Breaking out of the loop on cancellation left
        # later calls unanswered, which made an interrupted session unusable.
        answered = {
            block.tool_use_id
            for block in results
            if isinstance(block, ToolResultBlock)
        }
        for call in tool_calls:
            if call.id not in answered:
                results.append(ToolResultBlock(
                    tool_use_id=call.id,
                    content="Not executed: the run was interrupted.",
                    is_error=True,
                ))

        return results

    async def _execute_single_tool_read(self, call: ToolUseBlock) -> ToolResultBlock:
        """Execute a READ-category tool with semaphore."""
        async with self._read_semaphore:
            return await self._execute_tool_with_hooks(call)

    async def _execute_single_tool_write(self, call: ToolUseBlock) -> ToolResultBlock:
        """Execute a WRITE/EXEC-category tool with serialization lock."""
        async with self._write_lock:
            return await self._execute_tool_with_hooks(call)

    async def _execute_tool_with_hooks(self, call: ToolUseBlock) -> ToolResultBlock:
        """Execute a tool call with PreToolUse/PostToolUse hooks."""
        # Emit PreToolUse
        pre_event = Event(
            type=EventType.TOOL_START,
            session_id=self.session_id,
            data={
                "tool_name": call.name,
                "tool_use_id": call.id,
                "arguments": call.input,
            },
        )
        await self.event_bus.emit(pre_event)

        start_time = time.monotonic()
        try:
            result_str = await self.tool_registry.execute(
                call.name, call.input, tool_use_id=call.id
            )

            self.budget.tool_calls_used += 1

            return ToolResultBlock(
                tool_use_id=call.id,
                content=result_str,
                is_error=False,
            )
        except Exception as exc:
            logger.warning("Tool %s failed: %s", call.name, exc)
            duration_ms = int((time.monotonic() - start_time) * 1000)

            await self.event_bus.emit(Event(
                type=EventType.TOOL_COMPLETE,
                session_id=self.session_id,
                data={
                    "tool_name": call.name,
                    "tool_use_id": call.id,
                    "duration_ms": duration_ms,
                    "ok": False,
                    "error": str(exc),
                },
            ))

            return ToolResultBlock(
                tool_use_id=call.id,
                content=f"Error executing {call.name}: {exc}",
                is_error=True,
            )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _persist_turn(self, message: Message, usage: Usage) -> None:
        """Persist an assistant turn and its content to storage.

        Previously this stored a turn row with no messages, so a session could
        report how many turns it had but could never be replayed. It also called
        ``gateway.primary.name()`` — ``name`` is a property, so every persist
        raised and was swallowed by the except.
        """
        if self.session_repo is None:
            return
        try:
            turn_id = await self.session_repo.create_turn(
                session_id=self.session_id,
                seq=await self._next_turn_seq(),
                role="assistant",
                model=self.gateway.primary.name,
            )
            await self.session_repo.add_message(
                turn_id=turn_id,
                seq=0,
                role="assistant",
                content=serialize_blocks(message.content),
                content_type="blocks",
                metadata={"model": self.gateway.primary.name},
            )
            await self.session_repo.complete_turn(
                turn_id=turn_id,
                state="COMPLETED",
                token_count=usage.total_tokens,
            )
        except Exception:
            logger.warning("Failed to persist turn", exc_info=True)

    async def persist_user_message(self, message: Message) -> None:
        """Record a user (or tool-result) message so the session can be resumed."""
        if self.session_repo is None:
            return
        try:
            turn_id = await self.session_repo.create_turn(
                session_id=self.session_id,
                seq=await self._next_turn_seq(),
                role="user",
            )
            await self.session_repo.add_message(
                turn_id=turn_id,
                seq=0,
                role="user",
                content=serialize_blocks(message.content),
                content_type="blocks",
            )
            await self.session_repo.complete_turn(turn_id=turn_id, state="COMPLETED")
        except Exception:
            logger.warning("Failed to persist user message", exc_info=True)

    async def _persist_tool_results(self, message: Message) -> None:
        """Persist tool-result blocks from a ReAct observe step."""
        await self.persist_user_message(message)


def _estimate_request_tokens(
    system: str,
    messages: list[Message],
    tools: list[dict[str, Any]],
) -> int:
    """Estimate what a request will cost the provider to accept.

    Deliberately includes the tool block: providers that bill
    ``prompt + max_tokens`` against a per-minute ceiling count the schemas, and
    omitting them here is what let a 5k schema block plus an 8k reservation
    exceed an 8k allowance.
    """
    import json

    from kalash.core.budget import estimate_tokens

    total = estimate_tokens(system, mode="prose") if system else 0

    if tools:
        total += estimate_tokens(json.dumps(tools, separators=(",", ":")))

    for message in messages:
        for block in message.content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                total += estimate_tokens(text)
                continue
            content = getattr(block, "content", None)
            if isinstance(content, str):
                total += estimate_tokens(content)
                continue
            payload = getattr(block, "input", None)
            if isinstance(payload, dict):
                total += estimate_tokens(json.dumps(payload, separators=(",", ":")))
            else:
                total += 50

    # Provider-side framing we do not model: wire envelopes, role markers.
    return int(total * 1.05) + 32
