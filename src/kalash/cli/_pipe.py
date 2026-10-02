"""Headless execution: ``kalash -p "…"`` and piped stdin."""

from __future__ import annotations

import asyncio
import sys

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
from kalash.runtime.agent import Agent
from kalash.runtime.loop import TerminationReason

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
        print("kalash: no prompt supplied", file=sys.stderr)
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
        print(f"kalash: {reason}", file=sys.stderr)
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
    )
    sink = OutputSink(output_format, metrics=metrics)
    sink.emit_session_started()

    from kalash.core.events import Event, EventType

    async def on_tool_complete(event: Event) -> None:
        ok = event.data.get("ok")
        if ok is None:
            ok = not event.data.get("error")
        sink.emit_tool_event(
            str(event.data.get("tool_name", "?")),
            ok=bool(ok),
            detail="",
        )

    async def on_tool_output(event: Event) -> None:
        if output_format is OutputFormat.STREAM_JSON:
            sink._emit(
                "tool.output",
                {
                    "tool_use_id": event.data.get("tool_use_id"),
                    "stream": event.data.get("stream"),
                    "chunk": str(event.data.get("chunk", ""))[:500],
                },
            )

    bus = agent.loop.event_bus
    bus.on(EventType.TOOL_COMPLETE, on_tool_complete)
    bus.on(EventType.TOOL_OUTPUT, on_tool_output)

    try:
        result = await agent.send(prompt, on_text_delta=sink.on_text_delta)
    except KeyboardInterrupt:
        print("kalash: interrupted", file=sys.stderr)
        return sink.finish_error(
            code="KALASH_CANCELLED",
            message="interrupted",
            error_class="internal",
            exit_code=EXIT_CANCELLED,
        )
    except Exception as exc:
        from kalash.models.diagnose import explain

        msg = explain(str(exc))
        print(f"kalash: {msg}", file=sys.stderr)
        return sink.finish_error(
            code="KALASH_INTERNAL",
            message=msg,
            exit_code=EXIT_FAILURE,
        )

    finally:
        bus.off(EventType.TOOL_COMPLETE, on_tool_complete)
        bus.off(EventType.TOOL_OUTPUT, on_tool_output)

    metrics.input_tokens = agent.budget.tokens_used
    stop = result.termination_reason.value

    if result.termination_reason is TerminationReason.ERROR:
        err = result.error or "run failed"
        print(f"kalash: {err}", file=sys.stderr)
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
        print(
            f"kalash: stopped ({stop}) after {result.iterations} turns",
            file=sys.stderr,
        )
        return sink.finish_error(
            code="KALASH_BUDGET_EXCEEDED",
            message=f"stopped: {stop}",
            error_class="budget",
            exit_code=EXIT_BUDGET_EXCEEDED,
            partial=result.final_response,
        )

    if result.termination_reason is TerminationReason.USER_INTERRUPT:
        print("kalash: interrupted", file=sys.stderr)
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
            )
        print("kalash: model returned no output", file=sys.stderr)
        return EXIT_FAILURE

    return sink.finish_success(
        result_text=result.final_response,
        stop_reason=stop,
        iterations=result.iterations,
        total_tokens=result.total_tokens,
    )
