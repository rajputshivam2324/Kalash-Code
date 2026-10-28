"""Remote MCP servers over Streamable HTTP, both protocol eras (see ``kalash/mcp_http.py``).

Payload shapes and status codes were observed on live public servers on 2026-10-23: DeepWiki
(legacy: a 400 with a non-modern error to ``server/discover``), Microsoft Learn (legacy: a 200
event-stream carrying -32601, and an ``Mcp-Session-Id``), Cloudflare Docs and Context7 (modern).
"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from kalash.config import McpServer
from kalash.mcp import MCPError, MCPTool, start_servers
from kalash.mcp_http import (
    HttpMCPClient,
    encode_header_value,
    header_annotations,
    header_params,
)
from tests.harness.ctx import make_ctx

URL = "https://mcp.example.test/mcp"

TOOLS = [
    {
        "name": "echo",
        "description": "Echo text back",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    }
]


def sse(*messages: dict[str, Any]) -> httpx.Response:
    body = "".join(f"event: message\ndata: {json.dumps(m)}\n\n" for m in messages)
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def reply(result: Any, request: httpx.Request, **headers: str) -> httpx.Response:
    body = {"jsonrpc": "2.0", "id": json.loads(request.content)["id"], "result": result}
    return httpx.Response(200, json=body, headers=headers)


def error(code: int, message: str, request: httpx.Request, status: int = 200) -> httpx.Response:
    body = {
        "jsonrpc": "2.0",
        "id": json.loads(request.content)["id"],
        "error": {"code": code, "message": message},
    }
    return httpx.Response(status, json=body)


class Server:
    """A scripted HTTP endpoint that records what the client sent."""

    def __init__(self, handler: Callable[[httpx.Request, dict[str, Any]], httpx.Response]) -> None:
        self.handler = handler
        self.seen: list[tuple[dict[str, str], dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.seen.append((dict(request.headers), body))
        return self.handler(request, body)

    def methods(self) -> list[str]:
        return [body.get("method", "") for _, body in self.seen]


def client_for(server: Server, **config: Any) -> HttpMCPClient:
    cfg = McpServer(url=config.pop("url", URL), **config)
    return HttpMCPClient("remote", cfg, transport=httpx.MockTransport(server))


def modern_handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
    method = body["method"]
    if method == "server/discover":
        return reply(
            {
                "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {}},
                "resultType": "complete",
                "instructions": "Use echo.",
                "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "modern", "version": "9"}},
            },
            request,
        )
    if method == "tools/list":
        return reply({"tools": TOOLS, "resultType": "complete"}, request)
    if method == "tools/call":
        text = body["params"]["arguments"].get("text", "")
        return reply({"content": [{"type": "text", "text": f"echo:{text}"}]}, request)
    return error(-32601, "unknown", request, 404)


def legacy_handler(
    request: httpx.Request, body: dict[str, Any], *, session: str | None = "sess-1"
) -> httpx.Response:
    method = body.get("method")
    if method == "server/discover":  # DeepWiki: a 400 whose error is not a modern one
        return error(-32600, "Bad Request: Unsupported protocol version: 2026-07-28", request, 400)
    if method == "initialize":
        headers = {"mcp-session-id": session} if session else {}
        result = {
            "protocolVersion": "2025-06-18",
            "serverInfo": {"name": "legacy"},
            "capabilities": {},
        }
        return reply(result, request, **headers)
    if method == "notifications/initialized":
        return httpx.Response(202)
    if method == "tools/list":
        return reply({"tools": TOOLS}, request)
    if method == "tools/call":
        return reply({"content": [{"type": "text", "text": "legacy-ok"}]}, request)
    return error(-32601, "unknown", request)


# ------------------------------------------------------------------------------- modern


async def test_modern_server_is_detected_and_every_request_carries_its_metadata():
    server = Server(modern_handler)
    client = client_for(server)
    tools = await client.list_tools()
    assert client.era == "modern" and client.version == "2026-07-28"
    assert client.server_info["name"] == "modern" and client.instructions == "Use echo."
    assert [t["name"] for t in tools] == ["echo"]
    assert server.methods() == ["server/discover", "tools/list"], "no initialize handshake"

    result = await client.call_tool("echo", {"text": "hi"})
    assert result["content"][0]["text"] == "echo:hi"
    headers, body = server.seen[-1]
    assert headers["mcp-protocol-version"] == "2026-07-28"
    assert headers["mcp-method"] == "tools/call" and headers["mcp-name"] == "echo"
    assert "text/event-stream" in headers["accept"] and "application/json" in headers["accept"]
    meta = body["params"]["_meta"]
    assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
    assert meta["io.modelcontextprotocol/clientCapabilities"] == {}
    assert meta["io.modelcontextprotocol/clientInfo"]["name"] == "kalash"
    assert "mcp-session-id" not in headers
    await client.close()


async def test_modern_pagination_follows_cursors_until_done():
    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if body["method"] == "tools/list":
            cursor = body["params"].get("cursor")
            page = {
                "tools": [{"name": f"t{cursor or 0}"}],
                "nextCursor": "1" if not cursor else None,
            }
            return reply(page, request)
        return modern_handler(request, body)

    client = client_for(Server(handler))
    assert [t["name"] for t in await client.list_tools()] == ["t0", "t1"]


async def test_the_tool_name_header_is_base64_encoded_when_it_is_not_plain_ascii():
    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if body["method"] == "tools/list":
            return reply({"tools": [{"name": "grüße"}]}, request)
        return modern_handler(request, body)

    server = Server(handler)
    client = client_for(server)
    await client.list_tools()
    await client.call_tool("grüße", {})
    wanted = "=?base64?" + base64.b64encode("grüße".encode()).decode() + "?="
    assert server.seen[-1][0]["mcp-name"] == wanted


async def test_x_mcp_header_parameters_are_mirrored_into_headers():
    schema = {
        "type": "object",
        "properties": {
            "region": {"type": "string", "x-mcp-header": "Region"},
            "limit": {"type": "integer", "x-mcp-header": "Limit"},
            "dry": {"type": "boolean", "x-mcp-header": "Dry"},
            "note": {"type": "string"},
        },
    }

    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if body["method"] == "tools/list":
            return reply({"tools": [{"name": "deploy", "inputSchema": schema}]}, request)
        return modern_handler(request, body)

    server = Server(handler)
    client = client_for(server)
    await client.list_tools()
    await client.call_tool("deploy", {"region": "us-west1", "limit": 7, "dry": False, "note": "n"})
    headers = server.seen[-1][0]
    assert headers["mcp-param-region"] == "us-west1"
    assert headers["mcp-param-limit"] == "7" and headers["mcp-param-dry"] == "false"
    assert "mcp-param-note" not in headers
    await client.call_tool("deploy", {"limit": None})
    assert "mcp-param-limit" not in server.seen[-1][0], "null values are omitted"


async def test_a_tool_with_an_invalid_header_annotation_is_dropped_with_a_warning():
    bad = {"type": "object", "properties": {"x": {"type": "number", "x-mcp-header": "X"}}}

    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if body["method"] == "tools/list":
            return reply({"tools": [{"name": "bad", "inputSchema": bad}, *TOOLS]}, request)
        return modern_handler(request, body)

    client = client_for(Server(handler))
    assert [t["name"] for t in await client.list_tools()] == ["echo"]
    assert "ignoring tool 'bad'" in client.warnings[0]


def test_header_annotation_rules_from_the_spec():
    ok = {"properties": {"a": {"type": "string", "x-mcp-header": "A"}}}
    assert header_annotations(ok) == [(("a",), "A", "string")]
    nested = {"properties": {"o": {"properties": {"b": {"type": "integer", "x-mcp-header": "B"}}}}}
    assert header_annotations(nested) == [(("o", "b"), "B", "integer")]
    for bad in (
        {"properties": {"a": {"type": "string", "x-mcp-header": ""}}},
        {"properties": {"a": {"type": "string", "x-mcp-header": "bad name"}}},
        {"properties": {"a": {"type": "number", "x-mcp-header": "A"}}},
        {
            "properties": {
                "a": {"type": "string", "x-mcp-header": "A"},
                "b": {"type": "string", "x-mcp-header": "a"},
            }
        },
        {"items": {"x-mcp-header": "A"}, "properties": {}},
        {"anyOf": [{"properties": {"a": {"type": "string", "x-mcp-header": "A"}}}]},
    ):
        with pytest.raises(ValueError):
            header_annotations(bad)
    assert header_params(nested, {"o": {"b": 3}}) == {"Mcp-Param-B": "3"}


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        ("us-west1", "us-west1"),
        ("Hello, 世界", "=?base64?SGVsbG8sIOS4lueVjA==?="),
        (" padded ", "=?base64?IHBhZGRlZCA=?="),
        ("line1\nline2", "=?base64?bGluZTEKbGluZTI=?="),
        ("=?base64?literal?=", "=?base64?PT9iYXNlNjQ/bGl0ZXJhbD89?="),
    ],
)
def test_header_value_encoding_matches_the_spec_table(value: str, encoded: str):
    assert encode_header_value(value) == encoded


async def test_a_modern_server_asking_for_input_gets_a_clear_refusal():
    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if body["method"] == "tools/call":
            return reply({"resultType": "input_required", "inputRequests": {}}, request)
        return modern_handler(request, body)

    client = client_for(Server(handler))
    await client.list_tools()
    with pytest.raises(MCPError, match="sampling/elicitation"):
        await client.call_tool("echo", {})


async def test_a_recognised_modern_error_is_reported_not_papered_over_by_the_legacy_path():
    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        return error(-32021, "needs the elicitation capability", request, 400)

    server = Server(handler)
    with pytest.raises(MCPError, match="missing client capability.*elicitation"):
        await client_for(server).start()
    assert server.methods() == ["server/discover"], "no fallback to initialize"


async def test_a_server_that_supports_only_unknown_versions_says_so():
    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        body_ = {
            "jsonrpc": "2.0",
            "id": body["id"],
            "error": {"code": -32022, "message": "x", "data": {"supported": ["2030-01-01"]}},
        }
        return httpx.Response(400, json=body_)

    with pytest.raises(MCPError, match=r"supports \['2030-01-01'\]"):
        await client_for(Server(handler)).start()


# ------------------------------------------------------------------------------- legacy


async def test_legacy_server_is_detected_by_the_fallback_and_the_session_is_echoed():
    server = Server(legacy_handler)
    client = client_for(server)
    tools = await client.list_tools()
    assert client.era == "legacy" and client.version == "2025-06-18"
    assert [t["name"] for t in tools] == ["echo"]
    assert server.methods() == [
        "server/discover",
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    init_headers = server.seen[1][0]
    assert "mcp-session-id" not in init_headers
    for headers, _ in server.seen[2:]:
        assert headers["mcp-session-id"] == "sess-1"
        assert headers["mcp-protocol-version"] == "2025-06-18"
    await client.call_tool("echo", {})
    assert "_meta" not in server.seen[-1][1]["params"], (
        "legacy requests have no per-request metadata"
    )
    count = len(server.seen)
    await client.close()
    assert len(server.seen) == count + 1, "closing sent a DELETE"
    assert server.seen[-1][0].get("mcp-session-id") == "sess-1", "...for the session it ends"


async def test_event_stream_replies_are_read_until_the_matching_response():
    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if body.get("method") == "tools/call":
            progress = {
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {"progress": 1},
            }
            other = {"jsonrpc": "2.0", "id": 999, "result": {"content": []}}
            done = {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"content": [{"type": "text", "text": "streamed"}]},
            }
            return sse(progress, other, done)
        return legacy_handler(request, body)

    client = client_for(Server(handler))
    await client.start()
    result = await client.call_tool("echo", {})
    assert result["content"][0]["text"] == "streamed"


async def test_microsoft_learn_style_discovery_error_inside_an_event_stream_falls_back():
    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if body.get("method") == "server/discover":
            return sse(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {
                        "code": -32601,
                        "message": "Method 'server/discover' is not available.",
                    },
                }
            )
        return legacy_handler(request, body)

    client = client_for(Server(handler))
    await client.start()
    assert client.era == "legacy"


async def test_an_expired_session_is_recreated_once():
    state = {"calls": 0, "sessions": 0}

    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if body.get("method") == "initialize":
            state["sessions"] += 1
            return legacy_handler(request, body, session=f"sess-{state['sessions']}")
        if body.get("method") == "tools/call":
            state["calls"] += 1
            if state["calls"] == 1:  # the server forgot the session
                return httpx.Response(404, text="session not found")
        return legacy_handler(request, body)

    client = client_for(Server(handler))
    await client.start()
    result = await client.call_tool("echo", {})
    assert result["content"][0]["text"] == "legacy-ok"
    assert state["sessions"] == 2 and state["calls"] == 2


async def test_a_legacy_server_without_sessions_works_too():
    client = client_for(Server(lambda r, b: legacy_handler(r, b, session=None)))
    await client.list_tools()
    assert client._session_id is None


async def test_unsupported_legacy_version_is_rejected():
    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if body.get("method") == "initialize":
            return reply({"protocolVersion": "2020-01-01"}, request)
        return legacy_handler(request, body)

    with pytest.raises(MCPError, match="unsupported protocol version"):
        await client_for(Server(handler)).start()


# ------------------------------------------------------------------------- failure modes


async def test_authentication_failures_explain_how_to_add_a_header():
    server = Server(lambda r, b: httpx.Response(401, text="unauthorized"))
    with pytest.raises(MCPError) as info:
        await client_for(server).start()
    text = str(info.value)
    assert "requires authentication" in text and "Authorization" in text and "${MY_TOKEN}" in text
    assert server.methods() == ["server/discover"], "auth errors do not trigger the fallback"


async def test_redirects_are_refused_because_they_would_carry_our_headers_elsewhere():
    server = Server(lambda r, b: httpx.Response(302, headers={"location": "https://evil.test/mcp"}))
    with pytest.raises(MCPError, match="redirected to https://evil.test/mcp"):
        await client_for(server, headers={"Authorization": "Bearer x"}).start()


async def test_unreachable_servers_name_the_host():
    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        raise httpx.ConnectError("name resolution failed")

    with pytest.raises(MCPError, match="cannot reach mcp.example.test"):
        await client_for(Server(handler)).start()


async def test_a_slow_server_times_out_with_the_method_name():
    import asyncio

    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(5)
            return httpx.Response(200)

    client = HttpMCPClient("slow", McpServer(url=URL, timeout=0.2), transport=Slow())
    with pytest.raises(MCPError, match="timed out"):
        await client.start()


async def test_json_rpc_batches_and_stray_content_types_are_rejected():
    def batch(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if body.get("method") == "server/discover":
            return httpx.Response(200, json=[{"jsonrpc": "2.0", "id": 1, "result": {}}])
        return legacy_handler(request, body)

    client = client_for(Server(batch))
    await client.start()
    assert client.era == "legacy", "an unreadable probe reply hands over to the handshake"

    def html(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, text="<html>", headers={"content-type": "text/html"})

    with pytest.raises(MCPError, match="unexpected content type"):
        await client_for(Server(html)).start()


async def test_the_stream_ending_without_a_response_is_an_error():
    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if body.get("method") == "tools/call":
            return sse({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
        return legacy_handler(request, body)

    client = client_for(Server(handler))
    await client.start()
    with pytest.raises(MCPError, match="ended before"):
        await client.call_tool("echo", {})


async def test_oversized_replies_are_cut_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("kalash.mcp_http.MAX_RESPONSE_BYTES", 200)
    big = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "x" * 500}]}}

    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if body.get("method") == "tools/call":
            return httpx.Response(200, json={**big, "id": body["id"]})
        return legacy_handler(request, body)

    client = client_for(Server(handler))
    await client.start()
    with pytest.raises(MCPError, match="larger than"):
        await client.call_tool("echo", {})


# ------------------------------------------------------------------------------- headers


async def test_configured_headers_expand_environment_variables(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MCP_DEMO_TOKEN", "s3cret")
    server = Server(modern_handler)
    client = client_for(server, headers={"Authorization": "Bearer ${MCP_DEMO_TOKEN}", "X-A": "b"})
    await client.start()
    headers = server.seen[0][0]
    assert headers["authorization"] == "Bearer s3cret" and headers["x-a"] == "b"


async def test_a_missing_environment_variable_is_named_not_sent_blank(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    server = Server(modern_handler)
    with pytest.raises(MCPError, match=r"\$\{NOT_SET_ANYWHERE\}, which is not set"):
        await client_for(server, headers={"Authorization": "Bearer ${NOT_SET_ANYWHERE}"}).start()
    assert server.seen == [], "nothing was sent"


# ---------------------------------------------------------------------------------- config


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "exactly one of"),
        ({"command": "x", "url": URL}, "exactly one of"),
        ({"url": URL, "args": ["a"]}, "only apply to a local"),
        ({"command": "x", "headers": {"A": "b"}}, "only apply to a remote"),
        ({"url": "http://mcp.example.test/mcp"}, "must use https"),
        ({"url": "ftp://x/mcp"}, "http\\(s\\) URL"),
        ({"url": "https://user:pw@mcp.example.test/mcp"}, "credentials"),
    ],
)
def test_server_config_is_validated(kwargs: dict[str, Any], message: str):
    with pytest.raises(ValidationError, match=message):
        McpServer(**kwargs)


def test_plain_http_is_allowed_for_loopback_only():
    for url in ("http://localhost:3001/mcp", "http://127.0.0.1:8080/mcp", "http://[::1]:9/mcp"):
        assert McpServer(url=url).target == url
    assert McpServer(command="uvx", args=["mcp-server-time"]).target == "uvx mcp-server-time"


# ----------------------------------------------------------------- manager and a real socket


async def test_remote_tools_are_namespaced_and_a_dead_server_only_warns():
    ok = HttpMCPClient(
        "good", McpServer(url=URL), transport=httpx.MockTransport(Server(modern_handler))
    )

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    bad = HttpMCPClient("dead", McpServer(url=URL), transport=httpx.MockTransport(refuse))
    import kalash.mcp as mcp_module

    original = mcp_module.make_client
    mcp_module.make_client = lambda name, cfg: ok if name == "good" else bad  # type: ignore[assignment]
    try:
        manager = await start_servers(
            {"good": McpServer(url=URL), "dead": McpServer(url=URL)}, set()
        )
    finally:
        mcp_module.make_client = original
    assert [t.name for t in manager.tools] == ["mcp__good__echo"]
    assert any("'dead' unavailable" in w and "cannot reach" in w for w in manager.warnings)
    await manager.close()


def serve(handler: Callable[[BaseHTTPRequestHandler, dict[str, Any]], None]):  # type: ignore[no-untyped-def]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
            handler(self, body)

        def do_DELETE(self) -> None:
            self.send_response(405)
            self.send_header("content-length", "0")
            self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


async def test_a_real_loopback_server_streams_events_and_the_tool_runs_end_to_end(work):  # type: ignore[no-untyped-def]
    def handler(request: BaseHTTPRequestHandler, body: dict[str, Any]) -> None:
        method = body.get("method")
        if method == "server/discover":
            payload, status, ctype = (
                b'{"error":{"code":-32601,"message":"no"}}',
                404,
                "application/json",
            )
        elif method == "notifications/initialized":
            payload, status, ctype = b"", 202, "application/json"
        elif method == "initialize":
            result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "loop"}}
            payload = f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result})}\n\n".encode()
            status, ctype = 200, "text/event-stream"
        elif method == "tools/list":
            payload = json.dumps(
                {"jsonrpc": "2.0", "id": body["id"], "result": {"tools": TOOLS}}
            ).encode()
            status, ctype = 200, "application/json"
        else:
            note = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
            done = {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"content": [{"type": "text", "text": "from the socket"}]},
            }
            payload = (f"data: {json.dumps(note)}\n\ndata: {json.dumps(done)}\n\n").encode()
            status, ctype = 200, "text/event-stream"
        request.send_response(status)
        request.send_header("content-type", ctype)
        request.send_header("content-length", str(len(payload)))
        request.end_headers()
        request.wfile.write(payload)

    httpd = serve(handler)
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
        manager = await start_servers({"loop": McpServer(url=url, trust=True)}, set())
        assert manager.warnings == [] and [t.name for t in manager.tools] == ["mcp__loop__echo"]
        tool = manager.tools[0]
        result = await tool.run(tool.parse({"text": "x"}), make_ctx(work))
        assert result.text == "from the socket" and not result.is_error
        await manager.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_mcp_tool_descriptions_are_capped():
    from kalash.mcp import MAX_DESCRIPTION

    client = HttpMCPClient("r", McpServer(url=URL))
    tool = MCPTool(client, {"name": "t", "description": "d" * 10_000}, trusted=False)
    assert len(tool.description) <= MAX_DESCRIPTION + 80 and tool.description.endswith("…")
