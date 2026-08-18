"""Model provider CLI commands."""

from __future__ import annotations

from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

provider_app = typer.Typer(help="Model provider management")
console = Console()


@provider_app.command("ls")
def ls(
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show model lists")
    ] = False,
) -> None:
    """List configured model providers and credential status."""
    import os

    from kalash.models.resolve import active_selection, credential_for
    from kalash.tui.providers import PROVIDERS

    active_provider, active_model = active_selection()

    table = Table(title="Model Providers")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name")
    table.add_column("Key", justify="center")
    table.add_column("Active", justify="center")
    if verbose:
        table.add_column("Models", style="dim")

    for info in PROVIDERS:
        has_key = bool(credential_for(info.id)) or (
            not info.requires_key
        ) or bool(os.environ.get(info.env_key))
        key_status = "[green]✓[/green]" if has_key else "[red]✗[/red]"
        is_active = info.id == active_provider
        active = "[green]●[/green]" if is_active else ""
        row = [info.id, info.name, key_status, active]
        if verbose:
            models = ", ".join(info.models[:5])
            if len(info.models) > 5:
                models += f" (+{len(info.models) - 5})"
            row.append(models or "(dynamic)")
        table.add_row(*row)

    console.print(table)
    if active_provider:
        console.print(f"\nActive: [cyan]{active_provider}[/cyan] / {active_model or 'default'}")


@provider_app.command("test")
def test(
    provider_id: Annotated[
        Optional[str],
        typer.Argument(help="Provider to test (defaults to active)"),
    ] = None,
) -> None:
    """Verify provider credentials and model resolution."""
    from kalash.models.resolve import active_selection, build_provider

    pid, mid = active_selection()
    target = provider_id or pid
    if not target:
        console.print("[red]No provider configured. Run /connect in the TUI or set an API key.[/red]")
        raise typer.Exit(code=1)

    resolution = build_provider(target, mid if provider_id is None else None)
    if not resolution.ok:
        console.print(f"[red]✗ {resolution.reason}[/red]")
        raise typer.Exit(code=1)

    console.print(
        f"[green]✓ Provider resolved:[/green] {resolution.provider_id} / {resolution.model_id}"
    )


@provider_app.command("bench")
def bench(
    provider_id: Annotated[
        Optional[str],
        typer.Argument(help="Provider to benchmark"),
    ] = None,
) -> None:
    """Quick latency check (minimal token request)."""
    import asyncio
    import time

    from kalash.models.gateway import ModelGateway
    from kalash.models.normalize import Message, Role, TextBlock
    from kalash.models.resolve import build_provider

    resolution = build_provider(provider_id)
    if not resolution.ok or resolution.provider is None:
        console.print(f"[red]{resolution.reason or 'provider unavailable'}[/red]")
        raise typer.Exit(code=1)

    gateway = ModelGateway(primary=resolution.provider)
    message = Message(role=Role.USER, content=[TextBlock(text="Reply with exactly: ok")])

    async def ping() -> float:
        started = time.perf_counter()
        async for _event in gateway.stream([message], max_output_tokens=8):
            pass
        return (time.perf_counter() - started) * 1000

    try:
        latency = asyncio.run(ping())
    except Exception as exc:
        console.print(f"[red]Benchmark failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[green]✓[/green] {resolution.provider_id}/{resolution.model_id} — {latency:.0f}ms"
    )
