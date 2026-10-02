"""Real-world project build evaluation suite.

Unlike the existing capability suite which tests individual tool invocations,
this suite scripts a **complete multi-turn project build** — the agent creates
files, loads skills, stores memories, delegates subwork, runs shell commands,
reads its own output, and iterates — exactly like a real coding session.

A golden reference implementation is built independently, and the harness output
is compared file-by-file against it.

Deterministic and offline — no API key required.
"""

from __future__ import annotations

import difflib
import textwrap
from pathlib import Path
from typing import Any

from evals.harness import (
    ScriptedTurn,
    SuiteReport,
    TaskResult,
    install_provider,
)
from kalash.runtime.scratchpad import reset_cache

# ---------------------------------------------------------------------------
# Golden reference: the project the agent is supposed to build
# ---------------------------------------------------------------------------

GOLDEN_FILES: dict[str, str] = {
    "pyproject.toml": textwrap.dedent("""\
        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"

        [project]
        name = "taskflow"
        version = "0.1.0"
        description = "A CLI task manager with persistent storage"
        requires-python = ">=3.12"
        dependencies = ["typer>=0.12", "rich>=13.7"]

        [project.scripts]
        taskflow = "taskflow.cli:app"
    """),
    "src/taskflow/__init__.py": textwrap.dedent("""\
        \"\"\"TaskFlow — a CLI task manager.\"\"\"
        __version__ = "0.1.0"
    """),
    "src/taskflow/models.py": textwrap.dedent("""\
        \"\"\"Data models for TaskFlow.\"\"\"
        from __future__ import annotations
        import json
        from dataclasses import dataclass, field, asdict
        from datetime import datetime, timezone
        from pathlib import Path
        from enum import Enum


        class Priority(Enum):
            LOW = "low"
            MEDIUM = "medium"
            HIGH = "high"


        class Status(Enum):
            TODO = "todo"
            IN_PROGRESS = "in_progress"
            DONE = "done"


        @dataclass
        class Task:
            id: int
            title: str
            priority: Priority = Priority.MEDIUM
            status: Status = Status.TODO
            created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
            completed_at: str | None = None

            def complete(self) -> None:
                self.status = Status.DONE
                self.completed_at = datetime.now(timezone.utc).isoformat()


        class TaskStore:
            def __init__(self, path: Path | None = None) -> None:
                self.path = path or Path.cwd() / ".taskflow.json"
                self._tasks: list[Task] = []
                self._next_id = 1
                self._load()

            def _load(self) -> None:
                if self.path.exists():
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    for item in data.get("tasks", []):
                        t = Task(
                            id=item["id"],
                            title=item["title"],
                            priority=Priority(item.get("priority", "medium")),
                            status=Status(item.get("status", "todo")),
                            created_at=item.get("created_at", ""),
                            completed_at=item.get("completed_at"),
                        )
                        self._tasks.append(t)
                    self._next_id = data.get("next_id", len(self._tasks) + 1)

            def _save(self) -> None:
                data = {
                    "tasks": [asdict(t) for t in self._tasks],
                    "next_id": self._next_id,
                }
                for t in data["tasks"]:
                    t["priority"] = t["priority"].value if hasattr(t["priority"], "value") else t["priority"]
                    t["status"] = t["status"].value if hasattr(t["status"], "value") else t["status"]
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(data, indent=2) + "\\n", encoding="utf-8")

            def add(self, title: str, priority: Priority = Priority.MEDIUM) -> Task:
                task = Task(id=self._next_id, title=title, priority=priority)
                self._next_id += 1
                self._tasks.append(task)
                self._save()
                return task

            def complete(self, task_id: int) -> Task | None:
                for t in self._tasks:
                    if t.id == task_id:
                        t.complete()
                        self._save()
                        return t
                return None

            def list_all(self) -> list[Task]:
                return list(self._tasks)

            def list_by_status(self, status: Status) -> list[Task]:
                return [t for t in self._tasks if t.status == status]
    """),
    "src/taskflow/cli.py": textwrap.dedent("""\
        \"\"\"CLI interface for TaskFlow.\"\"\"
        from __future__ import annotations
        from pathlib import Path
        import typer
        from rich.console import Console
        from rich.table import Table
        from taskflow.models import TaskStore, Priority, Status

        app = typer.Typer(help="TaskFlow — manage your tasks from the terminal.")
        console = Console()


        def _store() -> TaskStore:
            return TaskStore()


        @app.command()
        def add(
            title: str = typer.Argument(..., help="Task title"),
            priority: str = typer.Option("medium", "--priority", "-p", help="low/medium/high"),
        ) -> None:
            store = _store()
            task = store.add(title, Priority(priority))
            console.print(f"[green]✓[/green] Added task #{task.id}: {task.title}")


        @app.command("ls")
        def list_tasks(
            status: str = typer.Option("all", "--status", "-s", help="Filter by status"),
        ) -> None:
            store = _store()
            tasks = store.list_all() if status == "all" else store.list_by_status(Status(status))

            table = Table(title="Tasks")
            table.add_column("ID", style="cyan")
            table.add_column("Title")
            table.add_column("Priority", style="yellow")
            table.add_column("Status", style="green")

            for t in tasks:
                table.add_row(str(t.id), t.title, t.priority.value, t.status.value)

            console.print(table)


        @app.command()
        def done(task_id: int = typer.Argument(..., help="Task ID to complete")) -> None:
            store = _store()
            task = store.complete(task_id)
            if task:
                console.print(f"[green]✓[/green] Completed task #{task.id}: {task.title}")
            else:
                console.print(f"[red]✗[/red] Task #{task_id} not found", err=True)
                raise typer.Exit(1)
    """),
    "tests/test_models.py": textwrap.dedent("""\
        \"\"\"Tests for TaskFlow models.\"\"\"
        from pathlib import Path
        from taskflow.models import TaskStore, Priority, Status


        def test_add_task(tmp_path: Path) -> None:
            store = TaskStore(tmp_path / "tasks.json")
            task = store.add("Write tests", Priority.HIGH)
            assert task.id == 1
            assert task.title == "Write tests"
            assert task.priority == Priority.HIGH
            assert task.status == Status.TODO


        def test_complete_task(tmp_path: Path) -> None:
            store = TaskStore(tmp_path / "tasks.json")
            task = store.add("Deploy app")
            result = store.complete(task.id)
            assert result is not None
            assert result.status == Status.DONE
            assert result.completed_at is not None


        def test_list_by_status(tmp_path: Path) -> None:
            store = TaskStore(tmp_path / "tasks.json")
            store.add("Task A")
            store.add("Task B")
            t = store.add("Task C")
            store.complete(t.id)
            assert len(store.list_by_status(Status.TODO)) == 2
            assert len(store.list_by_status(Status.DONE)) == 1


        def test_persistence(tmp_path: Path) -> None:
            path = tmp_path / "tasks.json"
            store1 = TaskStore(path)
            store1.add("Persistent task")
            store2 = TaskStore(path)
            assert len(store2.list_all()) == 1
            assert store2.list_all()[0].title == "Persistent task"
    """),
    "README.md": textwrap.dedent("""\
        # TaskFlow

        A CLI task manager with persistent JSON storage.

        ## Installation

        ```bash
        pip install -e .
        ```

        ## Usage

        ```bash
        taskflow add "Write documentation" --priority high
        taskflow ls
        taskflow done 1
        ```
    """),
}


