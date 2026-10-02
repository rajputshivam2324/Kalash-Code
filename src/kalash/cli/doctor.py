"""Environment diagnostic command."""

from __future__ import annotations

import platform
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


def doctor() -> None:
    """Run environment health checks and report status.

    Exit codes:
        0 = healthy (all checks pass)
        1 = degraded (some checks have warnings)
        2 = unhealthy (critical checks fail)
    """
    import typer

    console.print(Panel("[bold]Kalash Doctor[/bold] — Environment Diagnostic", style="cyan"))

    checks: list[_CheckResult] = []

    # Platform info
    checks.append(_check_platform())

    # Sandbox backend
    checks.append(_check_sandbox())

    checks.append(_check_provider())
    checks.append(_check_tools())
    checks.append(_check_permissions())
    checks.append(_check_database())
    checks.append(_check_scratchpad())
    checks.append(_check_search())
    checks.append(_check_ripgrep())
    checks.append(_check_hooks())
    checks.append(_check_skills())
    checks.append(_check_instructions())
    checks.append(_check_disk())
    checks.append(_check_config())

    # Report results
    table = Table(title="Diagnostic Results")
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Details")

    critical_fail = False
    has_warning = False

    for check in checks:
        if check.status == "pass":
            icon = "[green]✓ pass[/green]"
        elif check.status == "warn":
            icon = "[yellow]⚠ warn[/yellow]"
            has_warning = True
        else:
            icon = "[red]✗ fail[/red]"
            critical_fail = True

        table.add_row(check.name, icon, check.detail)

    console.print(table)

    # Summary
    if critical_fail:
        console.print("\n[red bold]Status: UNHEALTHY[/red bold] — critical issues detected")
        raise typer.Exit(code=2)
    if has_warning:
        console.print("\n[yellow bold]Status: DEGRADED[/yellow bold] — non-critical warnings")
        raise typer.Exit(code=1)
    console.print("\n[green bold]Status: HEALTHY[/green bold] — all checks passed")
    raise typer.Exit(code=0)


# ---------------------------------------------------------------------------
# Internal check helpers
# ---------------------------------------------------------------------------


class _CheckResult:
    """Result of a single diagnostic check."""

    __slots__ = ("name", "status", "detail")

    def __init__(self, name: str, status: str, detail: str) -> None:
        self.name = name
        self.status = status  # "pass", "warn", "fail"
        self.detail = detail


def _check_platform() -> _CheckResult:
    """Report platform information."""
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    os_info = f"{platform.system()} {platform.release()}"
    detail = f"Python {py_version} on {os_info}"

    return _CheckResult("Platform", "pass", detail)


def _check_sandbox() -> _CheckResult:
    """Report which OS sandbox backend is usable."""
    try:
        from kalash.sandbox.manager import get_sandbox_manager

        status = get_sandbox_manager().status()
    except Exception as exc:
        return _CheckResult("Sandbox", "fail", f"Error: {exc}")

    if status.available:
        return _CheckResult("Sandbox", "pass", status.describe())
    # Not fatal: the permission gate and path checks still apply. This reduces
    # defence in depth rather than removing enforcement.
    return _CheckResult("Sandbox", "warn", status.describe())


def _check_provider() -> _CheckResult:
    """Check that a model provider resolves."""
    try:
        from kalash.models.resolve import build_provider

        resolution = build_provider()
    except Exception as exc:
        return _CheckResult("Provider", "fail", f"Error: {exc}")

    if not resolution.ok:
        return _CheckResult("Provider", "fail", resolution.reason or "no provider configured")

    name = getattr(resolution.provider, "name", "unknown")
    return _CheckResult("Provider", "pass", f"resolved: {name}")


def _check_tools() -> _CheckResult:
    """Check the built-in tool registry populates and produces schemas."""
    try:
        from kalash.tools.builtins import default_registry

        registry = default_registry()
        schemas = registry.list_schemas()
    except Exception as exc:
        return _CheckResult("Tools", "fail", f"Error: {exc}")

    if not schemas:
        return _CheckResult("Tools", "fail", "no tools registered")
    names = ", ".join(sorted(t.name for t in registry.list_tools())[:6])
    return _CheckResult("Tools", "pass", f"{len(schemas)} registered ({names}, …)")


def _check_permissions() -> _CheckResult:
    """Check the permission gate is constructible and an approval path exists."""
    try:
        from kalash.core.config import load_config
        from kalash.permissions.console import is_interactive
        from kalash.runtime.toolhost import normalize_sandbox_mode

        mode = normalize_sandbox_mode(load_config().permissions.sandbox)
        channel = "terminal prompt" if is_interactive() else "non-interactive (denies)"
    except Exception as exc:
        return _CheckResult("Permissions", "fail", f"Error: {exc}")

    return _CheckResult("Permissions", "pass", f"sandbox={mode}, approvals={channel}")


