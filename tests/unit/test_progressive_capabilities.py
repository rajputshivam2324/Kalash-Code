"""Integration contracts for extension disclosure, policy and resumed context."""

import json
from unittest.mock import AsyncMock, MagicMock

from kalash.mcp.client import MCPToolSchema
from kalash.mcp.tools import MCPToolAdapter
from kalash.models.normalize import Message, Role, ToolResultBlock, ToolUseBlock
from kalash.runtime.agent import _discover_skills
from kalash.skills.loader import SkillLoader
from kalash.tools.base import ToolContext
from kalash.tools.schema import schema_tokens, tool_schema
from kalash.tools.skill import SkillParams, SkillTool
from tests.unit.test_agent_loop import (
    AutoApprover,
    FakeGateway,
    make_host,
    make_loop,
    send,
    text_turn,
    tool_turn,
)


def add_extension(host, name="mcp__docs__search", description="Search API documentation"):
    manager = MagicMock()
    manager.call_tool = AsyncMock(return_value={"content": "evidence"})
    tool = MCPToolAdapter(
        MCPToolSchema(
            name=name.split("__")[-1],
            namespaced_name=name,
            server_name="docs",
            description=description,
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "A detailed query " * 40}
                },
                "required": ["query"],
            },
        ),
        manager,
    )
    host.registry.register(tool, source="mcp")
    return manager


async def test_loop_discovers_schema_before_execution(tmp_path):
    host = make_host(tmp_path, ui=AutoApprover())
    manager = add_extension(host)
    gateway = FakeGateway(
        [
            tool_turn("tool_search", {"query": "documentation"}, call_id="discover"),
            tool_turn("mcp__docs__search", {"query": "API"}, call_id="execute"),
            text_turn("verified"),
        ],
    )
    loop = make_loop(gateway, host)
    result = await send(loop, "look up API docs")
    assert result.error is None
    assert "mcp__docs__search" not in {s["name"] for s in gateway.calls[0]["tools"]}
    assert "mcp__docs__search" in {s["name"] for s in gateway.calls[1]["tools"]}
    manager.call_tool.assert_awaited_once_with("mcp__docs__search", {"query": "API"})
    assert host.schemas() == gateway.calls[2]["tools"]


async def test_browse_is_metadata_only_and_search_does_not_execute(tmp_path):
    host = make_host(tmp_path, ui=AutoApprover())
    manager = add_extension(host)
    outcome = await host.execute_result("tool_search", {}, tool_use_id="browse")
    assert not outcome.is_error
    assert json.loads(outcome.content)["loaded"] == []
    assert host.loaded_tools == []
    assert "input_schema" not in outcome.content
    host.discover_tools(names=["mcp__docs__search"])
    manager.call_tool.assert_not_awaited()
    assert host.loaded_tools == ["mcp__docs__search"]


def test_discovery_filters_policy_and_resolved_source(tmp_path):
    host = make_host(tmp_path, ui=AutoApprover())
    add_extension(host)
    add_extension(host, "mcp__docs__create", "Create a remote page")
    host.mode = "plan"
    assert json.loads(host.discover_tools().content)["catalog_size"] == 1
    assert not host.discover_tools(names=["mcp__docs__create"]).ok
    host.network_enabled = False
    assert json.loads(host.discover_tools(query="docs").content)["loaded"] == []
    assert "fetch" not in {s["name"] for s in host.schemas()}
    host.network_enabled = True
    # A low-priority registration must not turn a built-in into a deferred extension.
    add_extension(host, "read", "Search remote content")
    assert host.registry.source("read") == "builtin"
    assert "read" not in {t["name"] for t in json.loads(host.discover_tools().content)["tools"]}


def test_resume_restores_only_availability_and_rechecks_policy(tmp_path):
    host = make_host(tmp_path, ui=AutoApprover())
    manager = add_extension(host)
    host.restore_tools(
        [
            Message(Role.ASSISTANT, [ToolUseBlock(id="find", name="tool_search", input={})]),
            Message(
                Role.USER,
                [
                    ToolResultBlock(
                        tool_use_id="find",
                        content=json.dumps(
                            {"loaded": ["mcp__docs__search", "missing", {"bad": "type"}]}
                        ),
                    )
                ],
            ),
        ]
    )
    assert host.loaded_tools == ["mcp__docs__search"]
    manager.call_tool.assert_not_awaited()
    host.network_enabled = False
    assert "mcp__docs__search" not in {s["name"] for s in host.schemas()}
    host.network_enabled = True
    assert "mcp__docs__search" in {s["name"] for s in host.schemas()}


