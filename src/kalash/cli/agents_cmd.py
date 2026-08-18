"""Subagent definition CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from kalash.core.paths import agents_dirs

agents_app = typer.Typer(help="Agent management")
console = Console()


def _discover_agent_files() -> list[tuple[str, Path, str]]:
    """Return (name, path, source) for each agent definition."""
    found: dict[str, tuple[Path, str]] = {}
    for directory in agents_dirs():
        if not directory.is_dir():
            continue
        source = "user" if ".kalash" not in str(directory) or str(directory).endswith("/agents") else "project"
        for path in sorted(directory.glob("*.md")):
            found[path.stem] = (path, source if "agents" in str(directory) else "project")
    return [(name, path, source) for name, (path, source) in sorted(found.items())]


@agents_app.command("ls")
def ls() -> None:
    """List available subagent definitions."""
    agents = _discover_agent_files()

    table = Table(title="Agents")
    table.add_column("Name", style="cyan")
    table.add_column("Source", style="yellow")
    table.add_column("Path", style="dim")

    for name, path, source in agents:
        table.add_row(name, source, str(path))

    if not agents:
        console.print(
            "[dim]No agents found. Add .md files under ~/.kalash/agents/ or .kalash/agents/[/dim]"
        )
    else:
        console.print(table)


@agents_app.command("run")
def run(
    name: Annotated[str, typer.Argument(help="Agent name")],
    prompt: Annotated[str, typer.Option("--prompt", "-p", help="Task prompt")],
    model: Annotated[
        Optional[str],
        typer.Option("--model", "-m", help="Model override"),
    ] = None,
) -> None:
    """Run a subagent headlessly and print its result."""
    import asyncio

    from kalash.agents.loader import load_agent_definition
    from kalash.runtime.agent import run_isolated

    definition = load_agent_definition(name)
    if definition is None:
        console.print(f"[red]Unknown agent:[/red] {name}")
        console.print("[dim]Run `kalash agents ls` to see available agents.[/dim]")
        raise typer.Exit(code=1)

    async def execute() -> None:
        result = await run_isolated(
            prompt,
            model_id=model or definition.model,
            mode=definition.mode,
            max_turns=definition.max_turns,
            agent_instructions=definition.body or None,
        )
        if result.get("error"):
            console.print(f"[red]{result['error']}[/red]")
            raise typer.Exit(code=1)
        console.print(result.get("output", ""))

    asyncio.run(execute())


@agents_app.command("new")
def new(
    name: Annotated[str, typer.Argument(help="Agent name")],
) -> None:
    """Create a starter agent definition in .kalash/agents/."""
    import re

    if not re.fullmatch(r"[a-z0-9-]{1,64}", name):
        console.print("[red]Name must match [a-z0-9-] (1–64 chars)[/red]")
        raise typer.Exit(code=1)

    path = Path.cwd() / ".kalash" / "agents" / f"{name}.md"
    if path.exists():
        console.print(f"[red]Agent already exists:[/red] {path}")
        raise typer.Exit(code=1)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
name: {name}
description: Specialized agent for {name.replace("-", " ")} tasks.
tools: [read, write, edit, search, shell]
model: sonnet
max_turns: 25
---

You are the {name} subagent. Complete the delegated task and return a concise summary.
""",
        encoding="utf-8",
    )
    console.print(f"[green]Created agent at[/green] {path}")
