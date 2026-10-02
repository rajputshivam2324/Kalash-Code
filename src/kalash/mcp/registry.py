"""MCP server registry for CLI management."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalash.core.paths import kalash_home
from kalash.mcp.client import MCPServerConfig, TransportType


def _mcp_settings_paths(cwd: Path | None = None) -> list[tuple[str, Path]]:
    paths: list[tuple[str, Path]] = [
        ("global", kalash_home() / "settings" / "mcp.json"),
        ("project", (cwd or Path.cwd()) / ".kalash" / "settings" / "mcp.json"),
    ]
    return paths


def _load_registry(cwd: Path | None = None) -> dict[str, Any]:
    merged: dict[str, Any] = {"servers": {}}
    for _scope, path in _mcp_settings_paths(cwd):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        servers = data.get("servers", {})
        if isinstance(servers, dict):
            merged["servers"].update(servers)
    return merged


def _save_registry(scope: str, data: dict[str, Any], cwd: Path | None = None) -> None:
    path = dict(_mcp_settings_paths(cwd))[scope]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


@dataclass
class MCPServerEntry:
    name: str
    transport: str
    url: str
    scope: str = "project"
    env: dict[str, str] = field(default_factory=dict)
    command: str = ""
    args: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    is_healthy: bool = False
    tool_count: int = 0


@dataclass
class MCPTestResult:
    success: bool
    protocol_version: str = ""
    tool_count: int = 0
    latency_ms: float = 0.0
    error: str = ""


@dataclass
class OAuthResult:
    success: bool
    error: str = ""


class MCPRegistry:
    """Persist and inspect configuration for a specific workspace."""

    def __init__(self, cwd: Path | None = None) -> None:
        self.cwd = (cwd or Path.cwd()).resolve()

    def add_server(
        self,
        *,
        name: str,
        url: str,
        transport: str = "stdio",
        env: dict[str, str] | None = None,
        scope: str = "project",
    ) -> MCPServerEntry:
        normalized_scope = "global" if scope in ("global", "user") else "project"
        path_scope = normalized_scope
        path = dict(_mcp_settings_paths(self.cwd))[path_scope]
        data: dict[str, Any] = {"servers": {}}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {"servers": {}}
        servers = data.setdefault("servers", {})
        servers[name] = {
            "transport": transport,
            "url": url,
            "env": env or {},
            "registered_at": datetime.now(UTC).isoformat(),
        }
        _save_registry(path_scope, data, self.cwd)
        if normalized_scope == "project":
            from kalash.core.trust import trust_file

            trust_file(path)
        return MCPServerEntry(
            name=name,
            transport=transport,
            url=url,
            scope=normalized_scope,
            env=env or {},
        )

    def list_servers(self, *, scope: str | None = None) -> list[MCPServerEntry]:
        registry = _load_registry(self.cwd)
        servers = registry.get("servers", {})
        entries: list[MCPServerEntry] = []
        for name, cfg in servers.items():
            if not isinstance(cfg, dict):
                continue
            entry_scope = "project"
            for label, path in _mcp_settings_paths(self.cwd):
                if path.exists():
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                        if name in data.get("servers", {}):
                            entry_scope = label
                    except (json.JSONDecodeError, OSError):
                        pass
            if scope and entry_scope != scope and scope not in ("global", "user", "project"):
                continue
            if scope in ("global", "user") and entry_scope != "global":
                continue
            if scope == "project" and entry_scope != "project":
                continue
            entries.append(
                MCPServerEntry(
                    name=name,
                    transport=str(cfg.get("transport", "stdio")),
                    url=str(cfg.get("url", "")),
                    scope=entry_scope,
                    env=dict(cfg.get("env", {})),
                    command=str(cfg.get("command", "")),
                    args=list(cfg.get("args", [])),
                    headers=dict(cfg.get("headers", {})),
                )
            )
        return entries

    def get_server(self, name: str) -> MCPServerEntry | None:
        for server in self.list_servers():
            if server.name == name:
                return server
        return None

    def _to_config(self, entry: MCPServerEntry) -> MCPServerConfig:
        def expand(values: dict[str, str]) -> dict[str, str]:
            def variable(match: re.Match[str]) -> str:
                name = match.group(1)
                if name not in os.environ:
                    raise ValueError(f"MCP {entry.name!r} requires environment variable {name}")
                return os.environ[name]

            return {
                key: re.sub(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}", variable, value)
                for key, value in values.items()
            }

        transport_map = {
            "stdio": TransportType.STDIO,
            "sse": TransportType.SSE,
            "streamable-http": TransportType.HTTP,
            "streamable_http": TransportType.HTTP,
            "http": TransportType.HTTP,
        }
        if entry.transport not in transport_map:
            raise ValueError(f"Unknown MCP transport {entry.transport!r}")
        transport = transport_map[entry.transport]
        if transport is TransportType.STDIO:
            import shlex

            command = [entry.command, *entry.args] if entry.command else shlex.split(entry.url)
            if not command or not command[0]:
                raise ValueError(f"MCP server {entry.name!r} requires a command")
            return MCPServerConfig(
                name=entry.name,
                transport=transport,
                command=command[0],
                args=command[1:],
                env=expand(entry.env),
            )
        return MCPServerConfig(
            name=entry.name,
            transport=transport,
            url=entry.url,
            env=expand(entry.env),
            headers=expand(entry.headers),
        )

    def test_server(self, name: str, *, timeout: int = 10) -> MCPTestResult:
        entry = self.get_server(name)
        if entry is None:
            return MCPTestResult(success=False, error=f"server {name!r} not found")

        async def probe() -> MCPTestResult:
            import time

            from kalash.mcp.client import MCPClient

            client = MCPClient(self._to_config(entry))
            started = time.perf_counter()
            try:
                await asyncio.wait_for(client.connect(), timeout=timeout)
                tools = client.tools
                latency = (time.perf_counter() - started) * 1000
                return MCPTestResult(
                    success=True,
                    protocol_version="mcp",
                    tool_count=len(tools),
                    latency_ms=latency,
                )
            except Exception as exc:
                return MCPTestResult(success=False, error=str(exc))
            finally:
                await client.disconnect()

        return asyncio.run(probe())

    def oauth_flow(self, name: str) -> OAuthResult:
        return OAuthResult(
            success=False,
            error="OAuth flow not yet implemented — use `kalash mcp auth --flow token`",
        )

    def store_token(self, name: str, token: str) -> None:
        from kalash.models.auth_store import save_credential

        save_credential(f"mcp:{name}", token)

    def store_api_key(self, name: str, key: str) -> None:
        from kalash.models.auth_store import save_credential

        save_credential(f"mcp:{name}", key)
