"""MCP over Streamable HTTP, for remote servers - both protocol eras.

Public servers in October 2026 are a mix (checked against live endpoints, 2026-10-23):

* *modern* servers (spec 2026-07-28; Cloudflare, Context7) have no handshake. Every request is its
  own POST carrying ``_meta`` with the protocol version, and the ``MCP-Protocol-Version``,
  ``Mcp-Method`` and ``Mcp-Name`` headers. ``server/discover`` says what they speak.
* *legacy* servers (<= 2025-11-25; DeepWiki, Microsoft Learn) want ``initialize`` first, may
  hand out an ``Mcp-Session-Id`` that must be echoed (and re-created after a 404), and answer in
  JSON or SSE.

The client probes ``server/discover`` and falls back to ``initialize`` unless the reply is a
recognised modern error (the spec's backward-compatibility rule). Both eras share ``MCPTool``.

Security: the URL comes only from user settings (``mcp_servers`` is not a project-safe key); it
must be https unless it is loopback; redirects are refused (a 3xx would move requests, and headers,
to another host); responses are size-capped; credentials are sent only if the user put them in
``headers``
(``${VAR}`` is expanded from the environment, so keys stay out of files).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
from typing import Any

import httpx

from kalash import __version__
from kalash.config import McpServer
from kalash.mcp import MCPError

MODERN_VERSION = "2026-07-28"
LEGACY_REQUEST_VERSION = "2025-11-25"
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_PAGES = 100
MODERN_ERRORS = {-32020: "header mismatch", -32021: "missing client capability"}
UNSUPPORTED_VERSION = -32022
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT = "io.modelcontextprotocol/clientInfo"
META_SERVER = "io.modelcontextprotocol/serverInfo"
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")  # RFC 9110 field-name token


class _Fatal(MCPError):
    """Failures that fallback to the other protocol era cannot fix: auth, redirects, no route."""


class _Reply(Exception):
    """The server answered, but with an error (HTTP status and/or a JSON-RPC error object)."""

    def __init__(self, status: int, error: dict[str, Any] | None, text: str) -> None:
        super().__init__(text)
        self.status, self.error, self.text = status, error or {}, text

    @property
    def code(self) -> int | None:
        code = self.error.get("code")
        return code if isinstance(code, int) else None

    @property
    def message(self) -> str:
        return str(self.error.get("message") or self.text or f"HTTP {self.status}")[:500]


# --------------------------------------------------------------------------- header encoding


def encode_header_value(value: str) -> str:
    """Plain ASCII as is; anything else (or an ambiguous value) as the spec's base64 sentinel."""
    plain = (
        value.isascii()
        and all(0x20 <= ord(c) <= 0x7E or c == "\t" for c in value)
        and value == value.strip(" \t")
    )
    if plain and not (value.startswith("=?base64?") and value.endswith("?=")):
        return value
    return "=?base64?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


def header_annotations(schema: dict[str, Any]) -> list[tuple[tuple[str, ...], str, str]]:
    """``x-mcp-header`` annotations as (property path, header name, json type).

    Raises ValueError for a definition the spec says must be rejected: empty or non-token names,
    duplicates (case-insensitive), non-primitive types, or an annotation not reachable through
    ``properties`` keys alone.
    """
    found: list[tuple[tuple[str, ...], str, str]] = []
    seen: set[str] = set()

    def walk(node: Any, path: tuple[str, ...]) -> None:
        if not isinstance(node, dict):
            return
        for key, child in (node.get("properties") or {}).items():
            if not isinstance(child, dict):
                continue
            if "x-mcp-header" in child:
                name = child["x-mcp-header"]
                if not isinstance(name, str) or not _TOKEN.match(name):
                    raise ValueError(f"invalid x-mcp-header name {name!r}")
                if name.lower() in seen:
                    raise ValueError(f"duplicate x-mcp-header {name!r}")
                kind = child.get("type")
                if kind not in ("string", "integer", "boolean"):
                    raise ValueError(f"x-mcp-header {name!r} on a {kind!r} parameter")
                seen.add(name.lower())
                found.append(((*path, key), name, kind))
            walk(child, (*path, key))

    walk(schema, ())
    # an annotation anywhere else (items, anyOf, $ref, ...) makes the definition invalid
    text = json.dumps({k: v for k, v in schema.items() if k != "properties"})
    if "x-mcp-header" in text:
        raise ValueError("x-mcp-header outside of statically reachable properties")
    return found