def _check_database() -> _CheckResult:
    """Check the SQLite store opens and has been migrated."""
    try:
        from kalash.core.paths import kalash_db_path
        from kalash.storage.engine import get_engine

        engine = get_engine()
        rows = engine.execute_read(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
        )
    except Exception as exc:
        return _CheckResult("Database", "fail", f"Error: {exc}")

    location = kalash_db_path()
    if not rows:
        return _CheckResult(
            "Database", "warn", f"{location} — not migrated yet (runs on first session)"
        )
    return _CheckResult("Database", "pass", f"{location} — schema present")


def _check_scratchpad() -> _CheckResult:
    """Check the blob store and scratchpad directory are writable."""
    try:
        from kalash.storage.blobs import read_blob, store_blob

        digest = store_blob(b"kalash doctor probe")
        recovered = read_blob(digest)
    except Exception as exc:
        return _CheckResult("Scratchpad", "fail", f"Error: {exc}")

    if recovered != b"kalash doctor probe":
        return _CheckResult("Scratchpad", "fail", "blob round trip mismatch")
    return _CheckResult("Scratchpad", "pass", "blob store round trip verified")


def _check_search() -> _CheckResult:
    """Check whether a web search provider is configured."""
    try:
        from kalash.tools.search_providers import available_providers, configuration_hint

        configured = available_providers()
    except Exception as exc:
        return _CheckResult("Web search", "fail", f"Error: {exc}")

    if configured:
        return _CheckResult("Web search", "pass", f"configured: {', '.join(configured)}")
    return _CheckResult("Web search", "warn", configuration_hint())


def _check_ripgrep() -> _CheckResult:
    """The search tool shells out to ripgrep."""
    import shutil

    if shutil.which("rg"):
        return _CheckResult("ripgrep", "pass", "rg found on PATH")
    return _CheckResult("ripgrep", "warn", "rg not on PATH — the `search` tool will be unavailable")


def _check_hooks() -> _CheckResult:
    """Report discovered project hooks."""
    try:
        from kalash.hooks.load import discover_hooks

        hooks = discover_hooks()
    except Exception as exc:
        return _CheckResult("Hooks", "fail", f"Error: {exc}")

    if not hooks:
        return _CheckResult("Hooks", "pass", "none configured")
    triggers = ", ".join(sorted({h.event.value for h in hooks}))
    return _CheckResult("Hooks", "pass", f"{len(hooks)} hook(s): {triggers}")


def _check_skills() -> _CheckResult:
    """Report discovered skills."""
    try:
        from kalash.skills.loader import SkillLoader

        entries = SkillLoader().discover_now()
    except Exception as exc:
        return _CheckResult("Skills", "fail", f"Error: {exc}")

    if not entries:
        return _CheckResult("Skills", "pass", "none installed")
    return _CheckResult("Skills", "pass", f"{len(entries)} installed")


def _check_instructions() -> _CheckResult:
    """Report discovered KALASH.md / AGENTS.md files."""
    try:
        from kalash.runtime.prompt import discover_project_instructions

        found = discover_project_instructions()
    except Exception as exc:
        return _CheckResult("Instructions", "fail", f"Error: {exc}")

    if not found.sources:
        return _CheckResult("Instructions", "pass", "no KALASH.md found (run /init to create one)")
    names = ", ".join(str(p) for p in found.sources)
    return _CheckResult("Instructions", "pass", names)


def _check_disk() -> _CheckResult:
    """Check available disk space where Kalash stores data."""
    import shutil

    try:
        from kalash.core.paths import kalash_home

        usage = shutil.disk_usage(kalash_home())
    except Exception as exc:
        return _CheckResult("Disk", "warn", f"Could not determine: {exc}")

    free_gb = usage.free / (1024**3)
    detail = f"{free_gb:.1f} GiB free"
    if free_gb < 1:
        return _CheckResult("Disk", "fail", f"{detail} — too little to operate safely")
    if free_gb < 5:
        return _CheckResult("Disk", "warn", detail)
    return _CheckResult("Disk", "pass", detail)


def _check_config() -> _CheckResult:
    """Check configuration loads."""
    try:
        from kalash.core.config import load_config

        config = load_config()
    except Exception as exc:
        return _CheckResult("Config", "fail", f"Could not load: {exc}")

    return _CheckResult(
        "Config",
        "pass",
        f"model={config.model.primary}, sandbox={config.permissions.sandbox}",
    )