def test_synthetic_schema_cost_drops_with_large_catalog(tmp_path):
    host = make_host(tmp_path, ui=AutoApprover())
    for i in range(100):
        add_extension(host, f"mcp__docs__search_{i}")
    eager = [tool_schema(t) for t in host.registry.list_tools()]
    initial = schema_tokens(host.schemas())
    host.discover_tools(names=["mcp__docs__search_42"])
    loaded = schema_tokens(host.schemas())
    assert initial < loaded < schema_tokens(eager) * 0.20
    # Repeated queries do not reshuffle or duplicate schema activations.
    before = host.schemas()
    host.discover_tools(names=["mcp__docs__search_42"])
    assert host.schemas() == before


async def test_bundled_catalog_body_and_one_resource(tmp_path):
    loader = SkillLoader(tmp_path)
    entries = loader.discover_now()
    expected = {
        "filesystem",
        "shell-execution",
        "code",
        "git",
        "web",
        "pdf",
        "documents",
        "spreadsheets",
        "presentations",
        "images",
        "archive",
        "data",
    }
    assert set(entries) == expected
    assert all(e.source == "builtin" and not e.is_loaded for e in entries.values())
    assert all(not loader.validate_skill(e.path.parent) for e in entries.values())
    catalog = _discover_skills(tmp_path)
    assert all(set(item) == {"name", "description"} for item in catalog)
    ctx = ToolContext(session_id="s", run_id="r", cwd=tmp_path)
    tool = SkillTool()
    body = await tool.execute(SkillParams(name="pdf"), ctx)
    assert "Available resources" in body.content
    assert "from reportlab" not in body.content
    resource = await tool.execute(
        SkillParams(name="pdf", resource="references/workflow.md", limit=80), ctx
    )
    assert resource.ok
    assert resource.metadata["more"]
    assert "[more: offset=80]" in resource.content


async def test_resource_paths_and_override_precedence(tmp_path):
    directory = tmp_path / ".kalash" / "skills" / "pdf"
    refs = directory / "references"
    refs.mkdir(parents=True)
    (directory / "SKILL.md").write_text("---\nname: pdf\ndescription: Project PDF\n---\nLOCAL")
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    (refs / "linked.md").symlink_to(outside)
    (refs / "one.md").write_text("A" * 100)
    loader = SkillLoader(tmp_path)
    assert loader.discover_now()["pdf"].source == "project"
    assert loader.list_resources("pdf") == ["references/one.md"]
    assert loader.read_resource("pdf", "references/one.md", offset=20, limit=10) == ("A" * 10, True)
    ctx = ToolContext(session_id="s", run_id="r", cwd=tmp_path)
    tool = SkillTool()
    for path in ("references/linked.md", "../outside.txt", str(outside)):
        outcome = await tool.execute(SkillParams(name="pdf", resource=path), ctx)
        assert not outcome.ok
        assert "private" not in outcome.content


async def test_resource_read_does_not_replace_retained_skill(tmp_path):
    host = make_host(tmp_path, ui=AutoApprover())
    await host.execute_result("skill", {"name": "data"}, tool_use_id="body")
    before = dict(host.loaded_skills)
    await host.execute_result(
        "skill", {"name": "data", "resource": "references/workflow.md"}, tool_use_id="resource"
    )
    assert host.loaded_skills == before


async def test_skill_restore_uses_recorded_bounded_body(tmp_path):
    host = make_host(tmp_path, ui=AutoApprover())
    outcome = await host.execute_result("skill", {"name": "data"}, tool_use_id="body")
    restored = make_host(tmp_path, ui=AutoApprover())
    restored.restore_tools(
        [
            Message(
                Role.ASSISTANT, [ToolUseBlock(id="body", name="skill", input={"name": "data"})]
            ),
            Message(Role.USER, [ToolResultBlock(tool_use_id="body", content=outcome.content)]),
        ]
    )
    assert restored.loaded_skills == host.loaded_skills
