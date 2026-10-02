"""Live Real-World Project Build Runner & Comparison Engine.

Executes a live LLM turn through Kalash Code to build a complete project,
then compares the resulting codebase against the golden standard implementation.
"""

from __future__ import annotations

import asyncio
import difflib
import os
import subprocess
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from kalash.permissions.prompt import ApprovalResponse, PromptContext, PromptResult
from kalash.runtime.agent import build_agent

console = Console()


class LiveAllowingUI:
    """Auto-approver for live benchmark runs."""

    async def show_approval_prompt(self, context: PromptContext) -> PromptResult:
        console.print(
            f"[bold cyan]● [gate][/bold cyan] Approved action: [bold]{context.action}[/bold] ({context.tool_name})"
        )
        return PromptResult(response=ApprovalResponse.ALLOW_ONCE)

    async def show_info(self, message: str) -> None:
        console.print(f"[dim]{message}[/dim]")


async def main() -> int:
    workspace = Path.cwd() / "live_build_kvstore"
    workspace.mkdir(parents=True, exist_ok=True)
    golden_dir = Path.cwd() / "evals" / "golden_kvstore"

    console.print(
        Panel.fit(
            "[bold cyan]Kalash Code — Live Project Build & Golden Reference Comparison[/bold cyan]\n"
            "Testing live model agent loop, tool execution, streaming, and output fidelity.",
            border_style="cyan",
        )
    )

    console.print(f"[dim]Target Workspace:[/dim] {workspace}")
    console.print(f"[dim]Golden Standard Directory:[/dim] {golden_dir}")

    # Build agent in live workspace
    agent, why = build_agent(cwd=workspace, interactive=False)
    if agent is None:
        console.print(f"[bold red]Failed to assemble agent:[/bold red] {why}")
        return 1

    if agent.host.approval is not None:
        agent.host.approval.ui = LiveAllowingUI()
        agent.host.approval.non_interactive = False

    prompt = (
        "Build a complete Python embedded Key-Value store package named 'kvstore' in this workspace.\n"
        "Requirements:\n"
        "1. pyproject.toml with hatchling backend, typer, and rich dependencies.\n"
        "2. src/kvstore/__init__.py and src/kvstore/store.py with KVStore class supporting:\n"
        "   - get(key, default=None), set(key, value, ttl_seconds=None), delete(key), list_keys(), clear(), stats()\n"
        "   - JSON disk persistence and TTL expiry purging\n"
        "3. src/kvstore/cli.py with Typer commands (set, get, delete, ls, stats).\n"
        "4. tests/test_store.py with comprehensive Pytest tests for get/set, TTL expiry, delete, and persistence.\n"
        "5. README.md with installation and usage instructions.\n"
        "6. Run pytest via shell to verify that your tests pass."
    )

    console.print("\n[bold yellow]▶ Prompting Kalash live...[/bold yellow]")
    start_time = time.monotonic()

    def on_text(delta: str) -> None:
        sys.stdout.write(delta)
        sys.stdout.flush()

    try:
        outcome = await agent.send(prompt, on_text_delta=on_text)
        duration = time.monotonic() - start_time
        console.print(
            f"\n\n[bold green]✓ Live Agent Run Complete[/bold green] in {duration:.1f}s ({outcome.iterations} iterations, {agent.budget.tool_calls_used} tool calls)"
        )
    except Exception as exc:
        console.print(f"\n[bold red]Agent run encountered exception:[/bold red] {exc}")
        return 1

    # --- Verification 1: Run Pytest on live generated workspace ---
    console.print("\n[bold cyan]1. Executing Pytest on Live Workspace...[/bold cyan]")
    test_res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(workspace / "tests")],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(workspace / "src")},
    )
    test_passed = test_res.returncode == 0
    console.print(f"Pytest Output:\n{test_res.stdout.strip() or test_res.stderr.strip()}")
    console.print(
        f"Status: [{'green' if test_passed else 'red'}]{'PASS' if test_passed else 'FAIL'}[/]"
    )

    # --- Verification 2: File-by-File Comparison against Golden Reference ---
    console.print(
        "\n[bold cyan]2. Comparing Live Generation vs Golden Reference Standard...[/bold cyan]"
    )
    table = Table(
        title="Live Build vs Golden Reference Comparison", border_style="cyan", show_lines=True
    )
    table.add_column("File / Component", style="bold")
    table.add_column("Live Size", justify="right")
    table.add_column("Golden Size", justify="right")
    table.add_column("Similarity Match", justify="center")
    table.add_column("Status", justify="center")

    golden_files = list(golden_dir.rglob("*"))
    all_golden_rels = [
        str(f.relative_to(golden_dir))
        for f in golden_files
        if f.is_file() and "__pycache__" not in str(f)
    ]

    all_matched = True
    for rel in sorted(all_golden_rels):
        live_file = workspace / rel
        golden_file = golden_dir / rel

        golden_text = golden_file.read_text(encoding="utf-8") if golden_file.exists() else ""
        live_text = live_file.read_text(encoding="utf-8") if live_file.exists() else ""

        if not live_file.exists():
            table.add_row(
                rel,
                "[red]MISSING[/red]",
                f"{len(golden_text)} B",
                "0%",
                "[bold red]FAIL[/bold red]",
            )
            all_matched = False
            continue

        matcher = difflib.SequenceMatcher(None, golden_text.splitlines(), live_text.splitlines())
        ratio = matcher.ratio()
        status = (
            "[bold green]PASS[/bold green]" if ratio >= 0.50 else "[bold yellow]DIFF[/bold yellow]"
        )
        table.add_row(
            rel,
            f"{len(live_text)} B",
            f"{len(golden_text)} B",
            f"{ratio:.0%}",
            status,
        )

    console.print(table)
    return 0 if (test_passed and all_matched) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
