"""Eval runner: ``python -m evals.run``.

Prints a report and exits non-zero if any suite regresses, so it can gate CI.
Deterministic and offline — no API key required.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sys
import tempfile
from pathlib import Path

from evals.project_build import run_project_build_suite
from evals.suites import (
    run_capability_suite,
    run_policy_suite,
    run_safety_suite,
    run_token_suite,
)

# A suite below its threshold fails the run. Safety is absolute: a gate that
# lets one dangerous command through is not "97% safe", it is broken.
THRESHOLDS: dict[str, float] = {
    "capability": 1.0,
    "safety": 1.0,
    "policy": 1.0,
    "token efficiency": 1.0,
    "project build": 1.0,
}


@contextlib.contextmanager
def _monkeypatch():
    """Standalone MonkeyPatch context, so evals run outside pytest."""
    from _pytest.monkeypatch import MonkeyPatch

    patcher = MonkeyPatch()
    try:
        yield patcher
    finally:
        patcher.undo()


def _rule(title: str) -> str:
    return f"\n{title}\n{'-' * len(title)}"


async def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="kalash-evals-"))
    reports = []
    notes: list[str] = []

    try:
        print("Kalash evals — deterministic, offline")

        capability = await run_capability_suite(root, _monkeypatch)
        reports.append(capability)

        safety_workspace = root / "safety"
        safety_workspace.mkdir(parents=True, exist_ok=True)
        with _monkeypatch() as patcher:
            patcher.setenv("KALASH_HOME", str(root / "safety_home"))
            reports.append(await run_safety_suite(safety_workspace))

        reports.append(await run_policy_suite())

        with _monkeypatch() as patcher:
            patcher.setenv("KALASH_HOME", str(root / "token_home"))
            token_report, notes = run_token_suite(root)
            reports.append(token_report)

        project_report = await run_project_build_suite(root, _monkeypatch)
        reports.append(project_report)

        # -- report ------------------------------------------------------
        failed = False
        for report in reports:
            print(_rule(report.name))
            failures = [r for r in report.results if not r.passed]
            print(f"  {report.passed}/{report.total} passed ({report.rate:.0%})")
            for result in report.results:
                if result.passed and result.detail:
                    print(f"    · {result.name}: {result.detail}")
            for result in failures:
                print(f"    FAIL {result.name}: {result.detail}")

            threshold = THRESHOLDS.get(report.name, 1.0)
            if report.rate < threshold:
                failed = True
                print(f"  below threshold {threshold:.0%}")

        if notes:
            print(_rule("token economics (input-equivalent units)"))
            for line in notes:
                print(f"  {line}")

        print(_rule("summary"))
        for report in reports:
            state = "ok" if report.rate >= THRESHOLDS.get(report.name, 1.0) else "REGRESSED"
            print(f"  {report.name:<18} {report.passed:>3}/{report.total:<3} {state}")

        return 1 if failed else 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
