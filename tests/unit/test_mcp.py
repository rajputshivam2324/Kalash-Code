"""MCP stdio client against a real child process."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kalash.config import McpServer
from kalash.mcp import MCPClient, MCPError, MCPTool, start_servers, tool_name
from tests.harness.ctx import make_ctx, run_tool

SERVER = str(Path(__file__).parent.parent / "harness" / "fake_mcp_server.py")


def config(mode: str = "normal", **kw) -> McpServer:
    return McpServer(command=sys.executable, args=[SERVER], env={"MODE": mode}, **kw)


@pytest.fixture
async def client():
    c = MCPClient("fake", config(timeout=10))
    await c.start()
    yield c
    await c.close()


async def test_handshake_and_paginated_tool_listing(client):
    tools = await client.list_tools()
    assert [t["name"] for t in tools] == ["echo", "big", "fail", "slow"]
    assert client.server_info["name"] == "fake"


async def test_call_survives_server_requests_and_stray_stdout(client):
    result = await client.call_tool("echo", {"text": "hi"})
    assert result["content"][0]["text"] == "echo: hi"  # not hijacked by the server's ping


async def test_very_large_responses_do_not_kill_the_connection(client):
    result = await client.call_tool("big", {})
    assert len(result["content"][0]["text"]) > 150_000
    assert (await client.call_tool("echo", {"text": "still alive"}))["content"][0]["text"]


async def test_timeout_raises_and_client_remains_usable():
    c = MCPClient("fake", config(timeout=1))
    try:
        await c.start()
        with pytest.raises(MCPError, match="timed out"):
            await c.call_tool("slow", {})
    finally:
        await c.close()


async def test_unsupported_protocol_version_is_rejected():
    c = MCPClient("fake", config("bad_version", timeout=5))
    with pytest.raises(MCPError, match="unsupported protocol version"):
        await c.start()
    assert c.process is None  # the child was reaped


async def test_server_that_dies_during_start_reports_stderr():
    c = MCPClient("fake", config("crash_on_start", timeout=5))
    with pytest.raises(MCPError, match="cannot bind socket"):
        await c.start()
    await c.close()


async def test_missing_binary_is_a_clear_error():
    c = MCPClient("ghost", McpServer(command="/nonexistent/binary-xyz"))
    with pytest.raises(MCPError, match="cannot start"):
        await c.start()


async def test_close_terminates_the_child_process_group():
    c = MCPClient("fake", config(timeout=5))
    await c.start()
    process = c.process
    await c.close()
    assert process is not None and process.returncode is not None


async def test_concurrent_start_spawns_one_process():
    c = MCPClient("fake", config(timeout=5))
    try:
        import asyncio

        await asyncio.gather(c.start(), c.start(), c.start())
        assert len(await c.list_tools()) == 4
    finally:
        await c.close()


async def test_tools_are_namespaced_untrusted_by_default_and_run_through_the_pipeline(work: Path):
    manager = await start_servers({"fake": config(timeout=10)}, reserved={"read"})
    try:
        assert manager.warnings == []
        by_name = {t.name: t for t in manager.tools}
        assert set(by_name) == {
            "mcp__fake__echo",
            "mcp__fake__big",
            "mcp__fake__fail",
            "mcp__fake__slow",
        }
        echo = by_name["mcp__fake__echo"]
        assert isinstance(echo, MCPTool) and echo.needs_approval and echo.read_only
        ctx = make_ctx(work)
        ok = await run_tool(echo, ctx, text="hello")
        assert ok.text == "echo: hello" and not ok.is_error
        failing = await run_tool(by_name["mcp__fake__fail"], ctx)
        assert failing.is_error and failing.text == "it failed"
        big = await run_tool(by_name["mcp__fake__big"], ctx)
        assert "THE-END" in big.text and len(big.text) < 40_000
        schema = echo.spec().parameters
        assert schema["required"] == ["text"]
    finally:
        await manager.close()


async def test_trusted_server_skips_approval():
    manager = await start_servers({"fake": config(timeout=10, trust=True)}, reserved=set())
    try:
        assert not any(t.needs_approval for t in manager.tools)
    finally:
        await manager.close()


async def test_broken_server_becomes_a_warning_and_good_servers_still_load():
    manager = await start_servers(
        {"bad": config("crash_on_start", timeout=5), "good": config(timeout=10)}, reserved=set()
    )
    try:
        assert len(manager.warnings) == 1 and "bad" in manager.warnings[0]
        assert {t.name.split("__")[1] for t in manager.tools} == {"good"}
    finally:
        await manager.close()


async def test_name_collisions_are_skipped_with_a_warning():
    manager = await start_servers({"fake": config(timeout=10)}, reserved={"mcp__fake__echo"})
    try:
        assert any("collision" in w for w in manager.warnings)
        assert "mcp__fake__echo" not in {t.name for t in manager.tools}
    finally:
        await manager.close()


def test_tool_names_are_sanitised_and_length_bounded():
    assert tool_name("my server", "do.it") == "mcp__my_server__do_it"
    long = tool_name("s" * 40, "t" * 40)
    assert len(long) <= 64 and long == tool_name("s" * 40, "t" * 40)
    assert long != tool_name("s" * 40, "t" * 39 + "u")
