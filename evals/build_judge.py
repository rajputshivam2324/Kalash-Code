"""Freeze build artifacts, verify behavior, and validate an independent judge score."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import shlex
import sys
from pathlib import Path
from typing import Any

from kalash.tools.base import ToolContext
from kalash.tools.shell import ShellParams, ShellTool

TASK_PATH = Path("evals/tasks/build_triage.json")
RUBRIC_PATH = Path("evals/tasks/build_triage_rubric.json")
CHECKS_PATH = Path("evals/triage_checks.py")


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze_spec(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    (root / "workspace").mkdir()
    for source, name in (
        (TASK_PATH, "task.json"),
        (RUBRIC_PATH, "rubric.json"),
        (CHECKS_PATH, "acceptance.py"),
    ):
        (root / name).write_bytes(source.read_bytes())
    specification = {
        name: hash_file(root / name) for name in ("task.json", "rubric.json", "acceptance.py")
    }
    write_json(root / "specification.json", specification)
    return specification


def artifact_snapshot(workspace: Path) -> dict[str, Any]:
    files: dict[str, Any] = {}
    total = 0
    for path in sorted(workspace.rglob("*")):
        if path.is_symlink():
            raise ValueError("Candidate contains a symlink")
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(workspace)
        # Data stores are not shipped code. Only these task deliverables enter grading.
        allowed = str(relative) in {"triage.py", "README.md"} or (
            relative.parts[0] == "tests" and path.suffix == ".py"
        )
        if not allowed:
            continue
        total += path.stat().st_size
        if path.stat().st_size > 65_536 or total > 200_000:
            raise ValueError("Candidate exceeds judge packet size limits")
        files[str(relative)] = {"sha256": hash_file(path), "content": path.read_text()}
    return files


async def execute_validation(root: Path, command: str, timeout: int = 90) -> Any:
    return await ShellTool().execute(
        ShellParams(command=command, timeout=timeout),
        ToolContext(
            session_id="build-grader",
            run_id="build-grader",
            cwd=root,
            writable_roots=(root,),
            allow_network=False,
            require_sandbox=True,
        ),
    )


async def prepare_packet(root: Path) -> dict[str, Any]:
    specification = json.loads((root / "specification.json").read_text())
    if any(hash_file(root / name) != digest for name, digest in specification.items()):
        raise ValueError("Frozen task, rubric or checks changed")
    files = artifact_snapshot(root / "workspace")
    validation = root / "validation"
    validation.mkdir(exist_ok=False)
    for name, artifact in files.items():
        path = validation / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(artifact["content"])
    (validation / "acceptance.py").write_bytes((root / "acceptance.py").read_bytes())
    result = await execute_validation(
        validation, f"/usr/bin/python3 -I {shlex.quote(str(validation / 'acceptance.py'))}"
    )
    if not result.ok or result.truncated:
        raise RuntimeError(result.content or str(result.error))
    checks = json.loads(result.content)
    rubric = json.loads((root / "rubric.json").read_text())
    if {check["id"] for check in checks} != set(rubric["required_checks"]):
        raise ValueError("Acceptance report is incomplete")
    # The builder's tests are additional evidence, never the acceptance oracle.
    (validation / "candidate_checks.py").write_text(
        "import sys, unittest\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
        "suite = unittest.defaultTestLoader.discover('tests')\n"
        "result = unittest.TextTestRunner(verbosity=2).run(suite)\n"
        "sys.exit(0 if result.wasSuccessful() else 1)\n"
    )
    tests = await execute_validation(
        validation,
        f"/usr/bin/python3 -I {shlex.quote(str(validation / 'candidate_checks.py'))}",
        timeout=30,
    )
    output = tests.content or ""
    match = re.search(r"Ran (\d+) tests?", output)
    test_count = int(match[1]) if match else 0
    candidate_tests = {
        "id": "candidate_tests",
        "passed": tests.ok and test_count >= 6,
        "count": test_count,
        "detail": output[:16_000],
    }
    if files != artifact_snapshot(root / "workspace"):
        raise ValueError("Candidate workspace changed during verification")
    packet = {
        "task": json.loads((root / "task.json").read_text()),
        "rubric": rubric,
        "specification": specification,
        "files": files,
        "checks": checks,
        "candidate_tests": candidate_tests,
        "builder_run": json.loads((root / "run.json").read_text()),
        "judge_instruction": rubric["instructions"],
        "evidence_ids": sorted([*files, *(check["id"] for check in checks), "candidate_tests"]),
        "limitations": [
            "Single uncalibrated Codex reviewer; one build attempt.",
            "Functional checks are not an adversarial sandbox/security audit.",
            "Builder termination and verified artifact quality are separate.",
        ],
    }
    write_json(root / "judge_packet.json", packet)
    return packet


def finalize(root: Path, judgment: dict[str, Any]) -> dict[str, Any]:
    packet_path = root / "judge_packet.json"
    if judgment.get("packet_sha256") != hash_file(packet_path):
        raise ValueError("Judgment must identify the exact frozen packet")
    if judgment.get("judge") != "Codex":
        raise ValueError("This run requires the explicitly requested Codex reviewer")
    packet = json.loads(packet_path.read_text())
    rubric = packet["rubric"]
    expected = {dimension["id"]: dimension for dimension in rubric["dimensions"]}
    dimensions = judgment["dimensions"]
    if len(dimensions) != len(expected) or {d["id"] for d in dimensions} != set(expected):
        raise ValueError("Judge must score every dimension exactly once")
    if sum(d["weight"] for d in expected.values()) != 100:
        raise ValueError("Rubric weights must sum to 100")
    score = 0.0
    for dimension in dimensions:
        value = dimension["score"]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 5
        ):
            raise ValueError("Scores must be finite numbers between zero and five")
        evidence = dimension.get("evidence", [])
        if (
            not dimension.get("rationale")
            or not evidence
            or any(item not in packet["evidence_ids"] for item in evidence)
        ):
            raise ValueError("Each judgment needs a rationale and known evidence IDs")
        score += value / 5 * expected[dimension["id"]]["weight"]
    checks_pass = all(check["passed"] for check in packet["checks"])
    tests_pass = packet["candidate_tests"]["passed"]
    deliverables = {
        "program": "triage.py" in packet["files"],
        "readme": "README.md" in packet["files"],
        "tests": any(name.startswith("tests/test_") for name in packet["files"]),
    }
    report = {
        "task": packet["task"]["id"],
        "judge": "Codex",
        "overall_score": round(score, 2),
        "passed": (
            all(deliverables.values())
            and checks_pass
            and tests_pass
            and score >= rubric["pass_threshold"]
        ),
        "deliverables_present": deliverables,
        "acceptance_passed": sum(check["passed"] for check in packet["checks"]),
        "acceptance_total": len(packet["checks"]),
        "candidate_tests": packet["candidate_tests"],
        "judgment": judgment,
        "packet_sha256": hash_file(packet_path),
        "builder_run": packet["builder_run"],
        "limitations": packet["limitations"],
        "artifact_hashes": {name: value["sha256"] for name, value in packet["files"].items()},
    }
    write_json(root / "judge_report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--judgment", type=Path, help="Finalize a Codex-authored judgment JSON")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.judgment:
        report = finalize(root, json.loads(args.judgment.read_text()))
        sys.stdout.write(
            json.dumps({k: report[k] for k in ("overall_score", "passed", "acceptance_passed")})
            + "\n"
        )
        sys.exit(0 if report["passed"] else 1)
    else:
        asyncio.run(prepare_packet(root))
        sys.stdout.write(f"Judge packet: {root / 'judge_packet.json'}\n")


if __name__ == "__main__":
    main()
