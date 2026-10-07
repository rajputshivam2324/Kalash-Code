"""Headless execution: ``kalash -p "…"`` and piped stdin."""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

from kalash.cli.output import (
    EXIT_APPROVAL_REQUIRED,
    EXIT_BUDGET_EXCEEDED,
    EXIT_CANCELLED,
    EXIT_FAILURE,
    EXIT_PROVIDER_UNAVAILABLE,
    OutputFormat,
    OutputSink,
    RunMetrics,
)
from kalash.runtime.loop import TerminationReason

if TYPE_CHECKING:
    from kalash.runtime.agent import Agent

# Legacy aliases for tests and scripts
EXIT_ERROR = EXIT_FAILURE
EXIT_NO_PROMPT = 2
EXIT_NO_PROVIDER = EXIT_PROVIDER_UNAVAILABLE
EXIT_INTERRUPT = EXIT_CANCELLED


def run_pipe_mode(
    prompt: str | None = None,
    *,
    mode: str = "build",
    resume_session: str | None = None,
    output_format: str = "text",
    model: str | None = None,
    sandbox: str | None = None,
    approval: str | None = None,
) -> None:
    """Entry point for headless mode. Raises SystemExit with the status code."""
    text = prompt or _read_prompt()
    if not text:
        sys.stderr.write("kalash: no prompt supplied\n")
        raise SystemExit(EXIT_NO_PROMPT)

    fmt = OutputFormat(output_format.replace("_", "-"))
    raise SystemExit(
        asyncio.run(
            _run(
                text,
                mode=mode,
                resume_session=resume_session,
                output_format=fmt,
                model=model,
                sandbox=sandbox,
                approval=approval,
            )
        )
    )


def _read_prompt() -> str:
    if sys.stdin.isatty():
        return ""
    return sys.stdin.read().strip()


async def _run(
    prompt: str,
    *,
    mode: str = "build",
    resume_session: str | None = None,
    output_format: OutputFormat = OutputFormat.TEXT,
    model: str | None = None,
    sandbox: str | None = None,
    approval: str | None = None,
) -> int:
    from kalash.core.logging import configure_logging
    from kalash.runtime.agent import build_agent
    from kalash.runtime.bootstrap import prepare_agent

    configure_logging()

    interactive = sys.stdin.isatty() and sys.stderr.isatty()

    provider_id: str | None = None
    model_id: str | None = None
    if model and "/" in model:
        provider_id, model_id = model.split("/", 1)

    config_overrides: dict[str, object] = {}
    if sandbox or approval:
        perms: dict[str, str] = {}
        if sandbox:
            perms["sandbox"] = sandbox
        if approval:
            perms["approval"] = approval
        config_overrides["permissions"] = perms

    from kalash.core.config import load_config

    config = load_config(overrides=config_overrides if config_overrides else None)

    agent, reason = build_agent(
        mode=mode,
        session_id=resume_session,
        resume=bool(resume_session),
        interactive=interactive,
        provider_id=provider_id,
        model_id=model_id,
        config=config,
    )
    if agent is None:
        sys.stderr.write(f"kalash: {reason}\n")
        if output_format is not OutputFormat.TEXT:
            sink = OutputSink(output_format)
            return sink.finish_error(
                code="KALASH_PROVIDER_UNAVAILABLE",
                message=reason,
                error_class="provider",
                exit_code=EXIT_NO_PROVIDER,
            )
        return EXIT_NO_PROVIDER

    try:
        await prepare_agent(agent)
        return await _execute(agent, prompt, output_format, config.permissions.sandbox)
    finally:
        await agent.close()


