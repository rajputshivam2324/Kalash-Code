"""Command line entry point."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import typer

from kalash import __version__
from kalash.cli.headless import EXIT_ERROR, EXIT_USAGE, FORMATS, run_headless
from kalash.config import ConfigError, Settings, load_settings, settings_path
from kalash.credentials import (
    CredentialError,
    get_selected_model,
    save_credential,
    set_selected_model,
    stored_providers,
)
from kalash.llm.providers import (
    ProviderError,
    all_specs,
    catalog_for,
    has_credential,
    model_problem,
    parse_model,
)
from kalash.mcp import MCPError
from kalash.paths import kalash_home, project_root
from kalash.session.log import list_sessions

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    invoke_without_command=True,
    help="Kalash: a terminal coding agent.",
)
auth_app = typer.Typer(help="Manage provider API keys.", no_args_is_help=True)
config_app = typer.Typer(help="Show or change settings.", no_args_is_help=True)
mcp_app = typer.Typer(help="Add, list, test and remove MCP servers.", no_args_is_help=True)
app.add_typer(auth_app, name="auth")
app.add_typer(config_app, name="config")
app.add_typer(mcp_app, name="mcp")


def _load(cwd: Path, overrides: dict[str, Any] | None = None) -> Settings:
    try:
        settings, warnings = load_settings(cwd, overrides)
    except ConfigError as error:
        typer.echo(f"kalash: {error}", err=True)
        raise typer.Exit(EXIT_USAGE) from error
    for line in warnings:
        typer.echo(f"kalash: warning: {line}", err=True)
    return settings


def _version(value: bool) -> None:
    if value:
        typer.echo(f"kalash {__version__}")
        raise typer.Exit


@app.callback()
def main_callback(
    ctx: typer.Context,
    prompt: Annotated[
        str | None,
        typer.Option(
            "-p", "--prompt", help="Run one prompt non-interactively ('-' reads it from stdin)."
        ),
    ] = None,
    model: Annotated[str | None, typer.Option("-m", "--model", help="provider/model")] = None,
    output_format: Annotated[
        str, typer.Option("--output-format", help="text | json | stream-json (with -p)")
    ] = "text",
    continue_: Annotated[
        bool,
        typer.Option("-c", "--continue", help="Continue the most recent session in this project."),
    ] = False,
    resume: Annotated[
        str | None,
        typer.Option("-r", "--resume", help="Resume a session by id (see `kalash sessions`)."),
    ] = None,
    sandbox: Annotated[
        str | None,
        typer.Option("--sandbox", help="read-only | workspace-write | danger-full-access"),
    ] = None,
    approval: Annotated[str | None, typer.Option("--approval", help="ask | never")] = None,
    effort: Annotated[
        str | None,
        typer.Option("--effort", help="reasoning effort: a level the model takes, auto or off"),
    ] = None,
    cwd: Annotated[Path, typer.Option("--cwd", help="Working directory.")] = Path("."),
    version: Annotated[bool, typer.Option("--version", callback=_version, is_eager=True)] = False,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    cwd = cwd.expanduser().resolve()
    if output_format not in FORMATS:
        typer.echo(f"kalash: --output-format must be one of {', '.join(FORMATS)}", err=True)
        raise typer.Exit(EXIT_USAGE)
    overrides: dict[str, Any] = {}
    if sandbox or approval:
        overrides["permissions"] = {
            k: v for k, v in {"sandbox": sandbox, "approval": approval}.items() if v
        }
    if model:
        overrides["model"] = model
    if effort:
        overrides["reasoning_effort"] = effort
    settings = _load(cwd, overrides)
    stdin_piped = not sys.stdin.isatty()
    if prompt == "-" or (prompt is None and stdin_piped and not continue_ and not resume):
        prompt = sys.stdin.read()
    if prompt is not None:
        if not prompt.strip():
            typer.echo("kalash: empty prompt", err=True)
            raise typer.Exit(EXIT_USAGE)
        code = asyncio.run(
            run_headless(
                prompt=prompt,
                cwd=cwd,
                settings=settings,
                fmt=output_format,
                model=model,
                session_ref=resume,
                continue_latest=continue_,
                interactive_approval=sys.stdin.isatty() and sys.stderr.isatty(),
            )
        )
        raise typer.Exit(code)
    if output_format != "text":
        typer.echo("kalash: --output-format applies only with -p", err=True)
        raise typer.Exit(EXIT_USAGE)
    from kalash.deps import ui_dependency_problem

    problem = ui_dependency_problem()
    if problem:
        typer.echo(f"kalash: {problem}", err=True)
        raise typer.Exit(EXIT_USAGE)
    from kalash.tui.app import run_tui

    raise typer.Exit(
        run_tui(
            cwd=cwd, settings=settings, model=model, session_ref=resume, continue_latest=continue_
        )
    )


# ----------------------------------------------------------------------------- auth / model


@auth_app.command("login")
def auth_login(
    provider: Annotated[str, typer.Argument(help="Provider id, e.g. anthropic, openai, sarvam")],
    key: Annotated[str | None, typer.Option("--key", help="API key (prompted if omitted)")] = None,
    base_url: Annotated[str | None, typer.Option("--base-url", help="Override endpoint")] = None,
) -> None:
    """Store an API key (encrypted) for a provider (same as `kalash provider ID`)."""
    _store_key(provider, key, base_url)


@app.command("provider")
def provider_cmd(
    provider: Annotated[str | None, typer.Argument(help="Provider id to set the key for")] = None,
    key: Annotated[str | None, typer.Option("--key", help="API key (prompted if omitted)")] = None,
    base_url: Annotated[str | None, typer.Option("--base-url", help="Override endpoint")] = None,
    remove: Annotated[bool, typer.Option("--remove", help="Delete the stored key")] = False,
) -> None:
    """List providers and where their keys come from, or set / remove one provider's key."""
    if provider is None:
        auth_list()
        return
    if remove:
        auth_logout(provider)
        return
    _store_key(provider, key, base_url)