def header_params(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, str]:
    """``Mcp-Param-*`` headers for a call (values that are absent or null are omitted)."""
    headers: dict[str, str] = {}
    for path, name, _kind in header_annotations(schema):
        value: Any = arguments
        for step in path:
            value = value.get(step) if isinstance(value, dict) else None
        if value is None:
            continue
        text = ("true" if value else "false") if isinstance(value, bool) else str(value)
        headers[f"Mcp-Param-{name}"] = encode_header_value(text)
    return headers


# ------------------------------------------------------------------------------- the client


class HttpMCPClient:
    def __init__(
        self,
        name: str,
        config: McpServer,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        assert config.url is not None
        self.name = name
        self.config = config
        self.server_info: dict[str, Any] = {}
        self.instructions = ""
        self.tools_changed = False
        self.era: str | None = None  # "modern" | "legacy"
        self.version = ""
        self.warnings: list[str] = []
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._schemas: dict[str, dict[str, Any]] = {}

    # -- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        async with self._lock:
            if self.era is not None:
                return
            timeout = httpx.Timeout(self.config.timeout, connect=min(15.0, self.config.timeout))
            self._client = httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,  # never let a 3xx carry our headers to another host
                transport=self._transport,
            )
            try:
                await self._detect_era()
            except BaseException:
                await self._shutdown()
                raise

    async def close(self) -> None:
        async with self._lock:
            await self._shutdown()

    async def _shutdown(self) -> None:
        client, self._client = self._client, None
        if client is not None and self._session_id and self.era == "legacy":
            headers = self._headers(None, None)  # carries the session id we are ending
            with contextlib.suppress(Exception):  # the server may answer 405: that is fine
                await asyncio.wait_for(client.delete(self._url, headers=headers), 3)
        self._session_id = None
        if client is not None:
            await client.aclose()
        self.era = None

    # -- era detection -----------------------------------------------------------------

    async def _detect_era(self) -> None:
        try:
            result = await self._exchange("server/discover", {}, modern=True)
        except _Fatal:
            raise
        except MCPError:  # an unreadable reply to the probe: let the legacy handshake decide
            await self._init_legacy()
            return
        except _Reply as reply:
            if reply.code in MODERN_ERRORS:  # a modern server that rejected us: say why
                raise MCPError(
                    f"{self.name}: {MODERN_ERRORS[reply.code]}: {reply.message}"
                ) from None
            if reply.code == UNSUPPORTED_VERSION:
                supported = (reply.error.get("data") or {}).get("supported") or []
                if not any(v in LEGACY_VERSIONS for v in supported):
                    raise MCPError(
                        f"{self.name}: server supports {supported}, Kalash speaks "
                        f"{MODERN_VERSION} and {', '.join(LEGACY_VERSIONS)}"
                    ) from None
            await self._init_legacy()  # anything else identifies a legacy server
            return
        versions = result.get("supportedVersions") if isinstance(result, dict) else None
        if isinstance(versions, list) and MODERN_VERSION in versions:
            self.era, self.version = "modern", MODERN_VERSION
            meta = result.get("_meta") or {}
            self.server_info = meta.get(META_SERVER) or {}
            self.instructions = str(result.get("instructions") or "")
            return
        await self._init_legacy()

    async def _init_legacy(self) -> None:
        self.era = "legacy"  # headers below depend on it
        self._session_id = None
        try:
            result = await self._exchange(
                "initialize",
                {
                    "protocolVersion": LEGACY_REQUEST_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "kalash", "version": __version__},
                },
                modern=False,
            )
        except _Reply as reply:
            self.era = None
            raise MCPError(f"{self.name}: initialize failed: {reply.message}") from None
        version = result.get("protocolVersion") if isinstance(result, dict) else None
        if version not in LEGACY_VERSIONS:
            self.era = None
            raise MCPError(f"{self.name}: server speaks unsupported protocol version {version!r}")
        self.version = str(version)
        self.server_info = result.get("serverInfo") or {}
        self.instructions = str(result.get("instructions") or "")
        try:
            await self._exchange("notifications/initialized", None, modern=False, notify=True)
        except _Reply as reply:
            self.era = None
            raise MCPError(
                f"{self.name}: initialized notification rejected: {reply.message}"
            ) from None

    # -- the protocol surface ----------------------------------------------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        await self.start()
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            result = await self._request("tools/list", {"cursor": cursor} if cursor else {})
            for tool in result.get("tools", []):
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                    continue
                schema = tool.get("inputSchema")
                if self.era == "modern" and isinstance(schema, dict):
                    try:
                        header_annotations(schema)
                    except ValueError as error:  # the spec: exclude, and warn
                        self.warnings.append(
                            f"{self.name}: ignoring tool {tool['name']!r}: {error}"
                        )
                        continue
                self._schemas[tool["name"]] = schema if isinstance(schema, dict) else {}
                tools.append(tool)
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
        raise MCPError(f"{self.name}: tools/list did not terminate after {MAX_PAGES} pages")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        await self.start()
        extra: dict[str, str] = {}
        if self.era == "modern":
            extra = header_params(self._schemas.get(name, {}), arguments)
        result = await self._request(
            "tools/call", {"name": name, "arguments": arguments}, name=name, headers=extra
        )
        if not isinstance(result, dict):
            raise MCPError(f"{self.name}: tools/call returned a non-object result")
        return result

    # -- plumbing ----------------------------------------------------------------------

    @property
    def _url(self) -> str:
        assert self.config.url is not None
        return self.config.url

    def _expanded_headers(self) -> dict[str, str]:
        def expand(value: str) -> str:
            def replace(match: re.Match[str]) -> str:
                variable = match.group(1)
                if variable not in os.environ:
                    raise MCPError(
                        f"{self.name}: header value refers to ${{{variable}}}, which is not set"
                    )
                return os.environ[variable]

            return _ENV_REF.sub(replace, value)

        return {key: expand(value) for key, value in self.config.headers.items()}

    def _headers(self, method: str | None, name: str | None) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **self._expanded_headers(),
        }
        if self.era == "modern":
            headers["MCP-Protocol-Version"] = MODERN_VERSION
            if method:
                headers["Mcp-Method"] = method
            if name is not None:
                headers["Mcp-Name"] = encode_header_value(name)
        elif self.era == "legacy" and self.version:
            headers["MCP-Protocol-Version"] = self.version
            if self._session_id:
                headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _meta(self, params: dict[str, Any]) -> dict[str, Any]:
        meta = {
            META_VERSION: MODERN_VERSION,
            META_CAPABILITIES: {},
            META_CLIENT: {"name": "kalash", "version": __version__},
        }
        return {**params, "_meta": {**(params.get("_meta") or {}), **meta}}

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        name: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        for attempt in (1, 2):
            try:
                result = await self._exchange(
                    method, params, modern=self.era == "modern", name=name, extra=headers
                )
            except _Reply as reply:
                if reply.status == 404 and self._session_id and attempt == 1:
                    await self._init_legacy()  # the session expired: start a new one
                    continue
                raise MCPError(f"{self.name}: {reply.message}") from None
            break
        if self.era == "modern" and isinstance(result, dict):
            kind = result.get("resultType", "complete")
            if kind == "input_required":
                raise MCPError(
                    f"{self.name}: the server asked for input (sampling/elicitation), which "
                    "Kalash does not support"
                )
            if kind != "complete":
                raise MCPError(f"{self.name}: unrecognised resultType {kind!r}")
        return result

    async def _exchange(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        modern: bool,
        name: str | None = None,
        notify: bool = False,
        extra: dict[str, str] | None = None,
    ) -> Any:
        """POST one JSON-RPC message; return the result (raises ``_Reply`` or ``MCPError``)."""
        client = self._client
        if client is None:
            raise MCPError(f"{self.name}: not connected")
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None or not notify:
            body["params"] = self._meta(params or {}) if modern else (params or {})
        request_id: int | None = None
        if not notify:
            self._next_id += 1
            request_id = body["id"] = self._next_id
        headers = {**self._headers(method, name), **(extra or {})}
        if modern and self.era is None:  # the probe, before the era is known
            headers["MCP-Protocol-Version"] = MODERN_VERSION
            headers["Mcp-Method"] = method
        try:
            return await asyncio.wait_for(
                self._post(client, headers, body, request_id), self.config.timeout
            )
        except TimeoutError:
            raise MCPError(
                f"{self.name}: `{method}` timed out after {self.config.timeout:.0f}s"
            ) from None
        except httpx.TimeoutException:
            raise _Fatal(f"{self.name}: `{method}` timed out") from None
        except httpx.HTTPError as error:
            host = httpx.URL(self._url).host
            raise _Fatal(
                f"{self.name}: cannot reach {host}: {error or type(error).__name__}"
            ) from error

    async def _post(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        body: dict[str, Any],
        request_id: int | None,
    ) -> Any:
        request = client.build_request(
            "POST", self._url, headers=headers, content=json.dumps(body).encode()
        )
        response = await client.send(request, stream=True)
        try:
            if 300 <= response.status_code < 400:
                where = response.headers.get("location", "somewhere else")
                raise _Fatal(
                    f"{self.name}: the server redirected to {where}; Kalash does not follow "
                    "redirects for MCP - update the url in settings"
                )
            if response.status_code in (401, 403):
                raise _Fatal(
                    f"{self.name}: the server requires authentication (HTTP "
                    f"{response.status_code}). Add a header in settings, e.g. "
                    '"headers": {"Authorization": "Bearer ${MY_TOKEN}"}'
                )
            if response.status_code == 202 and request_id is None:
                return None
            if response.status_code >= 400:
                text = (await _read_capped(response)).decode("utf-8", "replace")
                error_object = _error_object(text)
                if error_object is None:  # not a JSON-RPC error: say what it is, briefly
                    kind = response.headers.get("content-type", "").split(";")[0].strip()
                    page = " (a web page, so this is probably not an MCP endpoint)"
                    text = f"HTTP {response.status_code}" + (page if "html" in kind else "")
                raise _Reply(response.status_code, error_object, text[:300])
            if request_id is None:
                return None
            session = response.headers.get("mcp-session-id")
            if session and body.get("method") == "initialize":
                self._session_id = session
            message = await _find_response(response, request_id)
        finally:
            await response.aclose()
        if "error" in message:
            error = message["error"] if isinstance(message["error"], dict) else {}
            raise _Reply(response.status_code, error, str(error.get("message", "error")))
        return message.get("result")


