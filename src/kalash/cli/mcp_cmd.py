"""MCP (Model Context Protocol) server management CLI commands."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

mcp_app = typer.Typer(help="Manage MCP servers")
console = Console()


@mcp_app.command("trust")
def trust() -> None:
    """Authorize the current project MCP configuration after reviewing it."""
    from pathlib import Path

    from kalash.core.trust import trust_file

    path = Path.cwd() / ".kalash" / "settings" / "mcp.json"
    if not path.is_file():
        console.print("[red]No project MCP configuration found.[/red]")
        raise typer.Exit(code=1)
    digest = trust_file(path)
    console.print(f"Trusted MCP configuration: {digest[:16]}")


@mcp_app.command("add")
def add(
    name: Annotated[str, typer.Argument(help="Server name/alias")],
    url: Annotated[str, typer.Option("--url", "-u", help="Server URL or command")],
    transport: Annotated[
        str,
        typer.Option("--transport", "-t", help="Transport type: stdio, sse, streamable-http"),
    ] = "stdio",
    env: Annotated[
        list[str] | None,
        typer.Option("--env", "-e", help="Environment variables (KEY=VALUE)"),
    ] = None,
    scope: Annotated[
        str,
        typer.Option("--scope", "-s", help="Scope: global, project"),
    ] = "project",
) -> None:
    """Add a new MCP server configuration."""
    from kalash.mcp.registry import MCPRegistry

    env_dict: dict[str, str] = {}
    if env:
        for item in env:
            key, _, value = item.partition("=")
            env_dict[key] = value

    registry = MCPRegistry()
    server = registry.add_server(name=name, url=url, transport=transport, env=env_dict, scope=scope)
    console.print(f"[green]MCP server added:[/green] {server.name}")
    console.print(f"  Transport: {server.transport}")
    console.print(f"  URL: {server.url}")


@mcp_app.command("ls")
def ls(
    scope: Annotated[
        str | None,
        typer.Option("--scope", "-s", help="Filter by scope: global, project"),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show extra details")] = False,
) -> None:
    """List configured MCP servers."""
    from kalash.mcp.registry import MCPRegistry

    registry = MCPRegistry()
    servers = registry.list_servers(scope=scope)

    table = Table(title="MCP Servers")
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Transport", style="yellow")
    table.add_column("Status")
    table.add_column("Scope", style="dim")

    if verbose:
        table.add_column("URL")
        table.add_column("Tools", justify="right")

    for server in servers:
        status = "[green]● healthy[/green]" if server.is_healthy else "[red]● down[/red]"
        row = [server.name, server.transport, status, server.scope]

        if verbose:
            row.extend([server.url, str(server.tool_count)])

        table.add_row(*row)

    console.print(table)


@mcp_app.command("test")
def test(
    name: Annotated[str, typer.Argument(help="Server name to test")],
    timeout: Annotated[int, typer.Option("--timeout", help="Connection timeout in seconds")] = 10,
) -> None:
    """Test connectivity to an MCP server."""
    from kalash.mcp.registry import MCPRegistry

    registry = MCPRegistry()

    console.print(f"Testing connection to [cyan]{name}[/cyan]...")

    result = registry.test_server(name, timeout=timeout)

    if result.success:
        console.print("[green]✓ Connected successfully[/green]")
        console.print(f"  Protocol version: {result.protocol_version}")
        console.print(f"  Tools available: {result.tool_count}")
        console.print(f"  Latency: {result.latency_ms:.0f}ms")
    else:
        console.print(f"[red]✗ Connection failed:[/red] {result.error}")
        raise typer.Exit(code=1)


@mcp_app.command("auth")
def auth(
    name: Annotated[str, typer.Argument(help="Server name to authenticate with")],
    flow: Annotated[
        str,
        typer.Option("--flow", "-f", help="Auth flow: oauth, token, api-key"),
    ] = "oauth",
) -> None:
    """Run authentication flow for an MCP server."""
    from kalash.mcp.registry import MCPRegistry

    registry = MCPRegistry()
    server = registry.get_server(name)

    if server is None:
        console.print(f"[red]Server {name!r} not found.[/red]")
        raise typer.Exit(code=1)

    console.print(f"Starting {flow} authentication for [cyan]{name}[/cyan]...")

    if flow == "oauth":
        result = registry.oauth_flow(name)
        if result.success:
            console.print("[green]✓ Authentication successful[/green]")
            console.print("  Token stored securely.")
        else:
            console.print(f"[red]✗ Authentication failed:[/red] {result.error}")
            raise typer.Exit(code=1)
    elif flow == "token":
        token = typer.prompt("Enter token", hide_input=True)
        registry.store_token(name, token)
        console.print("[green]✓ Token stored[/green]")
    elif flow == "api-key":
        key = typer.prompt("Enter API key", hide_input=True)
        registry.store_api_key(name, key)
        console.print("[green]✓ API key stored[/green]")
    else:
        console.print(f"[red]Unknown auth flow: {flow!r}[/red]")
        raise typer.Exit(code=1)
