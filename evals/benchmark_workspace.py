"""Isolated workspace creation and grading in a pristine copy."""

from __future__ import annotations

import secrets
import shlex
from pathlib import Path

from evals.benchmark_tasks import CodingTask
from kalash.tools.base import ToolContext
from kalash.tools.shell import ShellParams, ShellTool


def materialize(task: CodingTask, root: Path) -> None:
    root.mkdir(parents=True)
    for name, content in task.files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def candidate_files(workspace: Path) -> dict[str, str]:
    """Accept regular source files only; never follow symlinks or retain test edits."""
    files = {}
    for path in workspace.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Symlink in candidate: {path.relative_to(workspace)}")
        if not path.is_file():
            continue
        relative = path.relative_to(workspace)
        if "__pycache__" in relative.parts or ".pytest_cache" in relative.parts:
            continue
        if relative.parts[0] != "src" or path.suffix != ".py":
            raise ValueError(f"Out-of-scope file: {relative}")
        if path.stat().st_size > 1_000_000:
            raise ValueError(f"Oversized candidate file: {relative}")
        files[str(relative)] = path.read_text()
    return files


async def grade(task: CodingTask, workspace: Path, root: Path) -> tuple[str, str]:
    try:
        candidate = candidate_files(workspace)
    except (ValueError, OSError, UnicodeError) as exc:
        return "failed", str(exc)
    materialize(task, root)
    for name in task.files.keys() - candidate.keys():
        (root / name).unlink()
    for name, content in candidate.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    # Verification lives only in the validation sandbox, after the agent stopped.
    sentinel = "KALASH_VERIFIED_" + secrets.token_hex(16)
    (root / "verify.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parent / 'src'))\n"
        + task.hidden_test
        + f"\nprint({sentinel!r})\n"
    )
    context = ToolContext(
        session_id="grader",
        run_id="grader",
        cwd=root,
        writable_roots=(root,),
        allow_network=False,
        require_sandbox=True,
    )
    result = await ShellTool().execute(
        ShellParams(
            command=f"/usr/bin/python3 -I {shlex.quote(str(root / 'verify.py'))}", timeout=30
        ),
        context,
    )
    if result.error and result.error.code == "KALASH_SANDBOX_UNAVAILABLE":
        return "infra_error", result.error.message
    return (
        "resolved" if result.ok and sentinel in (result.content or "") else "failed"
    ), result.content or (result.error.message if result.error else "")
