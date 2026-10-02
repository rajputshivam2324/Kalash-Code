"""Cron/scheduler management CLI commands."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

cron_app = typer.Typer(help="Manage scheduled tasks")
console = Console()


@cron_app.command("add")
def add(
    name: Annotated[str, typer.Argument(help="Schedule name")],
    schedule: Annotated[str, typer.Argument(help="Cron expression (e.g. '0 9 * * *')")],
    prompt: Annotated[str, typer.Option("--prompt", "-p", help="Prompt to execute")],
    model: Annotated[
        str | None,
        typer.Option("--model", "-m", help="Model to use (defaults to config)"),
    ] = None,
    max_turns: Annotated[
        int,
        typer.Option("--max-turns", help="Max turns per execution"),
    ] = 10,
) -> None:
    """Create a new scheduled task."""
    from kalash.scheduler.manager import SchedulerManager

    manager = SchedulerManager()
    job = manager.add(name=name, schedule=schedule, prompt=prompt, model=model, max_turns=max_turns)
    console.print(f"[green]Schedule created:[/green] {job.id[:12]} ({name})")
    console.print(f"  Next run: {job.next_run}")


@cron_app.command("ls")
def ls(
    all_: Annotated[bool, typer.Option("--all", "-a", help="Include disabled schedules")] = False,
) -> None:
    """List all scheduled tasks."""
    from kalash.scheduler.manager import SchedulerManager

    manager = SchedulerManager()
    jobs = manager.list_jobs(include_disabled=all_)

    table = Table(title="Scheduled Tasks")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name", style="bold")
    table.add_column("Schedule")
    table.add_column("Enabled", justify="center")
    table.add_column("Next Run", style="green")
    table.add_column("Last Run", style="dim")

    for job in jobs:
        enabled = "[green]✓[/green]" if job.enabled else "[red]✗[/red]"
        table.add_row(
            job.id[:12],
            job.name,
            job.schedule,
            enabled,
            job.next_run.strftime("%Y-%m-%d %H:%M") if job.next_run else "—",
            job.last_run.strftime("%Y-%m-%d %H:%M") if job.last_run else "never",
        )

    console.print(table)


@cron_app.command("rm")
def rm(
    job_id: Annotated[str, typer.Argument(help="Schedule ID to delete")],
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip confirmation")] = False,
) -> None:
    """Delete a scheduled task."""
    from kalash.scheduler.manager import SchedulerManager

    if not force:
        confirm = typer.confirm(f"Delete schedule {job_id!r}?")
        if not confirm:
            raise typer.Abort()

    manager = SchedulerManager()
    deleted = manager.delete(job_id)

    if deleted:
        console.print(f"[green]Schedule {job_id!r} deleted.[/green]")
    else:
        console.print(f"[red]Schedule {job_id!r} not found.[/red]")
        raise typer.Exit(code=1)


@cron_app.command("enable")
def enable(
    job_id: Annotated[str, typer.Argument(help="Schedule ID to enable")],
) -> None:
    """Enable a scheduled task."""
    from kalash.scheduler.manager import SchedulerManager

    manager = SchedulerManager()
    success = manager.set_enabled(job_id, enabled=True)

    if success:
        console.print(f"[green]Schedule {job_id!r} enabled.[/green]")
    else:
        console.print(f"[red]Schedule {job_id!r} not found.[/red]")
        raise typer.Exit(code=1)


@cron_app.command("disable")
def disable(
    job_id: Annotated[str, typer.Argument(help="Schedule ID to disable")],
) -> None:
    """Disable a scheduled task."""
    from kalash.scheduler.manager import SchedulerManager

    manager = SchedulerManager()
    success = manager.set_enabled(job_id, enabled=False)

    if success:
        console.print(f"[green]Schedule {job_id!r} disabled.[/green]")
    else:
        console.print(f"[red]Schedule {job_id!r} not found.[/red]")
        raise typer.Exit(code=1)


@cron_app.command("run")
def run(
    job_id: Annotated[str, typer.Argument(help="Schedule ID to trigger manually")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would run without executing")
    ] = False,
) -> None:
    """Manually trigger a scheduled task."""
    from kalash.scheduler.manager import SchedulerManager

    manager = SchedulerManager()

    if dry_run:
        job = manager.get(job_id)
        if job is None:
            console.print(f"[red]Schedule {job_id!r} not found.[/red]")
            raise typer.Exit(code=1)
        console.print(f"[yellow]Dry run:[/yellow] Would execute: {job.prompt[:100]}")
        return

    result = manager.trigger(job_id)

    if result is None:
        console.print(f"[red]Schedule {job_id!r} not found.[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]Triggered {job_id!r}[/green] — Session: {result.session_id[:12]}")


@cron_app.command("logs")
def logs(
    job_id: Annotated[
        str | None,
        typer.Argument(help="Schedule ID (omit for all)"),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max log entries")] = 20,
) -> None:
    """Show execution history for scheduled tasks."""
    from kalash.scheduler.manager import SchedulerManager

    manager = SchedulerManager()
    entries = manager.get_logs(job_id=job_id, limit=limit)

    table = Table(title="Execution History")
    table.add_column("Timestamp", style="green")
    table.add_column("Job", style="cyan")
    table.add_column("Status")
    table.add_column("Duration", justify="right")
    table.add_column("Session", style="dim")

    for entry in entries:
        status_display = "[green]✓ success[/green]" if entry.success else "[red]✗ failed[/red]"
        table.add_row(
            entry.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            entry.job_name,
            status_display,
            f"{entry.duration_seconds:.1f}s",
            entry.session_id[:12] if entry.session_id else "—",
        )

    console.print(table)
