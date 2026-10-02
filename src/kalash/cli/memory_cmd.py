"""Memory management CLI commands."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

memory_app = typer.Typer(help="Manage long-term memory")
console = Console()


@memory_app.command("add")
def add(
    content: Annotated[str, typer.Argument(help="Memory content to store")],
    tags: Annotated[
        list[str] | None,
        typer.Option("--tag", "-t", help="Tags for the memory"),
    ] = None,
    scope: Annotated[
        str,
        typer.Option("--scope", "-s", help="Scope: project, global, session"),
    ] = "project",
) -> None:
    """Add a new memory entry."""
    from kalash.memory.manager import MemoryManager

    manager = MemoryManager()
    entry = manager.add(content=content, tags=tags or [], scope=scope)
    console.print(f"[green]Memory added:[/green] {entry.id[:12]}")


@memory_app.command("search")
def search(
    query: Annotated[str, typer.Argument(help="Search query")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 10,
    scope: Annotated[
        str | None,
        typer.Option("--scope", "-s", help="Filter by scope"),
    ] = None,
) -> None:
    """Search memories by keyword (FTS) across configured providers."""
    from kalash.memory.manager import MemoryManager

    manager = MemoryManager()
    results = manager.search(query=query, limit=limit, scope=scope)

    table = Table(title=f"Search results for: {query!r}")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Score", justify="right", style="yellow")
    table.add_column("Content")
    table.add_column("Tags", style="dim")

    for r in results:
        table.add_row(
            r.id[:12],
            f"{r.score:.3f}",
            r.content[:80],
            ", ".join(r.tags) if r.tags else "—",
        )

    console.print(table)


@memory_app.command("ls")
def ls(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max entries to show")] = 20,
    scope: Annotated[
        str | None,
        typer.Option("--scope", "-s", help="Filter by scope"),
    ] = None,
    tag: Annotated[
        str | None,
        typer.Option("--tag", "-t", help="Filter by tag"),
    ] = None,
) -> None:
    """List memory entries."""
    from kalash.memory.manager import MemoryManager

    manager = MemoryManager()
    entries = manager.list_entries(limit=limit, scope=scope, tag=tag)

    table = Table(title="Memories")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Created", style="green")
    table.add_column("Scope", style="yellow")
    table.add_column("Content")
    table.add_column("Tags", style="dim")

    for e in entries:
        table.add_row(
            e.id[:12],
            e.created_at.strftime("%Y-%m-%d %H:%M"),
            e.scope,
            e.content[:60],
            ", ".join(e.tags) if e.tags else "—",
        )

    console.print(table)


@memory_app.command("forget")
def forget(
    memory_id: Annotated[str, typer.Argument(help="Memory ID to delete")],
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip confirmation")] = False,
) -> None:
    """Delete a memory entry."""
    from kalash.memory.manager import MemoryManager

    if not force:
        confirm = typer.confirm(f"Delete memory {memory_id!r}?")
        if not confirm:
            raise typer.Abort()

    manager = MemoryManager()
    deleted = manager.delete(memory_id)

    if deleted:
        console.print(f"[green]Memory {memory_id!r} deleted.[/green]")
    else:
        console.print(f"[red]Memory {memory_id!r} not found.[/red]")
        raise typer.Exit(code=1)


@memory_app.command("export")
def export(
    output: Annotated[
        str | None,
        typer.Option("--output", "-o", help="Output file path (defaults to stdout)"),
    ] = None,
    format: Annotated[
        str,
        typer.Option("--format", "-f", help="Export format: json, jsonl"),
    ] = "json",
) -> None:
    """Export all memories."""
    from kalash.memory.manager import MemoryManager

    manager = MemoryManager()
    data = manager.export_all(format=format)

    if output:
        from pathlib import Path

        Path(output).write_text(data, encoding="utf-8")
        console.print(f"[green]Exported to {output}[/green]")
    else:
        console.print(data)


@memory_app.command("import")
def import_(
    input_file: Annotated[str, typer.Argument(help="File to import memories from")],
    format: Annotated[
        str,
        typer.Option("--format", "-f", help="Import format: json, jsonl"),
    ] = "json",
    merge: Annotated[
        bool,
        typer.Option("--merge", help="Merge with existing (skip duplicates)"),
    ] = True,
) -> None:
    """Import memories from a file."""
    from pathlib import Path

    from kalash.memory.manager import MemoryManager

    path = Path(input_file)
    if not path.exists():
        console.print(f"[red]File not found: {input_file}[/red]")
        raise typer.Exit(code=1)

    manager = MemoryManager()
    count = manager.import_from_file(path, format=format, merge=merge)
    console.print(f"[green]Imported {count} memories.[/green]")


@memory_app.command("doctor")
def doctor() -> None:
    """Run memory health checks."""
    from kalash.memory.manager import MemoryManager

    manager = MemoryManager()
    report = manager.health_check()

    console.print("[bold]Memory Health Check[/bold]\n")

    for check in report.checks:
        icon = "✓" if check.passed else "✗"
        color = "green" if check.passed else "red"
        console.print(f"  [{color}]{icon}[/{color}] {check.name}: {check.message}")

    console.print(f"\n[bold]Status:[/bold] {report.status}")

    if not report.healthy:
        raise typer.Exit(code=1)
