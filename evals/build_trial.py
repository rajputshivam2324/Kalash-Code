"""One bounded live build, with a frozen task/rubric and Codex judge handoff."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from evals.benchmark import TOOLS
from evals.build_judge import freeze_spec, prepare_packet, write_json
from evals.compact import cost_inr, source_hash
from evals.compact_budget import MeteredProvider, SuiteLedger
from evals.output import default_output
from kalash.core.config import BudgetConfig, KalashConfig, MemoryConfig, ModelConfig
from kalash.core.events import Event, EventBus
from kalash.core.privacy import scrub_text
from kalash.models.gateway import ModelGateway, RetryConfig
from kalash.models.providers.sarvam import SarvamProvider
from kalash.models.resolve import credential_for
from kalash.runtime.agent import build_agent
from kalash.sandbox.manager import preflight_ok
from kalash.tools.shell import terminate_session_backgrounds


async def run(
    root: Path,
    prior_path: Path,
    *,
    reasoning: str,
    output_limit: int,
    task_limit: int = 52_000,
    recovery_from: Path | None = None,
) -> None:
    if not preflight_ok():
        raise RuntimeError("Build evaluation requires the OS sandbox")
    prior = json.loads(prior_path.read_text())
    if prior["violated"] or any(item["status"] == "reserved" for item in prior["attempts"]):
        raise ValueError("Previous ledger is violated or still has pending requests")
    specification = freeze_spec(root)
    recovered_files = {}
    if recovery_from is not None:
        import hashlib

        for path in sorted((recovery_from / "workspace").rglob("*")):
            if not path.is_file() or path.is_symlink() or "__pycache__" in path.parts:
                continue
            relative = path.relative_to(recovery_from / "workspace")
            destination = root / "workspace" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            recovered_files[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    ledger = SuiteLedger(
        root / "ledger.json",
        limit=prior["limit"],
        attempts=prior["attempts"],
        refusals=prior.get("refusals", []),
        task_limit=task_limit,
    )
    if ledger.charged != prior["charged_tokens"] or not ledger.remaining:
        raise ValueError("Previous ledger totals are invalid or exhausted")
    ledger.save()
    key = credential_for("sarvam")
    if not key:
        raise RuntimeError("No Sarvam credential")
    before = ledger.charged
    source = {
        "source_tree_sha256": source_hash(),
        "python": sys.version,
        "platform": platform.platform(),
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], text=True)),
    }
    write_json(root / "source_manifest.json", source)
    provider = SarvamProvider(
        model="sarvam-105b",
        api_key=key,
        max_tokens=output_limit,
        reasoning_effort=None if reasoning == "off" else reasoning,
    )
    bus = EventBus()
    previous_home = os.environ.get("KALASH_HOME")
    os.environ["KALASH_HOME"] = str(root / "home")
    agent = None
    started = time.monotonic()
    termination, error, answer = "not_started", "", ""
    with (
        (root / "events.jsonl").open("w") as trace,
        (root / "provider.jsonl").open("w") as provider_trace,
    ):
        wrapped = MeteredProvider(
            provider, ledger, root.name + ":build-triage", trace=provider_trace, credential=key
        )

        async def record(event: Event) -> None:
            trace.write(
                scrub_text(json.dumps(asdict(event), default=str)).replace(key, "[credential]")
                + "\n"
            )
            trace.flush()

        bus.on_all(record)
        try:
            config = KalashConfig(
                project_dir=root / "workspace",
                memory=MemoryConfig(enabled=False),
                model=ModelConfig(temperature=0, max_output_tokens=output_limit),
                budget=BudgetConfig(
                    max_tokens=min(task_limit, ledger.remaining),
                    max_cost_usd=Decimal(100),
                    max_wallclock_s=300,
                    max_turns=10,
                    max_tool_calls=32,
                ),
            )
            agent, reason = build_agent(
                cwd=root / "workspace",
                provider=wrapped,
                provider_id="sarvam",
                model_id=provider.name,
                config=config,
                event_bus=bus,
                interactive=False,
                persist=False,
                extensions=False,
                allowed_tools=TOOLS | {"expand"},
                max_iterations=10,
            )
            if agent is None:
                raise RuntimeError(reason)
            agent.loop.gateway = ModelGateway(
                primary=wrapped, retry_config=RetryConfig(max_attempts=1)
            )
            agent.host.workspace_only = True
            agent.host.network_enabled = False
            task = json.loads((root / "task.json").read_text())
            prompt = task["prompt"]
            if recovery_from is not None:
                prompt = (
                    "Recover an interrupted build. Existing files are an unverified partial "
                    "attempt; inspect them, repair failures and run the requested tests. "
                    "Preserve useful work. The original task below describes the required "
                    "final artifact, not the current workspace state.\n\n" + prompt
                )
            write_json(root / "submitted_prompt.json", {"prompt": prompt})
            result = await agent.send(prompt)
            termination = result.termination_reason.value
            error, answer = result.error or "", result.final_response
        except Exception as exc:
            termination, error = "error", str(exc)
        finally:
            if agent is not None:
                await terminate_session_backgrounds(agent.session_id)
            await provider.close()
            if previous_home is None:
                os.environ.pop("KALASH_HOME", None)
            else:
                os.environ["KALASH_HOME"] = previous_home
    (root / "answer.txt").write_text(scrub_text(answer).replace(key, "[credential]"))
    manifest = {
        "model": "sarvam-105b",
        "reasoning": reasoning,
        "output_limit": output_limit,
        "temperature": 0,
        "sdk_retries": 0,
        "gateway_attempts": 1,
        "max_wallclock_s": 300,
        "max_turns": 10,
        "max_tool_calls": 32,
        "task_token_ceiling": task_limit,
        "recovery_from": str(recovery_from) if recovery_from else None,
        "recovered_files": recovered_files,
        "allowed_tools": sorted(TOOLS | {"expand"}),
        "prior_ledger": str(prior_path),
        "specification": specification,
        **source,
        "termination": termination,
        "error": scrub_text(error).replace(key, "[credential]"),
        "duration_s": round(time.monotonic() - started, 3),
        "tool_calls": agent.budget.tool_calls_used if agent else 0,
        "new_charged_tokens": ledger.charged - before,
        "cumulative_charged_tokens": ledger.charged,
        "allowance": ledger.limit,
        "cumulative_reported_tokens": sum(
            sum(
                item["usage"].get(k, 0)
                for k in ("input_tokens", "output_tokens", "cache_write_tokens")
            )
            for item in ledger.attempts
            if item["usage"]
        ),
        "cumulative_estimated_cost_inr": cost_inr(ledger),
        "within_allowance": not ledger.violated and ledger.charged <= ledger.limit,
    }
    write_json(root / "run.json", manifest)
    packet = await prepare_packet(root)
    sys.stdout.write(
        json.dumps(
            {
                "termination": termination,
                "checks_passed": sum(c["passed"] for c in packet["checks"]),
                "new_charged_tokens": ledger.charged - before,
                "cumulative_charged_tokens": ledger.charged,
            }
        )
        + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=default_output("build"))
    parser.add_argument("--prior-ledger", type=Path, required=True)
    parser.add_argument("--reasoning", choices=("off", "low", "medium", "high"), default="low")
    parser.add_argument("--output-limit", type=int, default=8192)
    parser.add_argument("--task-limit", type=int, default=52_000)
    parser.add_argument("--recovery-from", type=Path)
    args = parser.parse_args()
    if not 128 <= args.output_limit <= 32768:
        parser.error("--output-limit must be between 128 and 32768")
    if not 0 < args.task_limit < 250_000:
        parser.error("--task-limit must be between 1 and 249999")
    asyncio.run(
        run(
            args.output.resolve(),
            args.prior_ledger.resolve(),
            reasoning=args.reasoning,
            output_limit=args.output_limit,
            task_limit=args.task_limit,
            recovery_from=args.recovery_from.resolve() if args.recovery_from else None,
        )
    )


if __name__ == "__main__":
    main()
