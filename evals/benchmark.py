"""Run local held-out coding trials with scripted or live provider responses.

Usage: python -m evals.benchmark --scripted --output /tmp/kalash-benchmark
This suite is a harness smoke benchmark, not SWE-bench.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import json
import os
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from evals.benchmark_tasks import CodingTask, load_tasks
from evals.benchmark_workspace import candidate_files, grade, materialize
from evals.harness import ScriptedProvider, ScriptedTurn
from kalash.core.budget import Pricing
from kalash.core.config import BudgetConfig, KalashConfig, MemoryConfig, ModelConfig
from kalash.core.events import Event, EventBus
from kalash.core.redact import redact
from kalash.models.providers.sarvam import SarvamProvider
from kalash.models.resolve import build_provider
from kalash.runtime.agent import build_agent
from kalash.runtime.loop import TerminationReason
from kalash.sandbox.manager import preflight_ok

TOOLS = frozenset({"read", "edit", "write", "multi_edit", "glob", "list", "search", "shell"})


class PricedProvider:
    """Use benchmark-supplied rates rather than a provider's catalog estimate."""

    def __init__(self, provider: Any, pricing: Pricing) -> None:
        self._provider = provider
        self.pricing = pricing

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)


def scripted_provider(task: CodingTask) -> ScriptedProvider:
    turns = []
    files = dict(task.files)
    for index, edit in enumerate(task.scripted_edits):
        arguments = {**edit, "digest": hashlib.sha256(files[edit["path"]].encode()).hexdigest()}
        turns.extend(
            [
                ScriptedTurn(tool="read", args={"path": edit["path"]}, call_id=f"read_{index}"),
                ScriptedTurn(tool="edit", args=arguments, call_id=f"edit_{index}"),
            ]
        )
        files[edit["path"]] = files[edit["path"]].replace(edit["old_str"], edit["new_str"], 1)
    return ScriptedProvider([*turns, ScriptedTurn(text="Candidate ready for verification.")])


def save_patch(task: CodingTask, workspace: Path, path: Path) -> None:
    candidate = candidate_files(workspace)
    diff = []
    for name in sorted(task.files.keys() | candidate.keys()):
        diff.extend(
            difflib.unified_diff(
                task.files.get(name, "").splitlines(keepends=True),
                candidate.get(name, "").splitlines(keepends=True),
                fromfile=f"a/{name}",
                tofile=f"b/{name}",
            )
        )
    path.write_text("".join(diff))


async def trial(task: CodingTask, args: argparse.Namespace, attempt: int) -> dict[str, Any]:
    root = args.output / f"{task.id}-{attempt}"
    root.mkdir(parents=True, exist_ok=False)
    workspace = root / "workspace"
    materialize(task, workspace)
    previous_home = os.environ.get("KALASH_HOME")
    os.environ["KALASH_HOME"] = str(root / "home")
    bus = EventBus()
    started = time.monotonic()
    provider = None
    agent = None
    outcome, detail, termination = "infra_error", "", "not_started"
    with (root / "events.jsonl").open("w") as events:

        async def record(event: Event) -> None:
            events.write(redact(json.dumps(asdict(event), default=str)).text + "\n")
            events.flush()

        bus.on_all(record)
        try:
            if not preflight_ok():
                raise RuntimeError(
                    "A working OS sandbox is required to run and grade benchmark trials"
                )
            if args.scripted:
                provider = scripted_provider(task)
            elif args.provider == "sarvam":
                from kalash.models.resolve import credential_for

                provider = SarvamProvider(
                    model=args.model,
                    api_key=credential_for("sarvam"),
                    max_tokens=args.max_output,
                    reasoning_effort=args.reasoning_effort,
                    pricing=Pricing(
                        input_per_mtok=args.input_price / args.usd_inr,
                        output_per_mtok=args.output_price / args.usd_inr,
                        cache_read_per_mtok=(
                            args.cache_hit_price / args.usd_inr
                            if args.cache_hit_price is not None
                            else None
                        ),
                    ),
                )
            else:
                resolution = build_provider(args.provider, args.model)
                if not resolution.ok:
                    raise RuntimeError(resolution.reason)
                provider = resolution.provider
                provider = PricedProvider(
                    provider,
                    Pricing(
                        input_per_mtok=args.input_price / args.usd_inr,
                        output_per_mtok=args.output_price / args.usd_inr,
                        cache_read_per_mtok=(
                            args.cache_hit_price / args.usd_inr
                            if args.cache_hit_price is not None
                            else None
                        ),
                    ),
                )
            config = KalashConfig(
                project_dir=workspace,
                memory=MemoryConfig(enabled=False),
                model=ModelConfig(temperature=0, max_output_tokens=args.max_output),
                budget=BudgetConfig(
                    max_tokens=args.max_tokens,
                    max_cost_usd=args.max_cost,
                    max_wallclock_s=args.timeout,
                    max_turns=args.max_iterations,
                    max_tool_calls=args.max_tool_calls,
                ),
            )
            agent, reason = build_agent(
                cwd=workspace,
                provider=provider,
                provider_id="eval" if args.scripted else args.provider,
                model_id="eval/scripted" if args.scripted else args.model,
                config=config,
                event_bus=bus,
                interactive=False,
                persist=False,
                extensions=False,
                allowed_tools=TOOLS,
                max_iterations=args.max_iterations,
            )
            if agent is None:
                raise RuntimeError(reason)
            agent.host.workspace_only = True
            agent.host.network_enabled = False
            result = await agent.send(task.prompt)
            termination = result.termination_reason.value
            from kalash.tools.shell import terminate_session_backgrounds

            await terminate_session_backgrounds(agent.session_id)
            # Grade even an incomplete run; keep termination independently visible.
            outcome, detail = await grade(task, workspace, root / "validation")
            if result.error and outcome != "resolved":
                outcome = (
                    "infra_error"
                    if result.termination_reason == TerminationReason.ERROR
                    else "budget"
                )
                detail = result.error + "\n" + detail
            save_patch(task, workspace, root / "candidate.patch")
        except Exception as exc:
            detail = str(exc)
        finally:
            if agent is not None:
                from kalash.tools.shell import terminate_session_backgrounds

                await terminate_session_backgrounds(agent.session_id)
            if provider is not None and hasattr(provider, "close"):
                await provider.close()
            if previous_home is None:
                os.environ.pop("KALASH_HOME", None)
            else:
                os.environ["KALASH_HOME"] = previous_home
    usage = asdict(agent.loop.gateway.total_usage) if agent else {}
    result = {
        "task_id": task.id,
        "attempt": attempt,
        "mode": "scripted" if args.scripted else "live",
        "model": "eval/scripted" if args.scripted else args.model,
        "outcome": outcome,
        "detail": detail,
        "termination": termination,
        "duration_s": round(time.monotonic() - started, 3),
        "usage": usage,
        "cost_usd": str(agent.budget.cost_used) if agent else "0",
        "iterations": agent.loop._iteration if agent else 0,
        "tool_calls": agent.budget.tool_calls_used if agent else 0,
    }
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


