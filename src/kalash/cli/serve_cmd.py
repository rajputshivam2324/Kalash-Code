"""``kalash serve`` — scheduler daemon and optional HTTP API."""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console

serve_app = typer.Typer(help="Run Kalash background services")
console = Console()


@serve_app.callback(invoke_without_command=True)
def serve(
    ctx: typer.Context,
    daemon: bool = typer.Option(
        True,
        "--daemon/--no-daemon",
        help="Run the schedule daemon (default).",
    ),
    tick: float = typer.Option(15.0, "--tick", help="Scheduler poll interval in seconds."),
) -> None:
    """Start background services.

    By default this runs the scheduler daemon that fires due cron jobs as
    headless agent sessions.
    """
    if ctx.invoked_subcommand is not None:
        return

    if not daemon:
        console.print("[yellow]Nothing to do — use --daemon or a subcommand.[/yellow]")
        raise typer.Exit(code=0)

    console.print("[bold]Starting Kalash scheduler daemon…[/bold]")
    console.print(f"  tick interval: {tick}s")
    console.print("  Press Ctrl+C to stop.")

    try:
        asyncio.run(_run_daemon(tick_interval=tick))
    except KeyboardInterrupt:
        console.print("\n[dim]Scheduler stopped.[/dim]")


async def _run_daemon(*, tick_interval: float) -> None:
    from kalash.scheduler.daemon import SchedulerDaemon
    from kalash.storage.engine import get_engine

    daemon = SchedulerDaemon(get_engine(), tick_interval_s=tick_interval)
    await daemon.start()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await daemon.stop()