# ------------------------------------------------------------------------------ responses


async def _read_capped(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise MCPError(f"response larger than {MAX_RESPONSE_BYTES // (1024 * 1024)} MiB")
        chunks.append(chunk)
    return b"".join(chunks)


def _error_object(text: str) -> dict[str, Any] | None:
    """The JSON-RPC error in an HTTP error body (plain JSON or a one-event SSE stream)."""
    payload = text.strip()
    if payload.startswith(("event:", "data:")):
        data = "".join(
            line[5:].strip() for line in payload.splitlines() if line.startswith("data:")
        )
        payload = data
    try:
        loaded = json.loads(payload)
    except ValueError:
        return None
    error = loaded.get("error") if isinstance(loaded, dict) else None
    return error if isinstance(error, dict) else None


async def _find_response(response: httpx.Response, request_id: int) -> dict[str, Any]:
    kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if kind == "text/event-stream":
        return await _response_from_sse(response, request_id)
    if kind == "application/json":
        data = json.loads((await _read_capped(response)).decode("utf-8"))
        if isinstance(data, list):
            raise MCPError("the server answered with a JSON-RPC batch, which MCP no longer allows")
        if isinstance(data, dict) and data.get("id") == request_id:
            return data
        raise MCPError(f"unexpected reply to request {request_id}: {str(data)[:200]}")
    raise MCPError(f"unexpected content type {kind or '(none)'} in the server's reply")


async def _response_from_sse(response: httpx.Response, request_id: int) -> dict[str, Any]:
    total = 0
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        total += len(line) + 1
        if total > MAX_RESPONSE_BYTES:
            raise MCPError(f"event stream larger than {MAX_RESPONSE_BYTES // (1024 * 1024)} MiB")
        if line:
            if line.startswith("data:"):
                data_lines.append(line[5:].removeprefix(" "))
            continue
        payload, data_lines = "\n".join(data_lines), []
        if not payload.strip():
            continue  # a priming event or keep-alive
        try:
            message = json.loads(payload)
        except ValueError:
            continue
        if (
            isinstance(message, dict)
            and message.get("id") == request_id
            and ("result" in message or "error" in message)
        ):
            return message
    raise MCPError("the event stream ended before the server's response")