async def run(args: argparse.Namespace) -> int:
    tasks = load_tasks(args.tasks)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "config.json").write_text(json.dumps(vars(args), default=str, indent=2))
    results = []
    for task in tasks:
        for attempt in range(1, args.trials + 1):
            result = await trial(task, args, attempt)
            results.append(result)
            print(f"{task.id} #{attempt}: {result['outcome']}")
    summary = {
        "suite": "local-coding-smoke",
        "mode": "scripted" if args.scripted else "live",
        "trials": len(results),
        "resolved": sum(r["outcome"] == "resolved" for r in results),
        "infra_errors": sum(r["outcome"] == "infra_error" for r in results),
        "pass_at_1": sum(r["outcome"] == "resolved" for r in results) / len(results),
        f"pass_at_{args.trials}": sum(
            any(r["task_id"] == task.id and r["outcome"] == "resolved" for r in results)
            for task in tasks
        )
        / len(tasks),
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))
    return int(summary["resolved"] != len(results))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tasks", type=Path, default=Path(__file__).parent / "tasks" / "smoke.json"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scripted", action="store_true")
    parser.add_argument("--provider", default="sarvam")
    parser.add_argument("--model", default="glm5.3")
    parser.add_argument("--reasoning-effort", choices=["low", "high", "max"], default="max")
    parser.add_argument("--input-price", type=Decimal)
    parser.add_argument("--output-price", type=Decimal)
    parser.add_argument("--cache-hit-price", type=Decimal)
    parser.add_argument(
        "--usd-inr",
        type=Decimal,
        default=Decimal(1),
        help="INR per USD when prices are in INR; record the conversion used",
    )
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--max-output", type=int, default=32768)
    parser.add_argument("--max-tokens", type=int, default=100_000)
    parser.add_argument("--max-cost", type=Decimal, default=Decimal("1"))
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--max-iterations", type=int, default=30)
    parser.add_argument("--max-tool-calls", type=int, default=100)
    args = parser.parse_args()
    if (
        min(args.trials, args.timeout, args.max_iterations, args.max_tool_calls, args.max_tokens)
        < 1
    ):
        parser.error("Trial counts and budgets must be positive")
    if not args.usd_inr.is_finite() or args.usd_inr <= 0:
        parser.error("--usd-inr must be finite and positive")
    if args.max_output < 1 or not args.max_cost.is_finite() or args.max_cost <= 0:
        parser.error("Output and cost budgets must be positive")
    if any(
        price is not None and (not price.is_finite() or price < 0)
        for price in (args.input_price, args.output_price, args.cache_hit_price)
    ):
        parser.error("Token prices must be finite and nonnegative")
    if not args.scripted:
        from kalash.models.resolve import credential_for

        if not credential_for(args.provider):
            parser.error(f"Live trials require a configured {args.provider} credential")
        if args.input_price is None or args.output_price is None:
            parser.error(
                "Live trials require current --input-price and --output-price per million tokens"
            )
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
