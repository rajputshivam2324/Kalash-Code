"""CLI interface for KVStore."""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.table import Table

from kvstore.store import KVStore

app = typer.Typer(help="KVStore — Embedded key-value CLI manager.")
console = Console()


def _get_store() -> KVStore:
    return KVStore()


@app.command()
def set(
    key: str = typer.Argument(..., help="Key name"),
    value: str = typer.Argument(..., help="Value string or JSON"),
    ttl: float = typer.Option(None, "--ttl", "-t", help="Time-to-live in seconds"),
) -> None:
    try:
        parsed_val = json.loads(value)
    except json.JSONDecodeError:
        parsed_val = value
    store = _get_store()
    store.set(key, parsed_val, ttl_seconds=ttl)
    console.print(f"[green]✓[/green] Stored key '{key}'")


@app.command()
def get(key: str = typer.Argument(..., help="Key to fetch")) -> None:
    store = _get_store()
    val = store.get(key)
    if val is None:
        console.print(f"[red]✗[/red] Key '{key}' not found", err=True)
        raise typer.Exit(1)
    console.print(val)


@app.command()
def delete(key: str = typer.Argument(..., help="Key to delete")) -> None:
    store = _get_store()
    if store.delete(key):
        console.print(f"[green]✓[/green] Deleted key '{key}'")
    else:
        console.print(f"[red]✗[/red] Key '{key}' not found", err=True)
        raise typer.Exit(1)


@app.command("ls")
def list_keys() -> None:
    store = _get_store()
    keys = store.list_keys()
    table = Table(title="KVStore Keys")
    table.add_column("Key", style="bold cyan")
    table.add_column("Value", style="green")
    for k in keys:
        table.add_row(k, str(store.get(k)))
    console.print(table)


@app.command()
def stats() -> None:
    store = _get_store()
    st = store.stats()
    console.print(st)
