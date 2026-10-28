"""Model Context Protocol: the stdio client, the tool adapter and the server manager.

Local servers speak newline-delimited JSON-RPC 2.0 over stdio. The client performs the
``initialize`` handshake, pages through ``tools/list``, answers the requests a server may send us
(``ping``, ``roots/list``), cancels timed-out calls and reaps the server process group. Remote
servers (``url`` in settings) are ``mcp_http.HttpMCPClient``; both feed the same ``MCPTool``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import signal
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ConfigDict

from kalash import __version__
from kalash.config import McpServer
from kalash.sandbox import clean_env
from kalash.tools.base import Params, Tool, ToolContext, ToolResult
from kalash.tools.output import limit_output

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_DESCRIPTION = 1500  # a server's tool text is untrusted; keep it from flooding the prompt
READ_LIMIT = 64 * 1024 * 1024  # a single JSON-RPC line may be large (images, big results)


class MCPError(Exception):
    """A server failed to start, answered with an error, or stopped responding."""


class MCPClient:
    def __init__(self, name: str, config: McpServer) -> None:
        self.name = name
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._reader: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr: deque[str] = deque(maxlen=20)
        self._lock = asyncio.Lock()
        self.tools_changed = False
        self.server_info: dict[str, Any] = {}

    async def start(self) -> None:
        async with self._lock:
            if self.process is not None and self.process.returncode is None:
                return
            env = clean_env(extra=self.config.env, pass_env=list(self.config.env))
            assert self.config.command is not None  # a url server uses HttpMCPClient
            try:
                self.process = await asyncio.create_subprocess_exec(
                    self.config.command,
                    *self.config.args,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=READ_LIMIT,
                    start_new_session=True,
                )
            except OSError as error:
                raise MCPError(f"cannot start `{self.config.command}`: {error}") from error
            self._reader = asyncio.create_task(self._read_loop())
            self._stderr_task = asyncio.create_task(self._drain_stderr())
            try:
                result = await self._request(
                    "initialize",
                    {
                        "protocolVersion": SUPPORTED_VERSIONS[1],
                        "capabilities": {},
                        "clientInfo": {"name": "kalash", "version": __version__},
                    },
                )
                version = result.get("protocolVersion") if isinstance(result, dict) else None
                if version not in SUPPORTED_VERSIONS:
                    raise MCPError(f"server speaks unsupported protocol version {version!r}")
                self.server_info = result.get("serverInfo", {})
                await self._notify("notifications/initialized")
            except BaseException:
                await self._terminate()
                raise

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(100):  # bounded: a server cannot page forever
            result = await self._request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(t for t in result.get("tools", []) if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
        raise MCPError("tools/list did not terminate after 100 pages")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        await self.start()
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise MCPError("tools/call returned a non-object result")
        return result

    async def close(self) -> None:
        async with self._lock:
            await self._terminate()

    # -- plumbing ----------------------------------------------------------------------

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(asyncio.shield(future), self.config.timeout)
        except TimeoutError:
            self._pending.pop(request_id, None)
            with contextlib.suppress(MCPError):
                await self._notify(
                    "notifications/cancelled", {"requestId": request_id, "reason": "client timeout"}
                )
            raise MCPError(
                f"{self.name}: `{method}` timed out after {self.config.timeout:.0f}s"
            ) from None
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            with contextlib.suppress(MCPError):
                await self._notify(
                    "notifications/cancelled", {"requestId": request_id, "reason": "cancelled"}
                )
            raise

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            message["params"] = params
        await self._send(message)

    async def _send(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise MCPError(f"{self.name}: server is not running{self._stderr_hint()}")
        try:
            process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as error:
            raise MCPError(f"{self.name}: server closed its input{self._stderr_hint()}") from error

    async def _read_loop(self) -> None:
        process = self.process
        assert process is not None and process.stdout is not None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # servers sometimes print stray text to stdout
                if isinstance(message, dict):
                    await self._dispatch(message)
        except (asyncio.LimitOverrunError, ValueError) as error:
            self._fail_all(f"{self.name}: unreadable server output: {error}")
            return
        self._fail_all(f"{self.name}: server exited{self._stderr_hint()}")

    async def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method is not None:  # a request or notification FROM the server
            if "id" in message:
                answer: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
                if method == "ping":
                    answer["result"] = {}
                elif method == "roots/list":
                    answer["result"] = {"roots": []}
                else:
                    answer["error"] = {"code": -32601, "message": f"method not found: {method}"}
                with contextlib.suppress(MCPError):
                    await self._send(answer)
            elif method == "notifications/tools/list_changed":
                self.tools_changed = True
            return
        future = self._pending.pop(message.get("id", -1), None)
        if future is None or future.done():
            return
        if "error" in message:
            error = message["error"] if isinstance(message["error"], dict) else {}
            future.set_exception(MCPError(f"{self.name}: {error.get('message', message['error'])}"))
        else:
            future.set_result(message.get("result"))

    async def _drain_stderr(self) -> None:
        process = self.process
        assert process is not None and process.stderr is not None
        with contextlib.suppress(asyncio.LimitOverrunError, ValueError):
            while line := await process.stderr.readline():
                self._stderr.append(line.decode("utf-8", "replace").rstrip())

    def _stderr_hint(self) -> str:
        return f" (stderr: {' | '.join(list(self._stderr)[-3:])})" if self._stderr else ""

    def _fail_all(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MCPError(reason))
        self._pending.clear()

    async def _terminate(self) -> None:
        process, self.process = self.process, None
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        for task in (self._reader, self._stderr_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._reader = self._stderr_task = None
        self._fail_all(f"{self.name}: server stopped")


# ----------------------------------------------------------------------------- tools


class RawParams(Params):
    model_config = ConfigDict(extra="allow")


def tool_name(server: str, tool: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp__{server}__{tool}")
    if len(clean) > 64:
        digest = hashlib.sha256(clean.encode()).hexdigest()[:8]
        clean = f"{clean[:55]}_{digest}"
    return clean


class MCPTool(Tool):
    params = RawParams
    description = ""

    def __init__(self, client: Client, spec: dict[str, Any], trusted: bool) -> None:
        self.client = client
        self.remote_name = str(spec["name"])
        self.name = tool_name(client.name, self.remote_name)  # type: ignore[misc]
        annotations = spec.get("annotations") or {}
        self.read_only = bool(annotations.get("readOnlyHint"))  # type: ignore[misc]
        self.needs_approval = not trusted  # type: ignore[misc]
        text = str(spec.get("description") or self.remote_name)
        if len(text) > MAX_DESCRIPTION:
            text = text[: MAX_DESCRIPTION - 1].rstrip() + "…"
        self.description = f"[MCP server {client.name}] {text}"  # type: ignore[misc]
        schema = spec.get("inputSchema")
        self._schema = schema if isinstance(schema, dict) else {"type": "object"}

    def spec(self) -> Any:
        from kalash.llm.types import ToolSpec

        schema = dict(self._schema)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        return ToolSpec(self.name, self.description, schema)

    def permission_key(self, params: Any, ctx: ToolContext) -> str:
        return f"{self.name} {json.dumps(params.model_dump(), sort_keys=True)[:200]}"

    async def run(self, params: RawParams, ctx: ToolContext) -> ToolResult:
        try:
            result = await self.client.call_tool(self.remote_name, params.model_dump())
        except MCPError as error:
            return ToolResult(str(error), is_error=True)
        parts: list[str] = []
        for item in result.get("content", []) or []:
            kind = item.get("type") if isinstance(item, dict) else None
            if kind == "text":
                parts.append(str(item.get("text", "")))
            elif kind == "image":
                parts.append(f"[image: {item.get('mimeType', 'unknown')}, not shown]")
            elif kind == "resource":
                resource = item.get("resource") or {}
                parts.append(str(resource.get("text") or f"[resource: {resource.get('uri')}]"))
            else:
                parts.append(json.dumps(item)[:500])
        if not parts and result.get("structuredContent") is not None:
            parts.append(json.dumps(result["structuredContent"]))
        text = limit_output("\n".join(parts) or "(no output)", ctx.spill_dir, "mcp")
        return ToolResult(text, is_error=bool(result.get("isError")))


class Client(Protocol):
    """What the tool adapter and the manager need from a server connection."""

    name: str
    config: McpServer
    server_info: dict[str, Any]

    async def start(self) -> None: ...
    async def list_tools(self) -> list[dict[str, Any]]: ...
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...
    async def close(self) -> None: ...


def make_client(name: str, config: McpServer) -> Client:
    if config.url is not None:
        from kalash.mcp_http import HttpMCPClient

        return HttpMCPClient(name, config)
    return MCPClient(name, config)


@dataclass
class MCPManager:
    clients: list[Client]
    tools: list[MCPTool]
    warnings: list[str]

    async def close(self) -> None:
        await asyncio.gather(*(c.close() for c in self.clients), return_exceptions=True)

    def set_trust(self, name: str, trusted: bool) -> int:
        """Run (or stop running) a server's tools without asking, for this session. Returns how
        many tools changed; 0 means there is no such server."""
        changed = 0
        for tool in self.tools:
            if tool.client.name == name:
                tool.needs_approval = not trusted  # type: ignore[misc]
                changed += 1
        for client in self.clients:
            if client.name == name:
                client.config.trust = trusted
        return changed


async def start_servers(servers: dict[str, McpServer], reserved: set[str]) -> MCPManager:
    """Start every configured server concurrently; failures become warnings, not crashes."""
    clients = [make_client(name, config) for name, config in servers.items()]

    async def boot(client: Client) -> tuple[Client, list[dict[str, Any]] | str]:
        try:
            await asyncio.wait_for(client.start(), client.config.timeout)
            return client, await client.list_tools()
        except (MCPError, TimeoutError, OSError) as error:
            await client.close()
            return client, f"MCP server {client.name!r} unavailable: {error or 'timed out'}"

    results = await asyncio.gather(*(boot(c) for c in clients))
    manager = MCPManager([], [], [])
    taken = set(reserved)
    for client, outcome in results:
        if isinstance(outcome, str):
            manager.warnings.append(outcome)
            continue
        manager.clients.append(client)
        for spec in outcome:
            if not isinstance(spec.get("name"), str):
                continue
            tool = MCPTool(client, spec, client.config.trust)
            if tool.name in taken:
                manager.warnings.append(f"MCP tool name collision, skipping: {tool.name}")
                continue
            taken.add(tool.name)
            manager.tools.append(tool)
    return manager
