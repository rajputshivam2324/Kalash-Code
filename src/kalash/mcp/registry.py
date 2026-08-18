"""MCP server registry for CLI management."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kalash.core.ids import generate_id
from kalash.core.paths import kalash_home
from kalash.mcp.client import MCPServerConfig, TransportType


def _mcp_settings_paths() -> list[tuple[str, Path]]:
    paths: list[tuple[str, Path]] = [
        ("global", kalash_home() / "settings" / "mcp.json"),
        ("project", Path.cwd() / ".kalash" / "settings" / "mcp.json"),
    ]
    return paths


def _load_registry() -> dict[str, Any]:
    merged: dict[str, Any] = {"servers": {}}
    for _scope, path in _mcp_settings_paths():
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


def _save_registry(scope: str, data: dict[str, Any]) -> None:
    path = dict(_mcp_settings_paths())[scope]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


@dataclass
class MCPServerEntry:
    name: str
    transport: str
    url: str
    scope: str = "project"
    env: dict[str, str] = field(default_factory=dict)
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
    """Persist and inspect MCP server configuration."""

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
        path = dict(_mcp_settings_paths())[path_scope]
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
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_registry(path_scope, data)
        return MCPServerEntry(
            name=name,
            transport=transport,
            url=url,
            scope=normalized_scope,
            env=env or {},
        )

    def list_servers(self, *, scope: str | None = None) -> list[MCPServerEntry]:
        registry = _load_registry()
        servers = registry.get("servers", {})
        entries: list[MCPServerEntry] = []
        for name, cfg in servers.items():
            if not isinstance(cfg, dict):
                continue
            entry_scope = "project"
            for label, path in _mcp_settings_paths():
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
                )
            )
        return entries

    def get_server(self, name: str) -> MCPServerEntry | None:
        for server in self.list_servers():
            if server.name == name:
                return server
        return None

    def _to_config(self, entry: MCPServerEntry) -> MCPServerConfig:
        transport_map = {
            "stdio": TransportType.STDIO,
            "sse": TransportType.SSE,
            "streamable-http": TransportType.HTTP,
            "streamable_http": TransportType.HTTP,
            "http": TransportType.HTTP,
        }
        transport = transport_map.get(entry.transport, TransportType.STDIO)
        if transport is TransportType.STDIO:
            return MCPServerConfig(
                name=entry.name,
                transport=transport,
                command=entry.url,
                env=entry.env,
            )
        return MCPServerConfig(
            name=entry.name,
            transport=transport,
            url=entry.url,
            env=entry.env,
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
                await client.disconnect()
                return MCPTestResult(
                    success=True,
                    protocol_version="mcp",
                    tool_count=len(tools),
                    latency_ms=latency,
                )
            except Exception as exc:
                return MCPTestResult(success=False, error=str(exc))

        return asyncio.run(probe())

    def oauth_flow(self, name: str) -> OAuthResult:
        return OAuthResult(
            success=False,
            error="OAuth flow not yet implemented — use `kalash mcp auth --flow token`",
        )

    def store_token(self, name: str, token: str) -> None:
        from kalash.tui.auth_store import save_credential

        save_credential(f"mcp:{name}", token)

    def store_api_key(self, name: str, key: str) -> None:
        from kalash.tui.auth_store import save_credential

        save_credential(f"mcp:{name}", key)