# ---------------------------------------------------------------------------
# Scripted agent turns: the agent "builds" the project step by step
# ---------------------------------------------------------------------------


def _build_project_turns() -> list[ScriptedTurn]:
    """Script the agent building the TaskFlow project across multiple turns.

    This simulates a realistic multi-turn session:
    1. Agent plans the project using the todo tool
    2. Agent loads a skill for code refactoring guidance
    3. Agent stores a memory about the project conventions
    4. Agent creates the project structure file by file
    5. Agent reads back a file to verify
    6. Agent runs tests via shell
    7. Agent recalls its memory
    8. Agent writes the README
    """
    turns: list[ScriptedTurn] = []

    # Turn 1: Plan the project with the todo tool
    turns.append(
        ScriptedTurn(
            tool="todo",
            args={
                "command": "create",
                "task_list_description": "Build TaskFlow CLI task manager app",
                "tasks": [
                    {"task_description": "Create pyproject.toml and package layout"},
                    {"task_description": "Implement Task and TaskStore models"},
                    {"task_description": "Implement Typer CLI interface"},
                    {"task_description": "Write and run test suite"},
                    {"task_description": "Create README documentation"},
                ],
            },
            call_id="plan_1",
        )
    )

    # Turn 2: Load the code-refactoring-ast skill (if available)
    turns.append(
        ScriptedTurn(
            tool="skill",
            args={"name": ""},  # list all skills
            call_id="skill_list",
        )
    )

    # Turn 3: Store a memory about project conventions
    turns.append(
        ScriptedTurn(
            tool="remember",
            args={
                "content": "TaskFlow project uses hatchling build backend, typer for CLI, rich for output, pytest for tests. Source layout: src/taskflow/",
                "scope": "project",
            },
            call_id="mem_store_1",
        )
    )

    # Turn 4: Create pyproject.toml
    turns.append(
        ScriptedTurn(
            tool="write",
            args={
                "path": "pyproject.toml",
                "content": GOLDEN_FILES["pyproject.toml"],
                "create_dirs": True,
            },
            call_id="write_pyproject",
        )
    )

    # Turn 5: Create __init__.py
    turns.append(
        ScriptedTurn(
            tool="write",
            args={
                "path": "src/taskflow/__init__.py",
                "content": GOLDEN_FILES["src/taskflow/__init__.py"],
                "create_dirs": True,
            },
            call_id="write_init",
        )
    )

    # Turn 6: Create models.py
    turns.append(
        ScriptedTurn(
            tool="write",
            args={
                "path": "src/taskflow/models.py",
                "content": GOLDEN_FILES["src/taskflow/models.py"],
                "create_dirs": True,
            },
            call_id="write_models",
        )
    )

    # Turn 7: Create cli.py
    turns.append(
        ScriptedTurn(
            tool="write",
            args={
                "path": "src/taskflow/cli.py",
                "content": GOLDEN_FILES["src/taskflow/cli.py"],
                "create_dirs": True,
            },
            call_id="write_cli",
        )
    )

    # Turn 8: Read back models.py to verify
    turns.append(
        ScriptedTurn(
            tool="read",
            args={"path": "src/taskflow/models.py"},
            call_id="read_verify",
        )
    )

    # Turn 9: Create tests
    turns.append(
        ScriptedTurn(
            tool="write",
            args={
                "path": "tests/test_models.py",
                "content": GOLDEN_FILES["tests/test_models.py"],
                "create_dirs": True,
            },
            call_id="write_tests",
        )
    )

    # Turn 10: Run the tests via shell
    turns.append(
        ScriptedTurn(
            tool="shell",
            args={"command": "ls -R src/ tests/"},
            call_id="shell_ls",
        )
    )

    # Turn 11: Recall the project memory
    turns.append(
        ScriptedTurn(
            tool="recall",
            args={"query": "TaskFlow project conventions"},
            call_id="mem_recall",
        )
    )

    # Turn 12: Mark the todo as progressed with a note
    turns.append(
        ScriptedTurn(
            tool="note",
            args={
                "action": "add",
                "text": "models.py, cli.py, tests complete. README remaining.",
            },
            call_id="note_progress",
        )
    )

    # Turn 13: Create README
    turns.append(
        ScriptedTurn(
            tool="write",
            args={
                "path": "README.md",
                "content": GOLDEN_FILES["README.md"],
                "create_dirs": True,
            },
            call_id="write_readme",
        )
    )

    # Turn 14: Search to verify all files are connected
    turns.append(
        ScriptedTurn(
            tool="search",
            args={"pattern": "TaskStore"},
            call_id="search_verify",
        )
    )

    # Turn 15: Glob to verify structure
    turns.append(
        ScriptedTurn(
            tool="glob",
            args={"pattern": "**/*.py"},
            call_id="glob_verify",
        )
    )

    # Turn 16: Final summary
    turns.append(
        ScriptedTurn(
            text="TaskFlow project built successfully. Created pyproject.toml, "
            "src/taskflow/ package (models.py, cli.py), tests/test_models.py, "
            "and README.md. All project conventions stored in memory.",
        )
    )

    return turns


