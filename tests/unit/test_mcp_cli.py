"""`kalash mcp add | list | test | remove` - configuring and checking servers without editing JSON."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from kalash.cli.main import app
from kalash.paths import kalash_home

runner = CliRunner()


def settings_file() -> dict[str, Any]:
    return json.loads((kalash_home() / "settings.json").read_text())


def test_add_remote_and_local_servers_then_list_and_remove(work: Path, monkeypatch):
    monkeypatch.chdir(work.parent)
    add = runner.invoke(app, ["mcp", "add", "wiki", "https://mcp.example.test/mcp", "--trust"])
    assert add.exit_code == 0 and "kalash mcp test wiki" in add.output
    local = runner.invoke(
        app, ["mcp", "add", "time", "-e", "TZ=UTC", "--", "uvx", "mcp-server-time", "--flag"]
    )
    assert local.exit_code == 0, local.output
    saved = settings_file()["mcp_servers"]
    assert saved["wiki"] == {"url": "https://mcp.example.test/mcp", "trust": True}
    assert saved["time"] == {
        "command": "uvx",
        "args": ["mcp-server-time", "--flag"],
        "env": {"TZ": "UTC"},
    }
    listing = runner.invoke(app, ["mcp", "list"]).output
    assert "wiki" in listing and "remote" in listing and "trusted" in listing
    assert "time" in listing and "local" in listing and "uvx mcp-server-time --flag" in listing
    removed = runner.invoke(app, ["mcp", "remove", "wiki"])
    assert removed.exit_code == 0 and "wiki" not in settings_file()["mcp_servers"]
    again = runner.invoke(app, ["mcp", "remove", "wiki"])
    assert again.exit_code == 2 and "no MCP server 'wiki'" in again.output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["bad name", "https://x.test/mcp"], "letters, digits"),
        (["x", "http://remote.test/mcp"], "must use https"),
        (["x", "https://x.test/mcp", "-H", "NoColon"], "NAME:VALUE"),
        (["x", "uvx", "-H", "A: b"], "only apply to a remote"),
        (["x", "https://x.test/mcp", "--scope", "project"], "user or local"),
    ],
)
def test_add_rejects_bad_input_without_writing(
    args: list[str], message: str, work: Path, monkeypatch
):
    monkeypatch.chdir(work.parent)
    result = runner.invoke(app, ["mcp", "add", *args])
    assert result.exit_code == 2 and message in result.output
    assert not (kalash_home() / "settings.json").exists()


def test_headers_are_stored_with_their_variable_references_not_values(work: Path, monkeypatch):
    monkeypatch.chdir(work.parent)
    monkeypatch.setenv("DEMO_TOKEN", "should-never-be-written")
    runner.invoke(
        app,
        ["mcp", "add", "api", "https://api.test/mcp", "-H", "Authorization: Bearer ${DEMO_TOKEN}"],
    )
    text = (kalash_home() / "settings.json").read_text()
    assert "Bearer ${DEMO_TOKEN}" in text and "should-never-be-written" not in text


def test_local_scope_writes_the_untracked_project_file(work: Path, monkeypatch):
    work.mkdir(parents=True, exist_ok=True)
    (work / ".git").mkdir(exist_ok=True)
    monkeypatch.chdir(work)
    result = runner.invoke(app, ["mcp", "add", "x", "https://x.test/mcp", "--scope", "local"])
    assert result.exit_code == 0
    local = json.loads((work / ".kalash" / "settings.local.json").read_text())
    assert "x" in local["mcp_servers"]


def serve_legacy_server() -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_DELETE(self) -> None:
            self.send_response(405)
            self.send_header("content-length", "0")
            self.end_headers()

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            method = body.get("method")
            result: Any = {}
            status = 200
            if method == "server/discover":
                status, error = 404, {"code": -32601, "message": "no"}
                payload = json.dumps({"jsonrpc": "2.0", "id": body["id"], "error": error}).encode()
            else:
                if method == "initialize":
                    result = {
                        "protocolVersion": "2025-06-18",
                        "serverInfo": {"name": "fake", "version": "1.2"},
                    }
                elif method == "tools/list":
                    result = {"tools": [{"name": "ping", "description": "Say pong\nmore"}]}
                if method == "notifications/initialized":
                    status, payload = 202, b""
                else:
                    payload = json.dumps(
                        {"jsonrpc": "2.0", "id": body["id"], "result": result}
                    ).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_test_command_connects_lists_tools_and_reports_failures(work: Path, monkeypatch):
    monkeypatch.chdir(work.parent)
    httpd = serve_legacy_server()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
        ad_hoc = runner.invoke(app, ["mcp", "test", url])
        assert ad_hoc.exit_code == 0, ad_hoc.output
        assert "[ok  ]" in ad_hoc.output and "fake 1.2" in ad_hoc.output
        assert "remote, legacy protocol 2025-06-18" in ad_hoc.output
        assert (
            "1 tools" in ad_hoc.output and "ping" in ad_hoc.output and "Say pong" in ad_hoc.output
        )
        assert "more" not in ad_hoc.output.replace("more than", ""), (
            "only the first description line"
        )
        runner.invoke(app, ["mcp", "add", "local-fake", url])
        configured = runner.invoke(app, ["mcp", "test", "local-fake"])
        assert configured.exit_code == 0 and "local-fake" in configured.output
    finally:
        httpd.shutdown()
        httpd.server_close()
    down = runner.invoke(app, ["mcp", "test", url, "--timeout", "3"])
    assert down.exit_code == 1 and "[FAIL]" in down.output and "cannot reach" in down.output


def test_test_command_rejects_unknown_names_and_handles_an_empty_config(work: Path, monkeypatch):
    monkeypatch.chdir(work.parent)
    assert "No MCP servers configured" in runner.invoke(app, ["mcp", "test"]).output
    unknown = runner.invoke(app, ["mcp", "test", "nope"])
    assert unknown.exit_code == 2 and "no MCP server named 'nope'" in unknown.output


def test_trust_flips_the_flag_in_the_file_that_defines_the_server(work: Path, monkeypatch):
    monkeypatch.chdir(work.parent)
    runner.invoke(app, ["mcp", "add", "wiki", "https://mcp.example.test/mcp"])
    on = runner.invoke(app, ["mcp", "trust", "wiki"])
    assert on.exit_code == 0 and "now trusted" in on.output
    assert settings_file()["mcp_servers"]["wiki"]["trust"] is True
    off = runner.invoke(app, ["mcp", "trust", "wiki", "--off"])
    assert off.exit_code == 0 and settings_file()["mcp_servers"]["wiki"]["trust"] is False
    missing = runner.invoke(app, ["mcp", "trust", "nope"])
    assert missing.exit_code == 2 and "no MCP server 'nope'" in missing.output
