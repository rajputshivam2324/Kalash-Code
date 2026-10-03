"""Fresh local coding evaluation, one suite-wide allowance below 250k tokens.

python -m evals.compact --output /tmp/kalash-compact --access-log /tmp/kalash-access-check.json
Hidden tests grade a fresh copy. This is a local smoke suite, not SWE-bench.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import subprocess
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from evals.benchmark import TOOLS, save_patch, scripted_provider
from evals.benchmark_tasks import load_tasks
from evals.benchmark_workspace import grade, materialize
from evals.compact_budget import MeteredProvider, SuiteLedger
from kalash.core.config import BudgetConfig, KalashConfig, MemoryConfig, ModelConfig
from kalash.core.events import Event, EventBus
from kalash.core.privacy import scrub_text
from kalash.models.providers.sarvam import SarvamProvider
from kalash.models.resolve import credential_for
from kalash.runtime.agent import build_agent
from kalash.sandbox.manager import preflight_ok
from kalash.tools.shell import terminate_session_backgrounds

PRICING_SOURCE = "https://docs.sarvam.ai/api/getting-started/pricing"


def cost_inr(ledger: SuiteLedger) -> str | None:
    if any(item["usage"] is None for item in ledger.attempts if item["status"] != "rejected_http"):
        return None
    cost = Decimal(0)
    for item in ledger.attempts:
        usage = item["usage"] or {}
        cached = usage.get("cache_read_tokens", 0)
        cost += Decimal(usage.get("input_tokens", 0) - cached) * Decimal("29.28")
        cost += Decimal(cached) * Decimal("10.98")
        cost += Decimal(usage.get("output_tokens", 0)) * Decimal("73.2")
    return str((cost / 1_000_000).quantize(Decimal("0.000001")))


async def run(args: argparse.Namespace) -> int:
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    tasks = load_tasks(args.tasks)
    ledger = SuiteLedger(args.output / "ledger.json", args.total_tokens)
    access = json.loads(args.access_log.read_text()) if args.access_log else {"requests": []}
    for entry in access["requests"]:
        usage = entry.get("usage")
        ledger.attempts.append(
            {
                "task": "access_probe",
                "reserved_tokens": entry["conservative_token_charge"],
                "charged_tokens": usage["total_tokens"]
                if usage
                else entry["conservative_token_charge"],
                "status": "rejected_http" if entry.get("http_status", 0) >= 400 else "complete",
                "usage": {
                    "input_tokens": usage["prompt_tokens"],
                    "output_tokens": usage["completion_tokens"],
                    "cache_read_tokens": 0,
                    "reasoning_tokens": 0,
                }
                if usage
                else None,
            }
        )
    if ledger.charged >= ledger.limit:
        raise ValueError("Access probes already exhausted the supplied budget")
    ledger.save()
    (args.output / "access.json").write_text(json.dumps(access, indent=2))
    manifest = {
        "suite": "compact-v1",
        "mode": "scripted" if args.scripted else "live",
        "model": "scripted" if args.scripted else "sarvam/sarvam-105b",
        "reasoning": "high",
        "output_limit": 4096,
        "total_token_allowance": ledger.limit,
        "task_actual_ceiling": 52_000,
        "reservation": "wire UTF8 bytes + 4096 + output allowance",
        "tasks_sha256": hashlib.sha256(args.tasks.read_bytes()).hexdigest(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], text=True)),
        "source_diff_sha256": hashlib.sha256(subprocess.check_output(["git", "diff"])).hexdigest(),
        "pricing_source": PRICING_SOURCE,
        "pricing_currency": "INR",
        "pricing_checked": "2026-10-03",
        "grading": "independent hidden assertions in a fresh network-disabled OS sandbox",
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    results = []
    available = any(
        item.get("model") == "sarvam-105b" and item.get("access") == "available"
        for item in access["requests"]
    )
    if not preflight_ok() or (not args.scripted and not available):
        blocker = (
            "OS sandbox unavailable" if not preflight_ok() else "Sarvam-105B access not verified"
        )
        summary = {
            "status": "blocked",
            "reason": blocker,
            "results": [],
            "charged_tokens": ledger.charged,
        }
        (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary))
        return 2
    previous_home = os.environ.get("KALASH_HOME")
    key = credential_for("sarvam") if not args.scripted else ""
    try:
        for task in tasks:
            root = args.output / task.id
            root.mkdir()
            workspace = root / "workspace"
            materialize(task, workspace)
            # Baseline must fail: otherwise the task does not evaluate a repair.
            baseline, baseline_detail = await grade(task, workspace, root / "baseline")
            if baseline != "failed":
                raise RuntimeError(f"Invalid task baseline {task.id}: {baseline} {baseline_detail}")
            os.environ["KALASH_HOME"] = str(root / "home")
            provider = (
                scripted_provider(task)
                if args.scripted
                else SarvamProvider(
                    model="sarvam-105b", api_key=key, max_tokens=4096, reasoning_effort="high"
                )
            )
            wrapped = provider if args.scripted else MeteredProvider(provider, ledger, task.id)
            bus = EventBus()
            started = time.monotonic()
            before = ledger.charged
            agent = None
            detail = ""
            outcome = "infra_error"
            termination = "not_started"
            with (root / "events.jsonl").open("w") as trace:

                async def record(event: Event) -> None:
                    trace.write(
                        scrub_text(json.dumps(asdict(event), default=str)).replace(
                            key, "[credential]"
                        )
                        if key
                        else scrub_text(json.dumps(asdict(event), default=str))
                    )
                    trace.write("\n")
                    trace.flush()

                bus.on_all(record)
                try:
                    config = KalashConfig(
                        project_dir=workspace,
                        memory=MemoryConfig(enabled=False),
                        model=ModelConfig(temperature=0, max_output_tokens=4096),
                        budget=BudgetConfig(
                            max_tokens=52_000,
                            max_cost_usd=Decimal(100),
                            max_wallclock_s=180,
                            max_turns=12,
                            max_tool_calls=40,
                        ),
                    )
                    agent, reason = build_agent(
                        cwd=workspace,
                        provider=wrapped,
                        provider_id="eval" if args.scripted else "sarvam",
                        model_id=provider.name,
                        config=config,
                        event_bus=bus,
                        interactive=False,
                        persist=False,
                        extensions=False,
                        allowed_tools=TOOLS,
                        max_iterations=12,
                    )
                    if agent is None:
                        raise RuntimeError(reason)
                    agent.host.workspace_only = True
                    agent.host.network_enabled = False
                    result = await agent.send(task.prompt)
                    termination = result.termination_reason.value
                    await terminate_session_backgrounds(agent.session_id)
                    outcome, detail = await grade(task, workspace, root / "validation")
                    if result.error:
                        detail = result.error + "\n" + detail
                    save_patch(task, workspace, root / "candidate.patch")
                    (root / "answer.txt").write_text(scrub_text(result.final_response))
                except Exception as exc:
                    detail = scrub_text(str(exc)).replace(key, "[credential]") if key else str(exc)
                finally:
                    if agent is not None:
                        await terminate_session_backgrounds(agent.session_id)
                    if hasattr(provider, "close"):
                        await provider.close()
            row = {
                "task": task.id,
                "baseline": baseline,
                "outcome": outcome,
                "termination": termination,
                "duration_s": round(time.monotonic() - started, 3),
                "charged_tokens": ledger.charged - before,
                "tool_calls": agent.budget.tool_calls_used if agent else 0,
                "detail": detail,
            }
            results.append(row)
            (root / "result.json").write_text(json.dumps(row, indent=2))
            print(f"{task.id}: {outcome}; {row['charged_tokens']} charged tokens", flush=True)
            if ledger.violated:
                break
    finally:
        if previous_home is None:
            os.environ.pop("KALASH_HOME", None)
        else:
            os.environ["KALASH_HOME"] = previous_home
    summary = {
        "suite": "compact-v1",
        "mode": manifest["mode"],
        "model": manifest["model"],
        "tasks": len(tasks),
        "attempted": len(results),
        "resolved": sum(r["outcome"] == "resolved" for r in results),
        "results": results,
        "allowance": ledger.limit,
        "charged_tokens": ledger.charged,
        "provider_reported_tokens": sum(
            sum(
                i["usage"].get(k, 0)
                for k in ("input_tokens", "output_tokens", "cache_write_tokens")
            )
            for i in ledger.attempts
            if i["usage"]
        ),
        "estimated_cost_inr": None if args.scripted else cost_inr(ledger),
        "within_allowance": not ledger.violated and ledger.charged < 250_000,
        "note": "One attempt/task; local smoke suite; no competitor or public-benchmark claim.",
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}), flush=True)
    return int(summary["resolved"] != len(tasks))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tasks", type=Path, default=Path(__file__).parent / "tasks" / "compact_v1.json"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--access-log", type=Path)
    parser.add_argument("--total-tokens", type=int, default=240_000)
    parser.add_argument("--scripted", action="store_true")
    args = parser.parse_args()
    if not 0 < args.total_tokens < 250_000:
        parser.error("Total allowance must be between 1 and 249999")
    if not args.scripted and args.access_log is None:
        parser.error("Live run requires the access probe log")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
