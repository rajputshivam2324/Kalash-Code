"""Project and session status for ``kalash status``."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

status_app = typer.Typer(help="Project and session status", invoke_without_command=True)
console = Console()


@status_app.callback()
def status() -> None:
    """Show current project posture: model, sandbox, provider, session."""
    from kalash.core.config import load_config
    from kalash.models.resolve import active_selection, build_provider
    from kalash.runtime.session import SessionManager

    config = load_config()
    provider_id, model_id = active_selection()
    resolution = build_provider(provider_id, model_id)

    table = Table(title="Kalash Status")
    table.add_column("Setting", style="cyan")
    table.add_column("Value")

    table.add_row("Project", str(Path.cwd()))
    table.add_row("Model (config)", config.model.primary)
    table.add_row(
        "Active provider",
        f"{provider_id or '—'} / {model_id or '—'}",
    )
    table.add_row(
        "Provider status",
        "ready" if resolution.ok else f"unavailable: {resolution.reason}",
    )
    table.add_row("Sandbox", config.permissions.sandbox)
    table.add_row("Approval", config.permissions.approval)
    table.add_row("Network", "enabled" if config.permissions.network else "disabled")
    table.add_row("Memory", "enabled" if config.memory.enabled else "disabled")

    try:
        from kalash.cli.doctor import _check_sandbox

        sb = _check_sandbox()
        table.add_row("Sandbox backend", sb.detail)
    except Exception:
        table.add_row("Sandbox backend", "unknown")

    sessions = SessionManager(config).list_sessions(limit=1)
    if sessions:
        s = sessions[0]
        table.add_row("Latest session", str(s.get("id", ""))[:20])
        table.add_row("Session turns", str(s.get("turn_count", 0)))
    else:
        table.add_row("Latest session", "none")

    console.print(table)
