"""Judge scores need real evidence; high subjective scores cannot override failed checks."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

import pytest
from evals.build_judge import (
    RUBRIC_PATH,
    artifact_snapshot,
    finalize,
    freeze_spec,
    hash_file,
    write_json,
)


def judge_fixture(root: Path, *, checks_pass: bool = True) -> dict[str, Any]:
    root.mkdir()
    rubric = json.loads(RUBRIC_PATH.read_text())
    packet = {
        "task": {"id": "build-triage-v1"},
        "rubric": rubric,
        "files": {
            name: {"sha256": "fixture"}
            for name in ("triage.py", "README.md", "tests/test_triage.py")
        },
        "checks": [{"id": name, "passed": checks_pass} for name in rubric["required_checks"]],
        "candidate_tests": {"passed": True, "count": 6},
        "evidence_ids": ["candidate_tests", *rubric["required_checks"]],
        "builder_run": {"termination": "budget_exhausted"},
        "limitations": [],
    }
    write_json(root / "judge_packet.json", packet)
    return {
        "judge": "Codex",
        "packet_sha256": hash_file(root / "judge_packet.json"),
        "dimensions": [
            {
                "id": d["id"],
                "score": 5,
                "rationale": "Inspected evidence",
                "evidence": ["candidate_tests"],
            }
            for d in rubric["dimensions"]
        ],
        "summary": "Reviewed independently",
    }


def test_subjective_maximum_cannot_override_failed_acceptance(tmp_path: Path) -> None:
    root = tmp_path / "run"
    judgment = judge_fixture(root, checks_pass=False)
    report = finalize(root, judgment)
    assert report["overall_score"] == 100
    assert report["passed"] is False


def test_judge_uses_controller_weights_and_artifact_quality(tmp_path: Path) -> None:
    root = tmp_path / "run"
    judgment = judge_fixture(root)
    judgment["dimensions"][0]["score"] = 3
    judgment["overall_score"] = 100
    report = finalize(root, judgment)
    assert report["overall_score"] == 82
    assert report["passed"] is True  # Termination is reported, not used as the quality oracle.


def test_missing_readme_cannot_pass_even_with_maximal_judge_scores(tmp_path: Path) -> None:
    root = tmp_path / "run"
    judgment = judge_fixture(root)
    packet_path = root / "judge_packet.json"
    packet = json.loads(packet_path.read_text())
    del packet["files"]["README.md"]
    write_json(packet_path, packet)
    judgment["packet_sha256"] = hash_file(packet_path)
    report = finalize(root, judgment)
    assert report["overall_score"] == 100
    assert report["passed"] is False
    assert report["deliverables_present"]["readme"] is False


@pytest.mark.parametrize("score", [True, -1, 6, float("nan"), float("inf"), "5"])
def test_invalid_judge_scores_are_rejected(tmp_path: Path, score: Any) -> None:
    root = tmp_path / "run"
    judgment = judge_fixture(root)
    judgment["dimensions"][0]["score"] = score
    with pytest.raises(ValueError, match="finite numbers"):
        finalize(root, judgment)


def test_judge_must_use_exact_packet_and_known_evidence(tmp_path: Path) -> None:
    root = tmp_path / "run"
    judgment = judge_fixture(root)
    judgment["packet_sha256"] = "stale"
    with pytest.raises(ValueError, match="exact frozen packet"):
        finalize(root, judgment)
    judgment["packet_sha256"] = hash_file(root / "judge_packet.json")
    judgment["dimensions"][0]["evidence"] = ["builder_says_tests_pass"]
    with pytest.raises(ValueError, match="known evidence"):
        finalize(root, judgment)


def test_spec_frozen_before_build_and_candidate_symlinks_rejected(tmp_path: Path) -> None:
    root = tmp_path / "run"
    specification = freeze_spec(root)
    assert all(hash_file(root / name) == digest for name, digest in specification.items())
    assert not list((root / "workspace").iterdir())
    outside = tmp_path / "outside.py"
    outside.write_text("danger = True\n")
    (root / "workspace" / "triage.py").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        artifact_snapshot(root / "workspace")


def test_missing_program_cannot_pass_negative_acceptance_checks(tmp_path: Path) -> None:
    from evals.build_judge import CHECKS_PATH

    script = tmp_path / "acceptance.py"
    script.write_bytes(CHECKS_PATH.read_bytes())
    result = subprocess.run(
        [sys.executable, "-I", str(script)], capture_output=True, text=True, check=True, timeout=10
    )
    checks = json.loads(result.stdout)
    assert len(checks) == 10
    assert not any(check["passed"] for check in checks)
