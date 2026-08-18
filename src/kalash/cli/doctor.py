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

    # Provider health
    checks.extend(_check_providers())

    # Model registry
    checks.append(_check_model_registry())

    # Memory subsystem
    checks.append(_check_memory())

    # MCP servers
    checks.append(_check_mcp())

    # Database
    checks.append(_check_database())

    # Disk space
    checks.append(_check_disk())

    # Config
    checks.append(_check_config())

    # Secrets
    checks.append(_check_secrets())

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
    elif has_warning:
        console.print("\n[yellow bold]Status: DEGRADED[/yellow bold] — non-critical warnings")
        raise typer.Exit(code=1)
    else:
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

    if sys.version_info < (3, 12):
        return _CheckResult("Platform", "warn", f"{detail} (Python 3.12+ recommended)")
    return _CheckResult("Platform", "pass", detail)


def _check_sandbox() -> _CheckResult:
    """Check sandbox backend availability."""
    try:
        from kalash.sandbox.manager import SandboxManager

        manager = SandboxManager()
        info = manager.get_info()
        detail = (
            f"Mode: {info.mode}, Backend: {info.backend}, "
            f"Enforcement: {info.enforcement_level}"
        )
        return _CheckResult("Sandbox", "pass", detail)
    except ImportError:
        return _CheckResult("Sandbox", "warn", "Sandbox module not available")
    except Exception as e:
        return _CheckResult("Sandbox", "fail", f"Error: {e}")


def _check_providers() -> list[_CheckResult]:
    """Check all configured model providers."""
    results: list[_CheckResult] = []
    try:
        from kalash.models.registry import ModelRegistry

        registry = ModelRegistry()
        providers = registry.list_providers()

        if not providers:
            results.append(_CheckResult("Providers", "warn", "No providers configured"))
            return results

        for provider in providers:
            try:
                health = provider.health_check()
                if health.healthy:
                    results.append(
                        _CheckResult(f"Provider: {provider.name}", "pass", "Reachable")
                    )
                else:
                    results.append(
                        _CheckResult(
                            f"Provider: {provider.name}", "warn", health.message
                        )
                    )
            except Exception as e:
                results.append(
                    _CheckResult(f"Provider: {provider.name}", "fail", str(e))
                )
    except ImportError:
        results.append(_CheckResult("Providers", "warn", "Provider module not available"))
    except Exception as e:
        results.append(_CheckResult("Providers", "fail", f"Error: {e}"))

    return results


def _check_model_registry() -> _CheckResult:
    """Check model registry status."""
    try:
        from kalash.models.registry import ModelRegistry

        registry = ModelRegistry()
        count = registry.model_count()
        return _CheckResult("Model Registry", "pass", f"{count} models registered")
    except ImportError:
        return _CheckResult("Model Registry", "warn", "Registry module not available")
    except Exception as e:
        return _CheckResult("Model Registry", "fail", f"Error: {e}")


def _check_memory() -> _CheckResult:
    """Check memory subsystem."""
    try:
        from kalash.memory.manager import MemoryManager

        manager = MemoryManager()
        info = manager.get_info()
        return _CheckResult(
            "Memory", "pass", f"Provider: {info.provider}, Entries: {info.count}"
        )
    except ImportError:
        return _CheckResult("Memory", "warn", "Memory module not available")
    except Exception as e:
        return _CheckResult("Memory", "fail", f"Error: {e}")


def _check_mcp() -> _CheckResult:
    """Check MCP server connections."""
    try:
        from kalash.mcp.registry import MCPRegistry

        registry = MCPRegistry()
        servers = registry.list_servers()

        if not servers:
            return _CheckResult("MCP", "pass", "No MCP servers configured")

        healthy = sum(1 for s in servers if s.is_healthy)
        total = len(servers)

        if healthy == total:
            return _CheckResult("MCP", "pass", f"{total} servers, all healthy")
        elif healthy > 0:
            return _CheckResult("MCP", "warn", f"{healthy}/{total} servers healthy")
        else:
            return _CheckResult("MCP", "fail", f"0/{total} servers reachable")
    except ImportError:
        return _CheckResult("MCP", "warn", "MCP module not available")
    except Exception as e:
        return _CheckResult("MCP", "fail", f"Error: {e}")


def _check_database() -> _CheckResult:
    """Check database connectivity."""
    try:
        from kalash.storage.db import Database

        db = Database()
        db.ping()
        return _CheckResult("Database", "pass", f"Connected ({db.backend})")
    except ImportError:
        return _CheckResult("Database", "warn", "Database module not available")
    except Exception as e:
        return _CheckResult("Database", "fail", f"Error: {e}")


def _check_disk() -> _CheckResult:
    """Check available disk space."""
    import shutil

    from kalash.config.paths import data_dir

    try:
        usage = shutil.disk_usage(data_dir())
        free_gb = usage.free / (1024**3)

        if free_gb < 0.5:
            return _CheckResult("Disk", "fail", f"{free_gb:.1f} GB free (< 500MB)")
        elif free_gb < 2.0:
            return _CheckResult("Disk", "warn", f"{free_gb:.1f} GB free (< 2GB)")
        else:
            return _CheckResult("Disk", "pass", f"{free_gb:.1f} GB free")
    except Exception as e:
        return _CheckResult("Disk", "fail", f"Error: {e}")


def _check_config() -> _CheckResult:
    """Check configuration validity."""
    try:
        from kalash.config.manager import ConfigManager

        manager = ConfigManager()
        errors = manager.validate()

        if not errors:
            return _CheckResult("Config", "pass", "Valid")
        else:
            return _CheckResult("Config", "warn", f"{len(errors)} issue(s): {errors[0]}")
    except ImportError:
        return _CheckResult("Config", "warn", "Config module not available")
    except Exception as e:
        return _CheckResult("Config", "fail", f"Error: {e}")


def _check_secrets() -> _CheckResult:
    """Check secrets/API key availability."""
    try:
        from kalash.config.secrets import SecretsManager

        secrets = SecretsManager()
        available = secrets.list_available()

        if not available:
            return _CheckResult("Secrets", "warn", "No API keys configured")
        return _CheckResult("Secrets", "pass", f"{len(available)} key(s) available")
    except ImportError:
        return _CheckResult("Secrets", "warn", "Secrets module not available")
    except Exception as e:
        return _CheckResult("Secrets", "fail", f"Error: {e}")
