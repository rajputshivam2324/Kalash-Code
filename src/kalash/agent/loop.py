"""The agent loop: one turn = steps of (request the model, run the tools it asked for).

Rules this module enforces:

* The session log is the only history. A response is committed only when complete.
* Every tool call gets exactly one result, appended in the model's call order.
* A call is executed only if its arguments parsed completely and permission allowed it.
* A turn ends with an honest reason; ``completed`` means the model stopped, nothing more.
* Cancellation closes open calls in the log before it propagates.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from kalash.agent import context as ctxmod
from kalash.agent import effort as effort_mod
from kalash.agent.context_blocks import ContextBlocks, digest, frame
from kalash.agent.events import (
    AgentEvent,
    Compacted,
    ContextUpdate,
    ModeChanged,
    Notice,
    ReasoningChunk,
    Retry,
    TextChunk,
    ToolDone,
    ToolOutput,
    ToolPending,
    ToolRunning,
    TurnEnd,
    TurnResult,
    TurnStart,
)
from kalash.agent.guards import RepeatFailureGuard, TodoWatch
from kalash.checkpoints import CheckpointStore, Snapshot
from kalash.config import Pricing, Settings
from kalash.hooks import HookRunner
from kalash.llm.catalog import ModelInfo, Price
from kalash.llm.errors import ErrorCode, LLMError
from kalash.llm.http import Adapter
from kalash.llm.retry import RetryPolicy, stream_with_retry
from kalash.llm.types import (
    AttemptUsage,
    Done,
    Message,
    ReasoningDelta,
    Request,
    Retrying,
    StreamEvent,
    TextBlock,
    TextDelta,
    ToolCallBlock,
    ToolCallStarted,
    ToolSpec,
    Usage,
    tool_result,
    user_text,
)
from kalash.modes import MODES, initial_mode
from kalash.permissions import PermissionEngine
from kalash.session.log import SessionLog
from kalash.session.memory import SessionMemory, task_snapshot
from kalash.tools.base import (
    Params,
    Registry,
    Tool,
    ToolContext,
    ToolResult,
    compact_tool_spec,
    validation_message,
)
from kalash.tools.diffing import diff_files
from kalash.tools.output import limit_output

log = logging.getLogger("kalash.agent")

MAX_PARALLEL_READS = 8
MAX_STOP_CONTINUATIONS = 3
MAX_CONSECUTIVE_TRUNCATIONS = 3
TOOL_TIMEOUT_S = 600.0
EMPTY_REPLY_NUDGE = (
    "[Kalash: your last reply was empty. In a sentence or two tell the user what you did and what "
    "you verified, or make the next tool call if the task is not finished.]"
)
FILE_TOOLS = ("read", "write", "edit")  # a successful one can reveal nested instruction files

TRUNCATED_CALL = (
    "Your response was cut off by the output limit before this tool call was complete, so it "
    "was NOT run. Retry with a smaller step (for example write a large file in several parts)."
)


@dataclass
class _Prepared:
    call: ToolCallBlock
    tool: Tool | None = None
    params: Params | None = None
    refusal: str | None = None  # model-visible text when the call will not run
    denied: bool = False
    arguments: dict[str, Any] | None = None
    summary: str = ""


class Agent:
    def __init__(
        self,
        *,
        adapter: Adapter,
        session: SessionLog,
        registry: Registry,
        settings: Settings,
        tool_ctx: ToolContext,
        permissions: PermissionEngine,
        system_prompt: str,
        emit: Callable[[AgentEvent], None] | None = None,
        hooks: HookRunner | None = None,
        retry: RetryPolicy | None = None,
        default_max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        pricing: Pricing | None = None,
        info: ModelInfo | None = None,
        context: ContextBlocks | None = None,
        memory: SessionMemory | None = None,
    ) -> None:
        self.adapter = adapter
        self.context = context
        self.session = session
        self.memory = memory or SessionMemory(session)
        self.registry = registry
        self.settings = settings
        self.tool_ctx = tool_ctx
        self.permissions = permissions
        self.system_prompt = system_prompt
        self._emit = emit or (lambda _event: None)
        self.hooks = hooks
        self.retry = retry or RetryPolicy()
        self.default_max_tokens = default_max_tokens
        self.info = info
        # The user's choice: None (the model default), "auto", "off" or a level. A choice made
        # earlier in this session wins over settings, so resuming (or switching model) keeps it.
        chosen, saved = session.effort()
        wanted = saved if chosen else (reasoning_effort or settings.reasoning_effort)
        self.effort_setting, self.effort_notice = effort_mod.normalize(info, wanted)
        if self.effort_notice and self.effort_setting is None:
            self.effort_notice = f"reasoning effort {wanted!r}: {self.effort_notice}"
        self._effort_now: str | None = None
        self._turn_steps = 0
        self._tools_digest = next(
            (
                str(e.data.get("digest", ""))
                for e in reversed(session.events)
                if e.type == "tool_schema"
            ),
            "",
        )
        self.price_override: Price | None = (
            Price(
                pricing.input_per_mtok,
                pricing.output_per_mtok,
                pricing.cache_read_per_mtok,
                pricing.cache_write_per_mtok,
            )
            if pricing is not None
            else None
        )
        self.window = settings.context_window or adapter.context_window
        self._guard = RepeatFailureGuard()
        self._todo_watch = TodoWatch()
        self._midturn_notes: list[str] = []  # appended to the next tool result
        self.checkpoints = CheckpointStore(session.id)
        self.mode = initial_mode(permissions.mode, permissions.approval)
        self._pending_notes: list[str] = []
        self.plan_decider: Callable[[str], Awaitable[tuple[str, str]]] | None = None
        tool_ctx.extras["todo_sink"] = self._record_todos
        tool_ctx.extras["set_mode"] = self.set_mode
        tool_ctx.extras["plan_active"] = lambda: self.mode == "plan"
        tool_ctx.extras["plan_decision"] = self._decide_plan
        self.repairs = session.repair()
        self._sync_session_state()

    # -- public ------------------------------------------------------------------------

    def emit(self, event: AgentEvent) -> None:
        try:
            self._emit(event)
        except Exception:
            log.exception("event listener failed")

    @property
    def model_label(self) -> str:
        return f"{self.adapter.provider}/{self.adapter.model}"

    @property
    def price_known(self) -> bool:
        return self.price_override is not None or (
            self.info is not None and self.info.price is not None
        )

    def cost_of(self, usage: Usage) -> float | None:
        """USD cost of one response, or None when tokens or the price are unknown."""
        if usage.input_tokens is None or usage.output_tokens is None:
            return None
        price = self.price_override or (
            self.info.price_for(usage.input_tokens) if self.info else None
        )
        if price is None:
            return None
        read, write = usage.cache_read_tokens, usage.cache_write_tokens
        uncached = max(0, usage.input_tokens - read - write)
        read_rate = price.cache_read if price.cache_read is not None else price.input
        write_rate = price.cache_write if price.cache_write is not None else price.input
        return (
            uncached * price.input
            + read * read_rate
            + write * write_rate
            + usage.output_tokens * price.output
        ) / 1_000_000

    # -- reasoning effort ----------------------------------------------------------------

    def set_effort(self, requested: str | None) -> tuple[str | None, str | None]:
        """Choose the reasoning effort now. Returns (stored setting, message); a message with no
        setting is a refusal, a message with a setting says what was substituted."""
        setting, problem = effort_mod.normalize(self.info, requested)
        if setting is None and problem is not None:
            return None, problem
        self.effort_setting = setting
        self.session.append("effort", value=setting)
        return setting, problem

    @property
    def effort_display(self) -> str:
        return effort_mod.label(self.info, self.effort_setting, self._effort_now)

    def _request_effort(self, messages: list[Message]) -> str | None:
        """The level for the next request (``auto`` decides per step)."""
        setting = self.effort_setting
        if setting == effort_mod.AUTO:
            tail: list[Message] = []
            for message in reversed(messages):
                if message.role != "tool":
                    break
                tail.append(message)
            level = effort_mod.pick_for_step(
                self.info,
                first_step=self._turn_steps == 0,
                last_failed=any(m.is_error for m in tail),
                nudged=any("[Kalash:" in m.text for m in tail),
            )
        elif setting is None:
            level = self.info.default_effort if self.info else None
        else:
            level = setting
        self._effort_now = level
        return level

    # -- modes, undo, user shell ---------------------------------------------------------

    def set_mode(self, mode_id: str, *, announce: bool = True) -> None:
        """Switch permission mode now. ``announce`` queues a runtime note for the model that is
        delivered with the next prompt (never mid-turn, where it would split a tool exchange)."""
        mode = MODES[mode_id]
        self.permissions.mode = mode.sandbox
        self.permissions.approval = mode.approval
        self.tool_ctx.sandbox_mode = mode.sandbox
        changed = mode_id != self.mode
        previous = MODES[self.mode]
        self.mode = mode_id
        if changed:
            self.session.append("mode", mode=mode_id, sandbox=mode.sandbox, approval=mode.approval)
            if announce:  # the new policy itself is in the next runtime-context block
                self._pending_notes.append(
                    f"The user changed the permission mode from {previous.label.upper()} to "
                    f"{mode.label.upper()}."
                )
        self.emit(ModeChanged(mode_id, mode.label))

    async def _decide_plan(self, plan: str) -> tuple[str, str]:
        if self.plan_decider is None:
            return "keep", "no interactive session is available to approve plans"
        return await self.plan_decider(plan)

    def undo_last_turn(self) -> str:
        """Revert the file changes of the last turn that has some. Returns a user message."""
        report = self.checkpoints.undo_last_turn(self.session)
        if not report.restored and not report.removed and not report.skipped:
            return "Nothing to undo: no tracked file changes remain."
        lines = [f"restored {p}" for p in report.restored] + [
            f"removed {p} (it was created by the agent)" for p in report.removed
        ]
        lines += [f"skipped {p}: {why}" for p, why in report.skipped]
        if report.changed:
            self._pending_notes.append(
                "The user undid your file changes from the previous turn ("
                + "; ".join(lines)
                + "). Re-read any file before editing it again."
            )
            for path in [*report.restored, *report.removed]:
                self.tool_ctx.observations.forget(Path(path))
        return "\n".join(lines)

    async def run_shell(self, command: str) -> ToolResult:
        """Run a command the *user* typed (``!cmd``) through the bash tool and its sandbox, and
        record command and output in the history so the model knows what happened."""
        tool = self.registry.get("bash")
        if tool is None:
            return ToolResult("The bash tool is not available.", is_error=True)
        call_id = f"user_shell_{self.session.turn_count()}_{len(self.session.events)}"
        params = tool.parse({"description": "User command", "command": command})
        self.emit(ToolRunning(call_id, "bash", f"bash {command}", {"command": command}))
        outcome = await tool.run(params, self._ctx_for(call_id))
        outcome.text = limit_output(outcome.text, self.tool_ctx.spill_dir, "bash")
        self.session.append_message(
            user_text(f"[The user ran a shell command]\n$ {command}\n{outcome.text}"),
            source="user-shell",
        )
        self.emit(ToolDone(call_id, "bash", outcome.text, outcome.is_error))
        return outcome

    async def run_turn(self, prompt: str) -> TurnResult:
        result = TurnResult("completed")
        turn = self.session.turn_count() + 1
        if self.hooks is not None:
            outcome = await self.hooks.run("UserPromptSubmit", {"prompt": prompt})
            if outcome.blocked:
                result = TurnResult("refused", error=f"prompt rejected by hook: {outcome.message}")
                self.emit(TurnEnd(result))
                return result
            for warning in outcome.warnings:
                self.emit(Notice(warning, "warning"))
            hook_context = outcome.context.strip()
        else:
            hook_context = ""
        self._sync_session_state()
        self.session.append("turn_start", turn=turn)
        message = user_text(prompt)
        blocks, digests = (
            self.context.pending(self.session.context_digests(), MODES[self.mode])
            if self.context is not None
            else ([], {})
        )
        remembered = self.memory.render_notes(max(512, min(16_000, self.window // 4)))
        memory_digest = digest(remembered) if remembered else None
        if self.session.context_digests().get("working_memory") != memory_digest:
            blocks.append(
                remembered
                or frame("Working memory has been cleared. Earlier notes are no longer current.")
            )
            digests["working_memory"] = memory_digest
        if self._pending_notes:
            notes = "\n".join(f"- {n}" for n in self._pending_notes)
            self._pending_notes.clear()
            blocks.append(frame(f"Notices from the harness:\n{notes}"))
        message.content[:0] = [TextBlock(b + "\n\n", source="context") for b in blocks]
        if hook_context:
            message.content.append(
                TextBlock(f"\n\n[Hook context]\n{hook_context}", source="context")
            )
        self.session.append_message(message)
        if digests:
            self.session.append("context", digests=digests)
        self._guard.clear()
        self._todo_watch.reset()
        self._midturn_notes.clear()
        self.emit(TurnStart(prompt))
        try:
            result = await self._loop(result)
        except asyncio.CancelledError:
            self.session.repair()
            result = TurnResult(
                "cancelled",
                steps=result.steps,
                usage=result.usage,
                cost=result.cost,
                denials=result.denials,
                tool_calls=result.tool_calls,
            )
            self.emit(TurnEnd(result))
            raise
        except LLMError as error:
            result.reason, result.error = "error", str(error)
            result.error_code = error.code.value
        except ctxmod.CompactionError as error:
            result.reason, result.error = "error", f"cannot compact the context: {error}"
        except Exception as error:
            log.exception("turn failed")
            result.reason = "error"
            result.error = f"internal error: {type(error).__name__}: {error}"
        self.session.close_open_calls()
        self.session.append("turn_end", turn=turn, reason=result.reason, error=result.error)
        self.emit(TurnEnd(result))
        return result

    async def compact_now(self) -> tuple[int, int]:
        """Summarise older history on request. Returns (tokens before, tokens after)."""
        result = TurnResult("completed")
        await self._compact("requested by the user", result)
        last = [e for e in self.session.events if e.type == "compaction"][-1]
        used = ctxmod.context_tokens(self.session, self.system_prompt, self.tool_specs())
        return int(last.data.get("replaced", 0)), used

    # -- the loop ----------------------------------------------------------------------

    async def _loop(self, result: TurnResult) -> TurnResult:
        truncations = 0
        stop_continuations = 0
        while True:
            await self._maybe_compact(result)
            self._turn_steps = result.steps
            done = await self._model_step(result)
            result.steps += 1
            message = done.message
            self.session.append_message(
                message,
                usage=done.usage.to_dict(),
                cost=self.cost_of(done.usage),
                finish=done.finish,
            )
            self._after_usage(result, done.usage)
            result.text = message.text
            if done.finish in ("content_filter", "refusal"):
                result.reason = "refused"
                result.error = f"the model stopped with {done.finish}"
                return result
            calls = message.tool_calls
            if done.finish == "max_tokens":
                if not calls:
                    result.reason = "max_tokens"
                    result.error = "the model hit its output limit before finishing"
                    return result
                truncations += 1
                for call in calls:
                    self.session.append_message(tool_result(call, TRUNCATED_CALL, is_error=True))
                    self.emit(ToolDone(call.id, call.name, TRUNCATED_CALL, True))
                if truncations > MAX_CONSECUTIVE_TRUNCATIONS:
                    result.reason = "max_tokens"
                    result.error = "output limit hit repeatedly while writing tool calls"
                    return result
                continue
            truncations = 0
            if calls:
                result.tool_calls += len(calls)
                await self._run_calls(calls, result)
                if self._guard.tripped:
                    result.reason = "stuck"
                    result.error = (
                        f"{self._guard.tripped}; stopped instead of retrying. The conversation "
                        "is intact - tell the model what to try, or change the setup."
                    )
                    result.error_code = "STUCK"
                    return result
                continue
            if self.hooks is not None and stop_continuations < MAX_STOP_CONTINUATIONS:
                outcome = await self.hooks.run("Stop", {"final_message": message.text})
                if outcome.blocked:
                    stop_continuations += 1
                    self.session.append_message(
                        Message(
                            "user", [TextBlock(f"[Stop hook] {outcome.message}", source="context")]
                        ),
                        source="stop-hook",
                    )
                    self.emit(Notice(f"Stop hook asked to continue: {outcome.message}"))
                    continue
            result.reason = "completed"
            return result

    def _after_usage(self, result: TurnResult, usage: Usage) -> None:
        result.usage.input_tokens = (result.usage.input_tokens or 0) + (usage.input_tokens or 0)
        result.usage.output_tokens = (result.usage.output_tokens or 0) + (usage.output_tokens or 0)
        result.usage.cache_read_tokens += usage.cache_read_tokens
        result.usage.cache_write_tokens += usage.cache_write_tokens
        result.usage.reasoning_tokens += usage.reasoning_tokens
        cost = self.cost_of(usage)
        if cost is not None:
            result.cost = (result.cost or 0.0) + cost
        used = ctxmod.context_tokens(self.session, self.system_prompt, self.tool_specs())
        self.emit(ContextUpdate(used, self.window, usage, cost))

    # -- model request -----------------------------------------------------------------

    def tool_specs(self) -> list[ToolSpec]:
        full = self.registry.specs()
        if ctxmod.fixed_tokens(self.system_prompt, full) > self.window * 0.5:
            return [compact_tool_spec(spec) for spec in full]
        return full

    def _build_request(self) -> Request:
        tools = self.tool_specs()
        serialized = [asdict(t) for t in tools]
        spec_digest = hashlib.sha256(json.dumps(serialized, sort_keys=True).encode()).hexdigest()
        if spec_digest != self._tools_digest:
            compact = (
                ctxmod.fixed_tokens(self.system_prompt, self.registry.specs()) > self.window * 0.5
            )
            self.session.append(
                "tool_schema",
                digest=spec_digest,
                profile="compact" if compact else "full",
                tools=serialized,
            )
            self._tools_digest = spec_digest
        used = ctxmod.context_tokens(self.session, self.system_prompt, tools)
        base = self.settings.max_output_tokens or self.default_max_tokens
        max_tokens = None if base is None else max(1, min(base, self.window - used - 256))
        messages = self.session.messages()
        return Request(
            model=self.adapter.model,
            system=self.system_prompt,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            reasoning_effort=self._request_effort(messages),
            temperature=self.settings.temperature,
        )

    async def _model_step(self, result: TurnResult) -> Done:
        compacted_for_overflow = False
        nudged_empty = False
        while True:
            try:
                return await self._stream_once(self._build_request(), result)
            except LLMError as error:
                self.session.append("attempt_failed", error=str(error), code=error.code.value)
                if error.code == ErrorCode.CONTEXT_OVERFLOW and not compacted_for_overflow:
                    compacted_for_overflow = True
                    self.emit(Notice("The provider reported a context overflow; compacting."))
                    await self._compact("provider reported context overflow", result)
                    continue
                if (
                    error.code == ErrorCode.EMPTY_RESPONSE
                    and not nudged_empty
                    and self._after_tool()
                ):
                    # Seen live (Sarvam 105B): after the last tool result the model says
                    # nothing, and asking again unchanged says nothing again. Ask in words.
                    nudged_empty = True
                    self.session.append_message(
                        Message("user", [TextBlock(EMPTY_REPLY_NUDGE, source="context")])
                    )
                    self.emit(Notice("The model sent an empty reply; asking for a summary."))
                    continue
                raise

    def _after_tool(self) -> bool:
        messages = self.session.messages()
        return bool(messages) and messages[-1].role == "tool"

    async def _stream_once(self, request: Request, result: TurnResult) -> Done:
        done: Done | None = None
        async for event in stream_with_retry(self.adapter, request, self.retry):
            if isinstance(event, TextDelta):
                self.emit(TextChunk(event.text))
            elif isinstance(event, ReasoningDelta):
                self.emit(ReasoningChunk(event.text))
            elif isinstance(event, ToolCallStarted):
                self.emit(ToolPending(event.id, event.name))
            elif isinstance(event, Retrying):
                self.emit(Retry(event.attempt, event.delay, event.error))
                if event.discarded:
                    self.emit(Notice("Connection interrupted mid-response; retrying.", "warning"))
            elif isinstance(event, Done):
                done = event
            elif isinstance(event, AttemptUsage):
                self.session.append(
                    "usage",
                    purpose="failed provider attempt",
                    error=event.error,
                    usage=event.usage.to_dict(),
                    cost=self.cost_of(event.usage),
                )
                self._after_usage(result, event.usage)
        if done is None:
            raise LLMError(
                "stream ended without a response",
                ErrorCode.TRANSPORT,
                provider=self.adapter.provider,
            )
        return done

    # -- compaction --------------------------------------------------------------------

    async def _maybe_compact(self, result: TurnResult) -> None:
        used = ctxmod.context_tokens(self.session, self.system_prompt, self.tool_specs())
        if used >= self.window * self.settings.compaction.threshold:
            try:
                await self._compact(f"context at {used}/{self.window} tokens", result)
            except ctxmod.CheckpointOnly:
                # An early pressure trigger is not an overflow. Keep working with the
                # recent exchanges until they can be compacted, if a valid request fits.
                if used + 256 >= self.window:
                    raise

    async def _compact(self, reason: str, result: TurnResult) -> None:
        self.emit(Notice("Compacting context..."))
        blocks = self.context.current(MODES[self.mode]) if self.context is not None else []
        blocks.append(
            task_snapshot(self.session, self.memory, max(512, min(16_000, self.window // 3)))
        )

        def observe(event: StreamEvent) -> None:
            if isinstance(event, AttemptUsage):
                self.session.append(
                    "usage",
                    purpose="failed compaction attempt",
                    error=event.error,
                    usage=event.usage.to_dict(),
                    cost=self.cost_of(event.usage),
                )
                self._after_usage(result, event.usage)
            if isinstance(event, Done):
                self.session.append(
                    "usage",
                    purpose="compaction summary",
                    usage=event.usage.to_dict(),
                    cost=self.cost_of(event.usage),
                    message=event.message.to_dict(),
                    finish=event.finish,
                )
                self._after_usage(result, event.usage)

        outcome = await ctxmod.compact(
            adapter=self.adapter,
            log=self.session,
            system=self.system_prompt,
            tools=self.tool_specs(),
            window=self.window,
            retain_ratio=self.settings.compaction.retain,
            threshold_ratio=self.settings.compaction.threshold,
            retry=self.retry,
            on_event=observe,
            blocks=blocks,
            reasoning_effort=self._request_effort(self.session.messages()),
            recovery_effort="off" if effort_mod.off_supported(self.info) else None,
            output_limit=self.settings.max_output_tokens or self.default_max_tokens,
        )
        self.session.append(
            "compaction",
            summary=outcome.summary,
            keep_from=outcome.keep_from,
            replaced=outcome.replaced,
            reason=reason,
            blocks=blocks,
        )
        self.emit(
            ContextUpdate(
                ctxmod.context_tokens(self.session, self.system_prompt, self.tool_specs()),
                self.window,
                Usage(),
                None,
            )
        )
        self.emit(Compacted(outcome.tokens_before, outcome.tokens_after))

    # -- tools -------------------------------------------------------------------------

    async def _run_calls(self, calls: list[ToolCallBlock], result: TurnResult) -> None:
        prepared = [await self._prepare(call) for call in calls]
        for item in prepared:
            if item.denied and item.refusal:
                result.denials.append(f"{item.call.name}: {item.refusal}")
        index = 0
        while index < len(prepared):
            item = prepared[index]
            batch = [item]
            if self._is_parallel(item):
                while (
                    index + len(batch) < len(prepared)
                    and self._is_parallel(prepared[index + len(batch)])
                    and len(batch) < MAX_PARALLEL_READS
                ):
                    batch.append(prepared[index + len(batch)])
            await self._run_batch(batch)
            index += len(batch)

    @staticmethod
    def _is_parallel(item: _Prepared) -> bool:
        return item.refusal is None and item.tool is not None and item.tool.can_run_concurrently

    async def _prepare(self, call: ToolCallBlock) -> _Prepared:
        item = _Prepared(call)
        tool = self.registry.get(call.name)
        if tool is None:
            names = ", ".join(self.registry.names())
            item.refusal = f"Unknown tool '{call.name}'. Available tools: {names}."
            return item
        try:
            raw = json.loads(call.arguments) if call.arguments.strip() else {}
        except ValueError as error:
            item.refusal = (
                f"The arguments for {call.name} are not valid JSON ({error}). "
                f"Send a single JSON object."
            )
            return item
        if not isinstance(raw, dict):
            item.refusal = f"The arguments for {call.name} must be a JSON object."
            return item
        try:
            params = tool.parse(raw)
        except ValidationError as error:
            item.refusal = validation_message(tool, error)
            return item
        item.tool, item.params, item.arguments = tool, params, raw
        item.summary = tool.permission_key(params, self.tool_ctx)
        decision = self.permissions.decide(tool, item.summary, tool.effects(params, self.tool_ctx))
        decision = await self.permissions.resolve(
            call.id, tool, item.summary, decision, tool.describe(params, self.tool_ctx)
        )
        if decision.verdict == "deny":
            item.refusal = f"Permission denied: {decision.reason}."
            item.denied = True
            return item
        if self.hooks is not None:
            outcome = await self.hooks.run(
                "PreToolUse", {"tool_name": call.name, "tool_input": raw}, match=call.name
            )
            if outcome.blocked:
                item.refusal = f"Blocked by a PreToolUse hook: {outcome.message}"
                item.denied = True
        return item

    async def _run_batch(self, batch: list[_Prepared]) -> None:
        tasks = [asyncio.create_task(self._execute(item)) for item in batch]
        recorded = 0
        try:
            for item, task in zip(batch, tasks, strict=True):
                self._record(item, await task)
                recorded += 1
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for item, task in zip(batch[recorded:], tasks[recorded:], strict=True):
                if task.done() and not task.cancelled() and task.exception() is None:
                    self._record(item, task.result())
            raise

    async def _execute(self, item: _Prepared) -> ToolResult:
        call = item.call
        if item.refusal is not None:
            return ToolResult(item.refusal, is_error=True, metadata={"denied": item.denied})
        assert item.tool is not None and item.params is not None
        self.session.append("tool_start", call_id=call.id, name=call.name)
        self.emit(ToolRunning(call.id, call.name, item.summary, item.arguments or {}))
        snapshots = self._snapshot_writes(item)
        try:
            outcome = await asyncio.wait_for(
                item.tool.run(item.params, self._ctx_for(call.id)), TOOL_TIMEOUT_S
            )
        except TimeoutError:
            return ToolResult(
                f"{call.name} did not finish within {TOOL_TIMEOUT_S:.0f}s.", is_error=True
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.exception("tool %s raised", call.name)
            return ToolResult(
                f"{call.name} failed unexpectedly: {type(error).__name__}: {error}", is_error=True
            )
        outcome.text = limit_output(outcome.text, self.tool_ctx.spill_dir, call.name)
        if snapshots and not outcome.is_error:
            self.checkpoints.record(self.session, call.id, call.name, snapshots)
            self._attach_diff(outcome, snapshots[0])
        if self.hooks is not None:
            hook = await self.hooks.run(
                "PostToolUse",
                {
                    "tool_name": call.name,
                    "tool_input": item.arguments,
                    "tool_response": outcome.text[:4000],
                    "is_error": outcome.is_error,
                },
                match=call.name,
            )
            if hook.blocked:
                outcome.text += f"\n\n[PostToolUse hook] {hook.message}"
        return outcome

    def _snapshot_writes(self, item: _Prepared) -> list[Snapshot]:
        if item.tool is None or item.params is None or item.call.name not in ("write", "edit"):
            return []
        writes = item.tool.effects(item.params, self.tool_ctx).writes
        return [self.checkpoints.capture(path) for path in writes]

    def _attach_diff(self, outcome: ToolResult, snap: Snapshot) -> None:
        try:
            before = None
            if snap.existed and snap.before_sha:
                before = self.checkpoints._blob(snap.before_sha).read_bytes()
            elif snap.existed:
                return
            after = snap.path.read_bytes() if snap.path.is_file() else None
        except OSError:
            return
        diff = diff_files(str(snap.path), before, after)
        if diff is not None:
            outcome.metadata["diff"] = diff.as_dict()

    def _touched_path(self, item: _Prepared) -> Path:
        return self.tool_ctx.resolve(str((item.arguments or {}).get("file_path", "")))

    def _ctx_for(self, call_id: str) -> ToolContext:
        """A per-call view of the shared context whose live-output callback targets this call."""
        return replace(
            self.tool_ctx,
            emit_output=lambda text: self.emit(ToolOutput(call_id, text)),
            call_id=call_id,
        )

    def _record(self, item: _Prepared, outcome: ToolResult) -> None:
        call = item.call
        text = outcome.text
        note = self._guard.observe(call.name, call.arguments, text, outcome.is_error)
        if note:
            text += f"\n\n{note}"
        if self.context is not None and not outcome.is_error and call.name in FILE_TOOLS:
            for notice in self.context.nested.touch(self._touched_path(item)):
                text += f"\n\n{notice}"
        reminder = self._todo_watch.observe(call.name, self.session.todos())
        if reminder:
            self._midturn_notes.append(reminder)
        if self._midturn_notes:
            text += "\n\n" + "\n".join(self._midturn_notes)
            self._midturn_notes.clear()
        denied = bool(outcome.metadata.get("denied"))
        self.session.append_message(tool_result(call, text, is_error=outcome.is_error))
        diff = outcome.metadata.get("diff")
        self.emit(ToolDone(call.id, call.name, text, outcome.is_error, denied, diff))

    # -- session state -----------------------------------------------------------------

    def _record_todos(self, todos: list[dict[str, str]]) -> None:
        self.session.append("todo", todos=todos)

    def _sync_session_state(self) -> None:
        if self.session.system_prompt() != self.system_prompt:
            self.session.append("system", prompt=self.system_prompt)
        previous = self.session.model()
        if previous != self.model_label:
            self.session.append("model", model=self.model_label)
            if previous and self.session.turn_count() > 0:  # dsh: say who wrote earlier turns
                self._pending_notes.append(
                    f"The model changed: earlier assistant turns were generated by {previous}; "
                    f"this session continues with {self.model_label}."
                )
