"""Session management CLI commands."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

session_app = typer.Typer(help="Manage sessions")
console = Console()


@session_app.command("ls")
def ls(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max sessions to show")] = 20,
    status: Annotated[
        str | None,
        typer.Option("--status", "-s", help="Filter by status (active, completed, abandoned)"),
    ] = None,
) -> None:
    """List recent sessions."""
    from kalash.runtime.session import SessionManager

    manager = SessionManager()
    sessions = manager.list_sessions(limit=limit, status=status)

    if not sessions:
        console.print("[dim]No sessions yet.[/dim]")
        return

    table = Table(title="Sessions")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Updated", style="green")
    table.add_column("State", style="yellow")
    table.add_column("Turns", justify="right")
    table.add_column("Project")

    for s in sessions:
        table.add_row(
            str(s.get("id", ""))[:20],
            str(s.get("updated_at", ""))[:16],
            str(s.get("state", "")),
            str(s.get("turn_count", 0)),
            str(s.get("project_dir", "")) or "—",
        )

    console.print(table)
    console.print("[dim]Resume with: kalash --resume <id>[/dim]")


@session_app.command("show")
def show(
    session_id: Annotated[str, typer.Argument(help="Session ID to inspect")],
    turns: Annotated[
        int | None,
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

    console.print(f"[bold]Session:[/bold] {session.get('id')}")
    console.print(f"[bold]Project:[/bold] {session.get('project_dir', '—')}")
    console.print(f"[bold]Created:[/bold] {session.get('created_at', '—')}")
    console.print(f"[bold]Updated:[/bold] {session.get('updated_at', '—')}")
    console.print(f"[bold]State:[/bold]   {session.get('state', '—')}")
    console.print(f"[bold]Turns:[/bold]   {session.get('turn_count', 0)}")
    console.print(f"[bold]Tokens:[/bold]  {int(session.get('total_tokens') or 0):,}")

    if turns:
        from kalash.models.normalize import TextBlock
        from kalash.runtime.serialize import deserialize_blocks

        transcript = manager.get_transcript(session_id)[-turns:]
        console.print(f"\n[bold]Last {len(transcript)} message(s):[/bold]")
        for row in transcript:
            role = str(row.get("role", "?"))
            color = {"user": "blue", "assistant": "green"}.get(role, "yellow")
            blocks = deserialize_blocks(row.get("content"))
            text = (
                " ".join(b.text for b in blocks if isinstance(b, TextBlock)).strip()
                or f"({len(blocks)} non-text block(s))"
            )
            console.print(f"  [{color}]{role}:[/{color}] {text[:160]}")


@session_app.command("export")
def export(
    session_id: Annotated[str, typer.Argument(help="Session ID to export")],
    output: Annotated[
        str | None,
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
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip confirmation")] = False,
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
