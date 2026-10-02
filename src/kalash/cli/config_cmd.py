"""Configuration management CLI commands."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.syntax import Syntax

config_app = typer.Typer(help="Manage configuration")
console = Console()


@config_app.command("get")
def get(
    key: Annotated[str, typer.Argument(help="Config key (dot-separated path)")],
) -> None:
    """Get a configuration value by key."""
    from kalash.config.manager import ConfigManager

    manager = ConfigManager()
    value = manager.get(key)

    if value is None:
        console.print(f"[yellow]Key {key!r} not set.[/yellow]")
        raise typer.Exit(code=1)

    console.print(f"[bold]{key}[/bold] = {value}")


@config_app.command("set")
def set_(
    key: Annotated[str, typer.Argument(help="Config key (dot-separated path)")],
    value: Annotated[str, typer.Argument(help="Value to set")],
    scope: Annotated[
        str,
        typer.Option("--scope", "-s", help="Scope: global, project, session"),
    ] = "project",
) -> None:
    """Set a configuration value."""
    from kalash.config.manager import ConfigManager

    manager = ConfigManager()
    manager.set(key, value, scope=scope)
    console.print(f"[green]Set {key} = {value} (scope: {scope})[/green]")


@config_app.command("edit")
def edit(
    scope: Annotated[
        str,
        typer.Option("--scope", "-s", help="Which config file to edit: global, project"),
    ] = "project",
) -> None:
    """Open configuration file in $EDITOR."""
    import os
    import subprocess

    from kalash.config.manager import ConfigManager

    manager = ConfigManager()
    config_path = manager.get_config_path(scope=scope)

    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))

    try:
        subprocess.run([editor, str(config_path)], check=True)
        console.print(f"[green]Config saved: {config_path}[/green]")
    except FileNotFoundError:
        console.print(f"[red]Editor not found: {editor}[/red]")
        raise typer.Exit(code=1)
    except subprocess.CalledProcessError:
        console.print("[red]Editor exited with error.[/red]")
        raise typer.Exit(code=1)


@config_app.command("show")
def show(
    scope: Annotated[
        str | None,
        typer.Option("--scope", "-s", help="Filter by scope: global, project"),
    ] = None,
    format: Annotated[
        str,
        typer.Option("--format", "-f", help="Output format: toml, json, yaml"),
    ] = "toml",
) -> None:
    """Show the full resolved configuration."""
    from kalash.config.manager import ConfigManager

    manager = ConfigManager()
    rendered = manager.render(scope=scope, format=format)

    lexer_map = {"toml": "toml", "json": "json", "yaml": "yaml"}
    lexer = lexer_map.get(format, "text")

    syntax = Syntax(rendered, lexer, theme="monokai", line_numbers=False)
    console.print(syntax)