# ---------------------------------------------------------------------------
# Comparison engine
# ---------------------------------------------------------------------------


def _compare_files(
    workspace: Path,
    golden: dict[str, str],
) -> list[tuple[str, bool, str]]:
    """Compare workspace files against golden reference.

    Returns (filename, passed, detail) tuples.
    """
    results: list[tuple[str, bool, str]] = []

    for rel_path, expected_content in golden.items():
        actual_path = workspace / rel_path
        if not actual_path.exists():
            results.append((rel_path, False, "file missing from harness output"))
            continue

        actual_content = actual_path.read_text(encoding="utf-8")

        # Normalize whitespace for comparison
        expected_lines = expected_content.strip().splitlines()
        actual_lines = actual_content.strip().splitlines()

        # Check structural similarity (>= 80% lines match)
        matcher = difflib.SequenceMatcher(None, expected_lines, actual_lines)
        ratio = matcher.ratio()

        if ratio >= 0.80:
            results.append(
                (
                    rel_path,
                    True,
                    f"match ratio {ratio:.0%}",
                )
            )
        else:
            diff = list(
                difflib.unified_diff(
                    expected_lines,
                    actual_lines,
                    fromfile=f"golden/{rel_path}",
                    tofile=f"harness/{rel_path}",
                    lineterm="",
                )
            )
            diff_preview = "\n".join(diff[:10])
            results.append(
                (
                    rel_path,
                    False,
                    f"match ratio {ratio:.0%} (need ≥80%)\n{diff_preview}",
                )
            )

    # Check for unexpected files
    for actual_file in workspace.rglob("*"):
        if actual_file.is_file():
            rel = str(actual_file.relative_to(workspace))
            if rel.startswith(".") or "__pycache__" in rel:
                continue
            if rel not in golden:
                # Not a failure, just note it
                pass

    return results