def _store_key(provider: str, key: str | None, base_url: str | None) -> None:
    specs = all_specs(_load(Path.cwd()).providers)
    if provider not in specs:
        typer.echo(
            f"kalash: unknown provider {provider!r}. Known: {', '.join(sorted(specs))}", err=True
        )
        raise typer.Exit(EXIT_USAGE)
    secret = key or typer.prompt(f"API key for {specs[provider].name}", hide_input=True)
    try:
        save_credential(provider, secret.strip())
        if base_url:
            save_credential(f"{provider}:base_url", base_url.strip())
    except (CredentialError, OSError) as error:
        typer.echo(f"kalash: {error}", err=True)
        raise typer.Exit(EXIT_ERROR) from error
    typer.echo(f"Saved key for {specs[provider].name} in {kalash_home() / 'auth.json'}")


@auth_app.command("logout")
def auth_logout(provider: Annotated[str, typer.Argument()]) -> None:
    """Remove a stored API key."""
    save_credential(provider, "")
    save_credential(f"{provider}:base_url", "")
    typer.echo(f"Removed stored key for {provider}")


@auth_app.command("list")
def auth_list() -> None:
    """Show which providers have a usable key."""
    specs = all_specs(_load(Path.cwd()).providers)
    stored = set(stored_providers())
    for spec in specs.values():
        env = next((e for e in spec.env_keys if os.environ.get(e)), None)
        source = f"env {env}" if env else ("stored" if spec.id in stored else "-")
        if not spec.requires_key:
            source = "no key needed"
        typer.echo(f"{spec.id:<12} {spec.name:<28} {source}")


