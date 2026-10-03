"""The agent turn loop.

Main loop: assemble → stream → parse tool calls → execute → append → repeat.
Terminates on: no tool calls, budget exhausted, user interrupt, or Stop hook.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from kalash.core.budget import BudgetState, Usage
from kalash.core.events import Event, EventBus, EventType
from kalash.models.diagnose import diagnose, explain
from kalash.models.gateway import ModelGateway
from kalash.models.normalize import (
    ContentBlock,
    Message,
    Role,
    StopReason,
    StreamError,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from kalash.storage.repositories.sessions import SessionRepository
from kalash.tools.schema import schema_tokens

from .compaction import COMPACTION_THRESHOLD, Compactor
from .context import ContextAssembler
from .execution import ToolExecutor
from .history import estimate_request_tokens as _estimate_request_tokens
from .history import extractive_summary, split_history, trim_oversized_results
from .request import request, usage_from_result
from .serialize import split_system as _split_system
from .stream import StreamResult
from .toolhost import ToolCategory, ToolHostProtocol
from .transcript import Transcript

logger = logging.getLogger(__name__)

__all__ = [
    "AgentLoop",
    "LoopResult",
    "TerminationReason",
    "ToolCategory",
    "ToolHostProtocol",
]


class TerminationReason(StrEnum):
    """Why the loop stopped."""

    NO_TOOL_CALLS = "no_tool_calls"
    BUDGET_EXHAUSTED = "budget_exhausted"
    USER_INTERRUPT = "user_interrupt"
    STOP_HOOK = "stop_hook"
    MAX_ITERATIONS = "max_iterations"
    ERROR = "error"


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
    model_id: str = ""
    provider_id: str = ""
    """Provider identity for diagnostics."""

    max_iterations: int = 50
    read_concurrency: int = 5
    max_output_tokens: int = 8192
    temperature: float | None = None

    # Internal state
    _running: bool = field(init=False, default=False)
    _cancelled: bool = field(init=False, default=False)
    _iteration: int = field(init=False, default=0)
    _conversation: list[Message] = field(init=False, default_factory=list)
    _executor: ToolExecutor = field(init=False)
    _transcript: Transcript = field(init=False)
    _retried_output_cap: bool = field(init=False, default=False)
    _compacted_summary: str | None = field(init=False, default=None)
    _response_parts: list[str] = field(init=False, default_factory=list)
    _cancel_event: asyncio.Event = field(init=False)
    _task_message: Message | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self._cancel_event = asyncio.Event()
        self._executor = ToolExecutor(
            self.tool_registry,
            self.budget,
            self.event_bus,
            self.session_id,
            self._cancel_event,
            self.read_concurrency,
            settle=self._settle_tool_result,
        )
        self._transcript = Transcript(self.session_repo, self.session_id)

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

    async def run(self, **kwargs: Any) -> LoopResult:
        """Bound the entire turn, including provider waits, hooks and tools."""
        start = time.monotonic()
        remaining = self.budget.max_wallclock_s - self.budget.wallclock_used_s
        if remaining <= 0:
            return await self._finish(TerminationReason.BUDGET_EXHAUSTED, error="wallclock")
        try:
            async with asyncio.timeout(remaining):
                return await self._run(**kwargs)
        except TimeoutError:
            return await self._finish(TerminationReason.BUDGET_EXHAUSTED, error="wallclock")
        finally:
            await self._close_pending_tools()
            self.budget.wallclock_used_s += time.monotonic() - start

    async def _close_pending_tools(self) -> None:
        pending: dict[str, ToolUseBlock] = {}
        for message in self._conversation:
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    pending[block.id] = block
                elif isinstance(block, ToolResultBlock):
                    pending.pop(block.tool_use_id, None)
        if pending:
            message = Message(
                role=Role.USER,
                content=[
                    ToolResultBlock(
                        tool_use_id=call.id,
                        content="Run interrupted. Outcome unknown; inspect workspace before retrying.",
                        is_error=True,
                    )
                    for call in pending.values()
                ],
            )
            self._conversation.append(message)
            await self._persist_tool_results(message)

    async def _run(
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
        self._cancel_event.clear()
        self._iteration = 0
        self._response_parts = []
        self._retried_output_cap = False
        self._task_message = user_message
        if compacted_summary is not None:
            self._compacted_summary = compacted_summary

        # Seed conversation with the user message
        self._conversation = list(recent_turns or [])
        self._conversation.append(user_message)
        restore_tools = getattr(self.tool_registry, "restore_tools", None)
        if restore_tools is not None:
            restore_tools(self._conversation)

        if self.hooks:
            from kalash.hooks.events import HookEvent, SessionStartPayload

            try:
                project_dir = ""
                if hasattr(self.tool_registry, "cwd"):
                    project_dir = str(self.tool_registry.cwd)
                await self.hooks.dispatch(
                    SessionStartPayload(
                        event=HookEvent.SESSION_START,
                        session_id=self.session_id,
                        project_dir=project_dir,
                    )
                )
            except Exception as e:
                logger.error("Failed to dispatch SESSION_START hook", exc_info=e)

        try:
            while self._running and self._iteration < self.max_iterations:
                self._iteration += 1
                if self._cancelled:
                    return await self._finish(TerminationReason.USER_INTERRUPT)

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
                    reason = (
                        TerminationReason.BUDGET_EXHAUSTED
                        if stream_result
                        and stream_result.error
                        and stream_result.error.code == "budget_exhausted"
                        else TerminationReason.ERROR
                    )
                    return await self._finish(reason, error=err)

                usage = usage_from_result(stream_result)

                if stream_result.text_content:
                    self._response_parts.append(stream_result.text_content)

                assistant_msg = Message(
                    role=Role.ASSISTANT,
                    content=stream_result.content,
                )
                self._conversation.append(assistant_msg)
                await self._persist_turn(assistant_msg, usage)

                if not stream_result.has_tool_calls:
                    if stream_result.stop_reason == StopReason.MAX_TOKENS:
                        return await self._finish(
                            TerminationReason.ERROR,
                            error="Model output was truncated before completion",
                        )
                    if self.hooks:
                        from kalash.core.errors import HookDeniedError
                        from kalash.hooks.events import HookEvent, StopPayload

                        try:
                            await self.hooks.dispatch(
                                StopPayload(
                                    event=HookEvent.STOP,
                                    session_id=self.session_id,
                                    reason="model_completed",
                                )
                            )
                        except HookDeniedError as exc:
                            feedback = Message(
                                Role.USER,
                                [TextBlock(text=f"Completion blocked by Stop hook: {exc}")],
                            )
                            self._conversation.append(feedback)
                            await self._persist_turn(feedback, Usage())
                            if stream_result.text_content:
                                self._response_parts.pop()
                            continue
                    await self.event_bus.emit(
                        Event(
                            type=EventType.TURN_COMPLETE,
                            session_id=self.session_id,
                            data={"iteration": self._iteration},
                        )
                    )
                    return await self._finish(
                        TerminationReason.NO_TOOL_CALLS,
                        final=stream_result.text_content,
                    )

                await self._tools_until_cancelled(stream_result.tool_calls)

                if self._cancelled:
                    return await self._finish(TerminationReason.USER_INTERRUPT)

            return await self._finish(TerminationReason.MAX_ITERATIONS)

        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
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
                await self.hooks.dispatch(
                    SessionEndPayload(
                        event=HookEvent.SESSION_END,
                        session_id=self.session_id,
                        reason=reason.value,
                        duration_ms=0,
                    )
                )
            except Exception as e:
                logger.error("Failed to dispatch SESSION_END hook", exc_info=e)

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
        await self.event_bus.emit(
            Event(
                type=event_type,
                session_id=self.session_id,
                data={
                    "prompt_tokens": prompt_tokens,
                    "context_window": window,
                    "fill_ratio": round(ratio, 3),
                },
            )
        )

    async def _stream_turn(
        self,
        *,
        system_identity: str,
        skills_catalog: list[dict[str, str]] | None,
        kalash_md_chain: list[str] | None,
        memory_blocks: list[str] | None,
        environment: dict[str, str] | None,
        on_text_delta: Any | None,
    ) -> StreamResult:
        """Assemble the full request; compact only for the actual context window."""
        tools = self.tool_registry.schemas()
        capabilities = getattr(self.gateway.primary, "capabilities", None)
        if capabilities is not None and not capabilities.tool_use:
            tools = []
        output_cap = min(
            self.max_output_tokens,
            getattr(capabilities, "max_output_tokens", self.max_output_tokens),
        )

        async def assemble() -> tuple[str, list[Message], int]:
            loader = getattr(self.tool_registry, "instruction_loader", None)
            nested = loader.nested if loader is not None else []
            loaded_skills = getattr(self.tool_registry, "loaded_skills", {})
            retained_skills = [
                body
                for name, body in sorted(loaded_skills.items())
                if not any(
                    isinstance(block, ToolResultBlock) and block.content == body
                    for message in self._conversation
                    for block in message.content
                )
            ]
            assembled = await self.assembler.assemble(
                system_identity=system_identity or self.system_prompt,
                tool_schemas=tools,
                skills_catalog=skills_catalog or [],
                kalash_md_chain=kalash_md_chain or [],
                memory_blocks=memory_blocks or [],
                nested_instructions=nested,
                active_skills=retained_skills,
                compacted_summary=self._compacted_summary,
                environment=environment,
                recent_turns=self._conversation[:-1],
                current_message=self._conversation[-1],
            )
            system, messages = _split_system(assembled)
            return system, messages, _estimate_request_tokens(system, messages, tools)

        system, messages, prompt_tokens = await assemble()
        if prompt_tokens + output_cap > self.assembler.context_window * COMPACTION_THRESHOLD:
            await self._ensure_context_fits(system, tools, prompt_tokens=prompt_tokens + output_cap)
            system, messages, prompt_tokens = await assemble()
        output_cap = min(output_cap, self.assembler.context_window - prompt_tokens - 256)
        if output_cap < 1:
            return StreamResult(
                message_id="",
                model=self.model_id,
                content=[],
                stop_reason=StopReason.END_TURN,
                error=StreamError(
                    error="Instructions and active exchange exceed the model context window. Narrow the request or use a larger context model.",
                    code="context_exceeded",
                    recoverable=False,
                ),
            )
        await self._emit_context_usage(prompt_tokens)
        await self.event_bus.emit(
            Event(
                type=EventType.TURN_START,
                session_id=self.session_id,
                data={
                    "iteration": self._iteration,
                    "react_step": self._iteration,
                    "tool_schema_count": len(tools),
                    "tool_schema_tokens_estimate": schema_tokens(tools),
                    "prompt_tokens": prompt_tokens,
                    "context_window": self.assembler.context_window,
                    "fill_ratio": round(prompt_tokens / max(self.assembler.context_window, 1), 3),
                },
            )
        )
        result = await self._stream_response(
            messages,
            system=system,
            tools=tools,
            max_output_tokens=output_cap,
            on_text_delta=on_text_delta,
        )
        # A stated output capability is safe to correct once. Quota errors never
        # rewrite the prompt, remove tools or initiate a synthetic final answer.
        if result.error and not result.content and not self._retried_output_cap:
            finding = diagnose(result.error.error, self.model_id)
            if finding.kind == "max_output" and finding.adjust_max_output:
                self._retried_output_cap = True
                self.max_output_tokens = min(output_cap, finding.adjust_max_output)
                result = await self._stream_response(
                    messages,
                    system=system,
                    tools=tools,
                    max_output_tokens=self.max_output_tokens,
                    on_text_delta=on_text_delta,
                )
        return result

    async def _ensure_context_fits(
        self,
        system_identity: str,
        tool_schemas: list[dict[str, Any]],
        *,
        prompt_tokens: int | None = None,
    ) -> str | None:
        """Compact or trim history when the prompt exceeds the context window."""
        total = (
            prompt_tokens
            if prompt_tokens is not None
            else _estimate_request_tokens(system_identity, self._conversation, tool_schemas)
        )
        ratio = total / max(self.assembler.context_window, 1)

        if ratio < COMPACTION_THRESHOLD:
            return self._compacted_summary

        if len(self._conversation) <= 3:
            if ratio >= COMPACTION_THRESHOLD:
                self._conversation = trim_oversized_results(
                    self._conversation, session_id=self.session_id
                )
                logger.info("trimmed oversized tool results (fill was %.0f%%)", ratio * 100)
            return self._compacted_summary

        # Keep the latest user message and the most recent assistant/tool turns.
        head, tail = split_history(self._conversation)
        if not head:
            self._conversation = trim_oversized_results(
                self._conversation, session_id=self.session_id
            )
            return self._compacted_summary

        compactor = Compactor(
            self.session_repo,
            self.event_bus,
            self.gateway,
            self.budget,
            self._cancel_event,
            self.assembler.context_window,
        )
        self._compacted_summary = await compactor.compact(
            self.session_id, head, self._compacted_summary or ""
        )
        # User constraints are kept verbatim, outside the model-written summary.
        anchors = [
            message
            for message in head
            if message.role == Role.USER
            and any(isinstance(block, TextBlock) for block in message.content)
            and not any(isinstance(block, ToolResultBlock) for block in message.content)
        ]
        self._conversation = [*anchors, *tail]
        logger.info(
            "compacted %d messages (fill was %.0f%%)",
            len(head),
            ratio * 100,
        )
        return self._compacted_summary

    _extractive_summary = staticmethod(extractive_summary)
    _trim_oversized_results = staticmethod(trim_oversized_results)

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

    async def _stream_response(
        self,
        messages: list[Message],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        max_output_tokens: int | None = None,
        on_text_delta: Any | None = None,
    ) -> StreamResult:
        return await request(
            self.gateway,
            self.budget,
            self._cancel_event,
            messages,
            system=system,
            tools=tools,
            max_output_tokens=max_output_tokens or self.max_output_tokens,
            temperature=self.temperature,
            on_text_delta=on_text_delta,
        )

    async def _tools_until_cancelled(self, calls: list[ToolUseBlock]) -> list[ContentBlock]:
        executor = asyncio.create_task(self._execute_tools(calls))
        cancelled = asyncio.create_task(self._cancel_event.wait())
        try:
            done, _ = await asyncio.wait({executor, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done and self._cancel_event.is_set():
                raise asyncio.CancelledError
            return await executor
        finally:
            executor.cancel()
            cancelled.cancel()
            await asyncio.gather(executor, cancelled, return_exceptions=True)

    async def _execute_tools(self, calls: list[ToolUseBlock]) -> list[ContentBlock]:
        return await self._executor.execute(calls)

    async def _persist_turn(self, message: Message, usage: Usage) -> None:
        await self._transcript.append(message, model=self.gateway.primary.name, usage=usage)

    async def persist_user_message(self, message: Message) -> None:
        await self._transcript.append(message)

    async def _settle_tool_result(self, result: ToolResultBlock) -> None:
        """Make the receipt durable before permitting another dependent effect."""
        message = Message(role=Role.USER, content=[result])
        await self._persist_tool_results(message)
        # Keep one protocol turn even when concurrent reads settle out of order.
        if (
            self._conversation
            and self._conversation[-1].role == Role.USER
            and all(isinstance(b, ToolResultBlock) for b in self._conversation[-1].content)
        ):
            self._conversation[-1].content.append(result)
        else:
            self._conversation.append(message)

    async def _persist_tool_results(self, message: Message) -> None:
        await self._transcript.append(message)