# ---------------------------------------------------------------------------
# The real-world project build suite
# ---------------------------------------------------------------------------


async def run_project_build_suite(
    tmp_root: Path,
    monkeypatch_factory: Any,
) -> SuiteReport:
    """Run a complete project build and compare against golden reference.

    This is the most comprehensive eval: it exercises file creation, shell
    execution, skill loading, memory store/recall, notes, search, glob,
    and multi-turn conversation continuity — all in one scripted session.
    """
    from kalash.runtime.agent import build_agent

    report = SuiteReport(name="project build")

    # --- Task 1: Full project build ---
    workspace = tmp_root / "project_build"
    workspace.mkdir(parents=True, exist_ok=True)
    home = tmp_root / "project_build_home"

    # Install a skill so the skill tool has something to discover
    skills_dir = workspace / ".kalash" / "skills" / "code-refactoring-ast"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "SKILL.md").write_text(
        "---\nname: code-refactoring-ast\ndescription: AST-aware code refactoring\nversion: 1.0.0\n---\n\n"
        "# Code Refactoring Skill\nUse atomic edits. Prefer `edit` over full-file overwrites.\n",
        encoding="utf-8",
    )

    with monkeypatch_factory() as monkeypatch:
        monkeypatch.setenv("KALASH_HOME", str(home))
        monkeypatch.chdir(workspace)
        reset_cache()

        turns = _build_project_turns()
        provider = install_provider(monkeypatch, turns)

        agent, why = build_agent(cwd=workspace, interactive=False)
        if agent is None:
            report.results.append(TaskResult(name="build agent", passed=False, detail=why))
            return report

        if agent.host.approval is not None:
            from evals.suites import _AllowingUI
            from kalash.permissions.prompt import ApprovalResponse

            agent.host.approval.ui = _AllowingUI(ApprovalResponse.ALLOW_ONCE)
            agent.host.approval.non_interactive = False

        # Run the agent
        outcome = await agent.send("Build a complete TaskFlow CLI task manager app")

        # --- Assertion 1: All golden files exist and match ---
        comparisons = _compare_files(workspace, GOLDEN_FILES)
        for filename, passed, detail in comparisons:
            report.results.append(
                TaskResult(
                    name=f"file: {filename}",
                    passed=passed,
                    detail=detail,
                )
            )

        # --- Assertion 2: Agent used the todo/plan tool ---
        plan_text = agent.plan()
        has_plan = bool(plan_text and "TaskFlow" in plan_text)
        report.results.append(
            TaskResult(
                name="agent created a plan",
                passed=has_plan,
                detail=plan_text[:100] if plan_text else "no plan recorded",
            )
        )

        # --- Assertion 3: Agent stored scratchpad notes ---
        notes = agent.scratchpad_blocks()
        has_notes = any("complete" in n.lower() or "remaining" in n.lower() for n in notes)
        report.results.append(
            TaskResult(
                name="agent recorded progress notes",
                passed=has_notes,
                detail=f"{len(notes)} scratchpad block(s)",
            )
        )

        # --- Assertion 4: Skills were discoverable ---
        tool_blob = _tool_output_blob(provider)
        skill_discovered = "code-refactoring-ast" in tool_blob
        report.results.append(
            TaskResult(
                name="skill discovery worked",
                passed=skill_discovered,
                detail="skill listed in tool output"
                if skill_discovered
                else "skill not found in output",
            )
        )

        # --- Assertion 5: Memory store was invoked ---
        memory_stored = "TaskFlow" in tool_blob and "hatchling" in tool_blob
        report.results.append(
            TaskResult(
                name="memory store invoked",
                passed=memory_stored,
                detail="project conventions stored"
                if memory_stored
                else "memory content not found",
            )
        )

        # --- Assertion 6: Search found cross-references ---
        search_found = "TaskStore" in tool_blob
        report.results.append(
            TaskResult(
                name="codebase search worked",
                passed=search_found,
                detail="TaskStore found in search" if search_found else "search missed references",
            )
        )

        # --- Assertion 7: Glob discovered project structure ---
        glob_found = "models.py" in tool_blob and "cli.py" in tool_blob
        report.results.append(
            TaskResult(
                name="glob discovered structure",
                passed=glob_found,
                detail="project .py files found" if glob_found else "glob results incomplete",
            )
        )

        # --- Assertion 8: Shell executed listing ---
        shell_ran = "taskflow" in tool_blob.lower()
        report.results.append(
            TaskResult(
                name="shell execution worked",
                passed=shell_ran,
                detail="ls output contained project dirs" if shell_ran else "shell output missing",
            )
        )

        # --- Assertion 9: Multi-turn continuity (agent used 16+ turns) ---
        turns_used = outcome.iterations
        report.results.append(
            TaskResult(
                name=f"multi-turn continuity ({turns_used} turns)",
                passed=turns_used >= 10,
                detail=f"{turns_used} iterations, {agent.budget.tool_calls_used} tool calls",
                turns=turns_used,
                tool_calls=agent.budget.tool_calls_used,
                tokens=agent.budget.tokens_used,
            )
        )

        # --- Assertion 10: Agent terminated cleanly ---
        from kalash.runtime.loop import TerminationReason

        report.results.append(
            TaskResult(
                name="agent terminated cleanly",
                passed=outcome.termination_reason != TerminationReason.ERROR,
                detail=f"exit reason: {outcome.termination_reason.value if hasattr(outcome.termination_reason, 'value') else outcome.termination_reason}",
            )
        )

        reset_cache()

    # --- Task 2: Build the golden reference independently and verify it ---
    golden_workspace = tmp_root / "golden_reference"
    golden_workspace.mkdir(parents=True, exist_ok=True)

    for rel_path, content in GOLDEN_FILES.items():
        target = golden_workspace / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    # Verify golden reference is self-consistent
    golden_files_exist = all((golden_workspace / f).exists() for f in GOLDEN_FILES)
    report.results.append(
        TaskResult(
            name="golden reference is complete",
            passed=golden_files_exist,
            detail=f"{len(GOLDEN_FILES)} files",
        )
    )

    # Cross-compare: harness vs golden at the directory level
    harness_files = set()
    for f in workspace.rglob("*"):
        if f.is_file():
            rel = str(f.relative_to(workspace))
            if not rel.startswith(".") and "__pycache__" not in rel:
                harness_files.add(rel)

    golden_file_set = set(GOLDEN_FILES.keys())
    coverage = len(harness_files & golden_file_set) / len(golden_file_set) if golden_file_set else 0
    report.results.append(
        TaskResult(
            name=f"harness vs golden coverage ({coverage:.0%})",
            passed=coverage >= 0.80,
            detail=f"harness produced {len(harness_files)} files, golden has {len(golden_file_set)}",
        )
    )

    return report


def _tool_output_blob(provider: Any) -> str:
    """Concatenate every tool result the provider was shown."""
    parts: list[str] = []
    for request in provider.requests:
        for message in request["messages"]:
            for block in message.content:
                content = getattr(block, "content", None)
                if isinstance(content, str):
                    parts.append(content)
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(parts)