@app.command("model")
def model_cmd(
    value: Annotated[str | None, typer.Argument(help="provider/model to select")] = None,
) -> None:
    """Select the default model (see `kalash models` for the full list)."""
    settings = _load(Path.cwd())
    specs = all_specs(settings.providers)
    if value is None:
        current = settings.model or get_selected_model()
        typer.echo(f"current: {current or '(none)'}\nrun `kalash models` to list models")
        return
    try:
        ref = parse_model(value, specs)
    except ProviderError as error:
        typer.echo(f"kalash: {error}", err=True)
        raise typer.Exit(EXIT_USAGE) from error
    problem = model_problem(ref.provider, ref.model)
    if problem and problem[0] == "error":
        typer.echo(f"kalash: {problem[1]}", err=True)
        raise typer.Exit(EXIT_USAGE)
    if problem:
        typer.echo(f"warning: {problem[1]}", err=True)
    set_selected_model(ref.provider, ref.model)
    typer.echo(f"Default model: {ref}")


def _fmt_tokens(value: int) -> str:
    return f"{value / 1_000_000:.3g}M" if value >= 1_000_000 else f"{value // 1000}k"


@app.command("models")
def models_cmd(
    provider: Annotated[str | None, typer.Argument(help="only this provider")] = None,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh",
            help="ask providers for their current "
            "model lists and limits (needs keys; cached for 24 h)",
        ),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List models with context, output limit and price; optionally refresh them live."""
    settings = _load(Path.cwd())
    specs = all_specs(settings.providers)
    if provider is not None and provider not in specs:
        typer.echo(
            f"kalash: unknown provider {provider!r}. Known: {', '.join(sorted(specs))}", err=True
        )
        raise typer.Exit(EXIT_USAGE)
    if refresh:
        from kalash.llm import discovery

        results, errors = asyncio.run(discovery.refresh(specs, only=provider))
        for name, models in sorted(results.items()):
            typer.echo(f"refreshed {name}: {len(models)} models", err=True)
        for name, message in sorted(errors.items()):
            typer.echo(f"could not refresh {name}: {message}", err=True)
    rows: list[dict[str, Any]] = []
    for spec in specs.values():
        if provider is not None and spec.id != provider:
            continue
        ready = has_credential(spec)
        for model_id, info in catalog_for(spec).items():
            rows.append(
                {
                    "model": f"{spec.id}/{model_id}",
                    "ready": ready,
                    "context": info.context,
                    "max_output": info.max_output,
                    "input_per_mtok": info.price.input if info.price else None,
                    "output_per_mtok": info.price.output if info.price else None,
                    "reasoning": list(info.efforts),
                    "note": info.note,
                }
            )
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        typer.echo("No models known for that provider. Try --refresh or configure it in settings.")
        return
    width = max(len(str(r["model"])) for r in rows)
    per_provider: dict[str, int] = {}
    hidden: dict[str, int] = {}
    for r in rows:
        name = str(r["model"]).split("/", 1)[0]
        per_provider[name] = per_provider.get(name, 0) + 1
        if provider is None and per_provider[name] > 12:
            hidden[name] = hidden.get(name, 0) + 1
            continue
        price = (
            f"${r['input_per_mtok']:g}/${r['output_per_mtok']:g}"
            if r["input_per_mtok"] is not None
            else "-"
        )
        flag = "*" if r["ready"] else " "
        typer.echo(
            f"{flag} {r['model']:<{width}}  ctx {_fmt_tokens(r['context']):>5}  "
            f"out {_fmt_tokens(r['max_output']):>5}  {price:<12} {r['note']}"
        )
    for name, count in hidden.items():
        typer.echo(f"  ... {count} more {name} models: kalash models {name}")
    typer.echo("\n* = a key is available.  Prices are USD per million tokens (input/output).")


@app.command("sessions")
def sessions_cmd(limit: Annotated[int, typer.Option("-n")] = 20) -> None:
    """List sessions of the current project."""
    root = project_root(Path.cwd())
    infos = list_sessions(root)[:limit]
    if not infos:
        typer.echo("No sessions in this project yet.")
    for info in infos:
        when = datetime.fromtimestamp(info.updated_at).strftime("%Y-%m-%d %H:%M")
        typer.echo(f"{info.id}  {when}  {info.model:<28} {info.title}")


@app.command("doctor")
def doctor() -> None:
    """Check the local setup."""
    import platform
    import shutil

    from kalash.sandbox import detect

    problems = 0

    def line(ok: bool | None, label: str, detail: str = "") -> None:
        nonlocal problems
        mark = {True: "ok  ", False: "FAIL", None: "warn"}[ok]
        if ok is False:
            problems += 1
        typer.echo(f"[{mark}] {label}{': ' + detail if detail else ''}")

    line(sys.version_info >= (3, 12), "python", platform.python_version())
    status = detect(refresh=True)
    line(status.ok, "sandbox", f"{status.backend} - {status.detail}")
    line(
        True if shutil.which("rg") else None,
        "ripgrep (rg)",
        "found" if shutil.which("rg") else "missing; grep falls back to a slower built-in",
    )
    line(bool(shutil.which("git")), "git")
    from kalash.deps import UI_REQUIREMENTS, installed_version, ui_dependency_problem

    ui_problem = ui_dependency_problem()
    versions = ", ".join(f"{p} {installed_version(p) or 'missing'}" for p in UI_REQUIREMENTS)
    line(ui_problem is None, "terminal UI", versions)
    if ui_problem:
        typer.echo(f"       {ui_problem}")
    try:
        settings, warnings = load_settings(Path.cwd())
        line(True, "settings", "loaded")
        for warning in warnings:
            line(None, "settings", warning)
    except ConfigError as error:
        line(False, "settings", str(error))
        settings = Settings()
    specs = all_specs(settings.providers)
    ready = [s.id for s in specs.values() if s.requires_key and has_credential(s)]
    line(bool(ready), "credentials", ", ".join(ready) or "none; run `kalash provider <id>`")
    selected = settings.model or get_selected_model()
    if selected:
        try:
            ref = parse_model(selected, specs)
            line(has_credential(specs[ref.provider]), "model", str(ref))
        except ProviderError as error:
            line(False, "model", str(error))
    else:
        line(None, "model", "none selected; run `kalash model`")
    raise typer.Exit(EXIT_ERROR if problems else 0)


# ----------------------------------------------------------------------------- config


# ----------------------------------------------------------------------------- mcp

MCP_NAME = r"^[A-Za-z0-9_-]{1,32}$"


def _mcp_scope(scope: str) -> Path:
    if scope not in ("user", "local"):
        typer.echo(
            "kalash: --scope must be user or local (a committed project file cannot hold MCP "
            "servers: they run code or reach the network with your privileges)",
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)
    return settings_path(scope, Path.cwd())  # type: ignore[arg-type]


def _kv(pairs: list[str], what: str, separator: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for pair in pairs:
        key, found, value = pair.partition(separator)
        if not found or not key.strip():
            typer.echo(
                f"kalash: {what} must look like NAME{separator}VALUE, got {pair!r}", err=True
            )
            raise typer.Exit(EXIT_USAGE)
        parsed[key.strip()] = value.strip()
    return parsed


@mcp_app.command("add")
def mcp_add(
    name: Annotated[str, typer.Argument(help="A short name; tools appear as mcp__NAME__tool")],
    target: Annotated[str, typer.Argument(help="https://... URL of a remote server, or a command")],
    args: Annotated[
        list[str] | None, typer.Argument(help="Arguments for a command (put them after --)")
    ] = None,
    header: Annotated[
        list[str] | None,
        typer.Option(
            "--header", "-H", help='Remote only: "Name: value"; ${VAR} reads the environment'
        ),
    ] = None,
    env: Annotated[
        list[str] | None, typer.Option("--env", "-e", help="Command only: NAME=value")
    ] = None,
    trust: Annotated[bool, typer.Option("--trust", help="Run its tools without asking")] = False,
    timeout: Annotated[float, typer.Option("--timeout", help="Seconds per request")] = 60.0,
    scope: Annotated[str, typer.Option("--scope", help="user | local")] = "user",
) -> None:
    """Add a server, then check it with `kalash mcp test NAME`.

    \b
    kalash mcp add deepwiki https://mcp.deepwiki.com/mcp
    kalash mcp add time -- uvx mcp-server-time
    kalash mcp add memory -e MEMORY_FILE_PATH=/tmp/m.json -- npx -y server-memory-package
    """
    import re

    from pydantic import ValidationError

    from kalash.config import McpServer, update_setting

    if not re.match(MCP_NAME, name):
        typer.echo("kalash: the name may use letters, digits, - and _ (max 32)", err=True)
        raise typer.Exit(EXIT_USAGE)
    path = _mcp_scope(scope)
    remote = target.startswith(("http://", "https://"))
    try:
        server = McpServer(
            url=target if remote else None,
            command=None if remote else target,
            args=[] if remote else list(args or []),
            headers=_kv(header or [], "--header", ":"),
            env=_kv(env or [], "--env", "="),
            trust=trust,
            timeout=timeout,
        )
    except ValidationError as error:
        typer.echo(f"kalash: {error.errors()[0]['msg'].removeprefix('Value error, ')}", err=True)
        raise typer.Exit(EXIT_USAGE) from error
    if remote and args:
        typer.echo("kalash: a URL takes no extra arguments", err=True)
        raise typer.Exit(EXIT_USAGE)
    try:
        update_setting(path, f"mcp_servers.{name}", server.model_dump(exclude_defaults=True))
    except ConfigError as error:
        typer.echo(f"kalash: {error}", err=True)
        raise typer.Exit(EXIT_ERROR) from error
    typer.echo(f"Added MCP server {name!r} ({server.target}) to {path}")
    typer.echo(f"Check it with:  kalash mcp test {name}")


@mcp_app.command("list")
def mcp_list() -> None:
    """Show the configured MCP servers."""
    servers = _load(Path.cwd()).mcp_servers
    if not servers:
        typer.echo(
            "No MCP servers configured. Try: kalash mcp add deepwiki https://mcp.deepwiki.com/mcp"
        )
        return
    for name, server in servers.items():
        kind = "remote" if server.url else "local "
        trust = "trusted" if server.trust else "asks first"
        typer.echo(f"{name:<14} {kind}  {trust:<10}  {server.target}")


@mcp_app.command("remove")
def mcp_remove(
    name: Annotated[str, typer.Argument()],
    scope: Annotated[str, typer.Option("--scope", help="user | local")] = "user",
) -> None:
    """Remove a configured MCP server."""
    from kalash.config import remove_setting

    path = _mcp_scope(scope)
    if not remove_setting(path, f"mcp_servers.{name}"):
        typer.echo(f"kalash: no MCP server {name!r} in {path}", err=True)
        raise typer.Exit(EXIT_USAGE)
    typer.echo(f"Removed MCP server {name!r} from {path}")


@mcp_app.command("trust")
def mcp_trust(
    name: Annotated[str, typer.Argument()],
    off: Annotated[bool, typer.Option("--off", help="Ask before its tools run again")] = False,
) -> None:
    """Let a server's tools run without asking (or, with --off, ask again).

    Needed for Auto mode and headless runs, where nothing can prompt: an untrusted server's tools
    are refused there. Only trust servers you would run yourself.
    """
    from kalash.config import update_setting

    for scope in ("local", "user"):
        path = settings_path(scope, Path.cwd())
        try:
            known = (
                json.loads(path.read_text("utf-8")).get("mcp_servers", {}) if path.exists() else {}
            )
        except (OSError, ValueError):
            known = {}
        if name in known:
            update_setting(path, f"mcp_servers.{name}.trust", not off)
            typer.echo(f"MCP server {name!r} is now {'untrusted' if off else 'trusted'} ({path})")
            return
    typer.echo(f"kalash: no MCP server {name!r} in your user or local settings", err=True)
    raise typer.Exit(EXIT_USAGE)


@mcp_app.command("test")
def mcp_test(
    names: Annotated[
        list[str] | None, typer.Argument(help="Configured names, or a URL to try without saving")
    ] = None,
    timeout: Annotated[float, typer.Option("--timeout")] = 30.0,
) -> None:
    """Connect and list the tools - the quickest way to see whether a server works."""
    import time

    from kalash.config import McpServer
    from kalash.mcp import make_client

    configured = _load(Path.cwd()).mcp_servers
    wanted: dict[str, McpServer] = {}
    for item in names or list(configured):
        if item.startswith(("http://", "https://")):
            try:
                host = urlsplit(item).hostname or item
                wanted[host] = McpServer(url=item, timeout=timeout)
            except ValueError as error:
                typer.echo(f"kalash: {error}", err=True)
                raise typer.Exit(EXIT_USAGE) from error
        elif item in configured:
            wanted[item] = configured[item]
        else:
            typer.echo(f"kalash: no MCP server named {item!r} (see `kalash mcp list`)", err=True)
            raise typer.Exit(EXIT_USAGE)
    if not wanted:
        typer.echo("No MCP servers configured. Try: kalash mcp test https://mcp.deepwiki.com/mcp")
        return

    async def probe(name: str, server: McpServer) -> bool:
        client = make_client(name, server)
        started = time.monotonic()
        try:
            await client.start()
            tools = await client.list_tools()
            era, version = getattr(client, "era", None), getattr(client, "version", "")
        except (MCPError, TimeoutError, OSError) as error:
            reason = str(error).removeprefix(f"{name}: ") or "timed out"
            typer.echo(f"[FAIL] {name}: {reason}")
            return False
        finally:
            await client.close()
        took = time.monotonic() - started
        how = f"remote, {era} protocol {version}" if era else "local process"
        info = client.server_info
        label = f"{info.get('name', '?')} {info.get('version', '')}".strip()
        typer.echo(f"[ok  ] {name}: {label} ({how}) - {len(tools)} tools in {took:.1f}s")
        for tool in tools:
            first = str(tool.get("description") or "").strip().splitlines()[:1]
            typer.echo(f"         {tool['name']:<32} {(first[0] if first else '')[:70]}")
        for warning in getattr(client, "warnings", []):
            typer.echo(f"[warn] {warning}")
        return True

    async def run() -> list[bool]:
        return [await probe(name, server) for name, server in wanted.items()]

    raise typer.Exit(0 if all(asyncio.run(run())) else EXIT_ERROR)


@config_app.command("show")
def config_show() -> None:
    """Print the effective settings."""
    settings = _load(Path.cwd())
    typer.echo(json.dumps(settings.model_dump(mode="json"), indent=2))


@config_app.command("set")
def config_set(
    key: Annotated[str, typer.Argument(help="Dotted key, e.g. permissions.approval")],
    value: Annotated[str, typer.Argument(help="JSON value, or plain text")],
    scope: Annotated[str, typer.Option("--scope", help="user | project | local")] = "user",
) -> None:
    """Set one key in a settings file (validated before it is written)."""
    from kalash.config import PROJECT_SAFE_KEYS
    from kalash.secure_io import atomic_write

    if scope not in ("user", "project", "local"):
        typer.echo("kalash: --scope must be user, project or local", err=True)
        raise typer.Exit(EXIT_USAGE)
    top = key.split(".")[0]
    if scope == "project" and top not in PROJECT_SAFE_KEYS:
        typer.echo(
            f"kalash: {top!r} is ignored in committed project settings; use --scope local or user",
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)
    try:
        parsed: Any = json.loads(value)
    except ValueError:
        parsed = value
    path = settings_path(scope, Path.cwd())  # type: ignore[arg-type]
    try:
        data = json.loads(path.read_text("utf-8")) if path.exists() else {}
    except (OSError, ValueError) as error:
        typer.echo(f"kalash: cannot read {path}: {error}", err=True)
        raise typer.Exit(EXIT_ERROR) from error
    node = data
    parts = key.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = parsed
    previous = path.read_bytes() if path.exists() else None
    atomic_write(path, (json.dumps(data, indent=2) + "\n").encode())
    try:
        load_settings(Path.cwd())
    except ConfigError as error:
        if previous is None:
            path.unlink()
        else:
            atomic_write(path, previous)
        typer.echo(f"kalash: not saved - {error}", err=True)
        raise typer.Exit(EXIT_USAGE) from error
    typer.echo(f"Set {key} in {path}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