async def _execute(agent: Agent, prompt: str, output_format: OutputFormat, sandbox: str) -> int:
    metrics = RunMetrics(
        session_id=agent.session_id,
        model_id=agent.loop.model_id,
        sandbox=sandbox,
        max_tokens=agent.budget.max_tokens,
        max_turns=agent.budget.max_turns,
    )
    initial_usage = agent.loop.gateway.total_usage
    initial_cost = agent.budget.cost_used
    sink = OutputSink(output_format, metrics=metrics)
    sink.emit_session_started()

    from kalash.core.events import Event, EventType

    async def on_runtime(event: Event) -> None:
        if event.session_id not in (None, agent.session_id):
            return
        metrics.run_id = event.run_id or metrics.run_id
        if event.type == EventType.MODEL_REQUEST:
            used = str(event.data.get("model", ""))
            if used and used != metrics.model_id and used not in metrics.fallback_chain:
                metrics.fallback_chain.append(used)
        if (
            event.type == EventType.TOOL_COMPLETE
            and event.data.get("ok") is True
            and event.data.get("operation") in ("created", "modified")
            and event.data.get("path")
        ):
            change = {
                key: event.data[key]
                for key in ("path", "operation", "content_digest")
                if key in event.data
            }
            metrics.files_changed = [
                row for row in metrics.files_changed if row["path"] != change["path"]
            ]
            metrics.files_changed.append(change)
        sink.emit_runtime_event(event)

    def collect_metrics() -> None:
        usage = agent.loop.gateway.total_usage
        for counter in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        ):
            setattr(metrics, counter, getattr(usage, counter) - getattr(initial_usage, counter))
        metrics.usage_unknown = bool(agent.budget.unknown_usage_attempts)
        metrics.usage_source = "observed_partial" if metrics.usage_unknown else "provider_reported"
        metrics.budget_tokens_used = agent.budget.tokens_used
        metrics.turns_used = agent.budget.turns_used
        pricing = getattr(agent.loop.gateway.primary, "pricing", None)
        if (
            pricing is not None
            and pricing.currency == "USD"
            and (pricing.input_per_mtok or pricing.output_per_mtok)
            and not metrics.usage_unknown
        ):
            metrics.cost_usd = str(agent.budget.cost_used - initial_cost)

    bus = agent.loop.event_bus
    bus.on_all(on_runtime)

    try:
        result = await agent.send(prompt, on_text_delta=sink.on_text_delta)
    except KeyboardInterrupt:
        collect_metrics()
        sys.stderr.write("kalash: interrupted\n")
        return sink.finish_error(
            code="KALASH_CANCELLED",
            message="interrupted",
            error_class="internal",
            exit_code=EXIT_CANCELLED,
        )
    except Exception as exc:
        collect_metrics()
        from kalash.models.diagnose import explain

        msg = explain(str(exc))
        sys.stderr.write(f"kalash: {msg}\n")
        return sink.finish_error(
            code="KALASH_INTERNAL",
            message=msg,
            exit_code=EXIT_FAILURE,
        )

    finally:
        bus.off_all(on_runtime)

    collect_metrics()
    metrics.run_id = result.run_id
    metrics.verification = result.verification
    metrics.used_model = result.model
    metrics.completion_status = result.completion_status.value
    stop = result.termination_reason.value

    if (
        result.termination_reason is TerminationReason.ERROR or result.error
    ) and result.termination_reason not in (
        TerminationReason.BUDGET_EXHAUSTED,
        TerminationReason.MAX_ITERATIONS,
        TerminationReason.USER_INTERRUPT,
    ):
        err = result.error or "run failed"
        sys.stderr.write(f"kalash: {err}\n")
        if "REFUSED" in (err or "") or "approval" in (err or "").lower():
            return sink.finish_error(
                code="KALASH_APPROVAL_REQUIRED",
                message=err,
                error_class="permission",
                exit_code=EXIT_APPROVAL_REQUIRED,
                partial=result.final_response,
            )
        return sink.finish_error(
            code="KALASH_RUN_FAILED",
            message=err,
            exit_code=EXIT_FAILURE,
            partial=result.final_response,
        )

    if result.termination_reason in (
        TerminationReason.BUDGET_EXHAUSTED,
        TerminationReason.MAX_ITERATIONS,
    ):
        sys.stderr.write(f"kalash: stopped ({stop}) after {result.iterations} turns\n")
        return sink.finish_error(
            code="KALASH_BUDGET_EXCEEDED",
            message=result.error or f"stopped: {stop}",
            error_class="budget",
            exit_code=EXIT_BUDGET_EXCEEDED,
            partial=result.final_response,
        )

    if result.termination_reason is TerminationReason.USER_INTERRUPT:
        sys.stderr.write("kalash: interrupted\n")
        return sink.finish_error(
            code="KALASH_CANCELLED",
            message="interrupted",
            exit_code=EXIT_CANCELLED,
            partial=result.final_response,
        )

    if not result.final_response and output_format is OutputFormat.TEXT:
        if getattr(agent.loop, "_had_tool_work", False) or result.iterations > 1:
            return sink.finish_success(
                result_text="",
                stop_reason=stop,
                iterations=result.iterations,
                total_tokens=result.total_tokens,
                completion_status=result.completion_status.value,
                usage_unknown=result.usage_unknown,
            )
        sys.stderr.write("kalash: model returned no output\n")
        return EXIT_FAILURE

    return sink.finish_success(
        result_text=result.final_response,
        stop_reason=stop,
        iterations=result.iterations,
        total_tokens=result.total_tokens,
        completion_status=result.completion_status.value,
        usage_unknown=result.usage_unknown,
    )
