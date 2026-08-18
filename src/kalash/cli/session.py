"""Session management CLI commands."""

from __future__ import annotations

from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

session_app = typer.Typer(help="Manage sessions")
console = Console()


@session_app.command("ls")
def ls(
    limit: Annotated[
        int, typer.Option("--limit", "-n", help="Max sessions to show")
    ] = 20,
    status: Annotated[
        Optional[str],
        typer.Option("--status", "-s", help="Filter by status (active, completed, abandoned)"),
    ] = None,
) -> None:
    """List recent sessions."""
    from kalash.runtime.session import SessionManager

    manager = SessionManager()
    sessions = manager.list_sessions(limit=limit, status=status)

    table = Table(title="Sessions")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Started", style="green")
    table.add_column("Status", style="yellow")
    table.add_column("Turns", justify="right")
    table.add_column("Summary")

    for s in sessions:
        table.add_row(
            s.id[:12],
            s.started_at.strftime("%Y-%m-%d %H:%M"),
            s.status,
            str(s.turn_count),
            s.summary or "—",
        )

    console.print(table)


@session_app.command("show")
def show(
    session_id: Annotated[str, typer.Argument(help="Session ID to inspect")],
    turns: Annotated[
        Optional[int],
        typer.Option("--turns", "-t", help="Number of recent turns to display"),
    ] = None,
) -> None:
    """Show details of a specific session."""
    from kalash.runtime.session import SessionManager

    manager = SessionManager()
    session = manager.get_session(session_id)

    if session is None:
        console.print(f"[red]Session {session_id!r} not found.[/red]")
        raise typer.Exit(code=1)

    console.print(f"[bold]Session:[/bold] {session.id}")
    console.print(f"[bold]Started:[/bold] {session.started_at}")
    console.print(f"[bold]Status:[/bold] {session.status}")
    console.print(f"[bold]Turns:[/bold] {session.turn_count}")
    console.print(f"[bold]Model:[/bold] {session.model}")
    console.print(f"[bold]Tokens:[/bold] {session.total_tokens:,}")

    if turns:
        console.print(f"\n[bold]Last {turns} turns:[/bold]")
        for turn in session.get_turns(limit=turns):
            role_color = {"user": "blue", "assistant": "green", "system": "yellow"}.get(
                turn.role, "white"
            )
            console.print(f"  [{role_color}]{turn.role}:[/{role_color}] {turn.content[:120]}")


@session_app.command("export")
def export(
    session_id: Annotated[str, typer.Argument(help="Session ID to export")],
    output: Annotated[
        Optional[str],
        typer.Option("--output", "-o", help="Output file path (defaults to stdout)"),
    ] = None,
    format: Annotated[
        str,
        typer.Option("--format", "-f", help="Export format: json, markdown"),
    ] = "json",
) -> None:
    """Export a session to JSON or Markdown."""
    from kalash.runtime.session import SessionManager

    manager = SessionManager()
    data = manager.export_session(session_id, format=format)

    if data is None:
        console.print(f"[red]Session {session_id!r} not found.[/red]")
        raise typer.Exit(code=1)

    if output:
        from pathlib import Path

        Path(output).write_text(data, encoding="utf-8")
        console.print(f"[green]Exported to {output}[/green]")
    else:
        console.print(data)


@session_app.command("rm")
def rm(
    session_id: Annotated[str, typer.Argument(help="Session ID to delete")],
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Skip confirmation")
    ] = False,
) -> None:
    """Delete a session permanently."""
    from kalash.runtime.session import SessionManager

    if not force:
        confirm = typer.confirm(f"Delete session {session_id!r}? This cannot be undone.")
        if not confirm:
            raise typer.Abort()

    manager = SessionManager()
    deleted = manager.delete_session(session_id)

    if deleted:
        console.print(f"[green]Session {session_id!r} deleted.[/green]")
    else:
        console.print(f"[red]Session {session_id!r} not found.[/red]")
        raise typer.Exit(code=1)
