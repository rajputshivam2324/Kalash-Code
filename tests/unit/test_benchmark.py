"""Benchmark grading must fail wrong, tampered or prematurely exited candidates."""

import json

import pytest
from evals.benchmark_tasks import CodingTask, load_tasks
from evals.benchmark_workspace import candidate_files, grade, materialize

from kalash.tools.base import ToolContext
from kalash.tools.shell import ShellParams, ShellTool


def task():
    return CodingTask(
        id="bug",
        prompt="fix answer",
        files={"src/answer.py": "def answer(): return 0\n"},
        hidden_test="from answer import answer\nassert answer() == 42\n",
    )


@pytest.mark.parametrize("identifier", ["..", ".", "a/b", "a\n", ""])
def test_invalid_task_id_is_rejected(tmp_path, identifier):
    fixture = tmp_path / "tasks.json"
    fixture.write_text(
        json.dumps(
            [{"id": identifier, "prompt": "fix", "files": {"src/a.py": ""}, "hidden_test": ""}]
        )
    )
    with pytest.raises(ValueError):
        load_tasks(fixture)


def test_candidate_symlink_is_rejected(tmp_path):
    root = tmp_path / "candidate"
    materialize(task(), root)
    (root / "src" / "escape.py").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="Symlink"):
        candidate_files(root)


@pytest.mark.parametrize(
    "source, expected",
    [
        ("def answer(): return 0\n", "failed"),
        ("def answer(): return 42\n", "resolved"),
        ("import os; os._exit(0)\n", "failed"),
    ],
)
async def test_pristine_hidden_grading(tmp_path, source, expected):
    root = tmp_path / "candidate"
    materialize(task(), root)
    assert not (root / "verify.py").exists()
    (root / "src/answer.py").write_text(source)
    outcome, detail = await grade(task(), root, tmp_path / "validation")
    assert outcome == expected, detail


async def test_grader_file_tampering_is_rejected(tmp_path):
    root = tmp_path / "candidate"
    materialize(task(), root)
    (root / "verify.py").write_text("print('passed')")
    outcome, _ = await grade(task(), root, tmp_path / "validation")
    assert outcome == "failed"


async def test_shell_refuses_missing_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "kalash.tools.shell._wrap_sandboxed", lambda argv, ctx, cwd: (argv, False, "unavailable")
    )
    result = await ShellTool().execute(
        ShellParams(command="touch escaped"),
        ToolContext(session_id="test", run_id="test", cwd=tmp_path, writable_roots=(tmp_path,)),
    )
    assert result.error.code == "KALASH_SANDBOX_UNAVAILABLE"
    assert not (tmp_path / "escaped").exists()
