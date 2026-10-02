"""Hooks CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

hooks_app = typer.Typer(help="Hook management")
console = Console()


@hooks_app.command("ls")
def ls() -> None:
    """List hooks discovered from .kalash/hooks and .kiro/hooks."""
    from kalash.hooks.load import discover_hooks

    hooks = discover_hooks(Path.cwd())

    table = Table(title="Hooks")
    table.add_column("Name", style="cyan")
    table.add_column("Event", style="yellow")
    table.add_column("Matcher", style="dim")
    table.add_column("Type")

    for hook in hooks:
        table.add_row(
            hook.name,
            hook.event.value,
            hook.matcher or "—",
            hook.handler_type.value,
        )

    if not hooks:
        console.print("[dim]No hooks configured. Add JSON files under .kalash/hooks/[/dim]")
    else:
        console.print(table)


@hooks_app.command("test")
def test(
    event: Annotated[
        str,
        typer.Argument(help="Hook event to simulate, e.g. PostToolUse"),
    ],
    tool: Annotated[
        str,
        typer.Option("--tool", "-t", help="Tool name for matcher tests"),
    ] = "write",
) -> None:
    """Dry-run hooks for an event without starting the agent."""
    import asyncio

    from kalash.hooks.events import HookEvent, HookPayload
    from kalash.hooks.load import build_hook_runner

    try:
        hook_event = HookEvent(event)
    except ValueError:
        console.print(f"[red]Unknown event:[/red] {event}")
        raise typer.Exit(code=1) from None

    runner = build_hook_runner(Path.cwd())
    if runner is None:
        console.print("[yellow]No hooks registered.[/yellow]")
        return

    async def run() -> None:
        payload = HookPayload(
            event=hook_event,
            session_id="test",
            data={"tool_name": tool},
        )
        results = await runner.dispatch(payload)
        for result in results:
            status = "[green]ok[/green]" if result.exit_code == 0 else "[red]fail[/red]"
            detail = result.stderr or result.stdout or str(result.exit_code)
            console.print(f"{status} {result.hook_id}: {detail}")

    asyncio.run(run())


@hooks_app.command("trust")
def trust(
    path: Annotated[
        str | None,
        typer.Argument(help="Hook file or project directory"),
    ] = None,
) -> None:
    """Authorize the current contents of the selected project hook files."""
    from kalash.core.trust import trust_file
    from kalash.hooks.load import HOOK_DIRS

    base = Path(path) if path else Path.cwd()
    if base.is_file():
        targets = [base]
    else:
        targets = [
            hook_file
            for relative in HOOK_DIRS
            for hook_file in sorted((base / relative).glob("*.json"))
            if (base / relative).is_dir()
        ]

    if not targets:
        console.print("[dim]No hook files found.[/dim]")
        return

    table = Table(title="Hook trust fingerprints")
    table.add_column("File", style="cyan")
    table.add_column("SHA-256", style="dim")

    for hook_file in targets:
        digest = trust_file(hook_file)[:16]
        table.add_row(str(hook_file), digest)

    console.print(table)
