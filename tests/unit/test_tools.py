"""Tests for the tools layer."""

import pytest

from kalash.tools.base import ToolContext, ToolEnvelope
from kalash.tools.fs import EditTool, ReadTool, WriteTool
from kalash.tools.registry import ToolRegistry
from kalash.tools.todo import TodoTool


@pytest.fixture
def ctx(tmp_path):
    """A test ToolContext with a temporary working directory."""
    return ToolContext(
        session_id="test_session",
        run_id="test_run",
        cwd=tmp_path,
        writable_roots=(tmp_path,),
        capabilities=frozenset({"fs.read", "fs.write", "shell.exec"}),
    )


# --- ToolEnvelope ---


class TestToolEnvelope:
    def test_success_factory(self):
        env = ToolEnvelope.success(content="hello", metadata={"key": "val"})
        assert env.ok is True
        assert env.content == "hello"
        assert env.metadata == {"key": "val"}
        assert env.error is None

    def test_fail_factory(self):
        env = ToolEnvelope.fail(
            code="KALASH_TOOL_ERROR",
            message="something broke",
            recoverable=True,
        )
        assert env.ok is False
        assert env.error is not None
        assert env.error.code == "KALASH_TOOL_ERROR"
        assert env.error.recoverable is True


# --- ToolRegistry ---


class TestToolRegistry:
    def test_register_and_get(self):
        registry = ToolRegistry()
        tool = ReadTool()
        registry.register(tool, source="builtin")
        assert registry.get("read") is tool

    def test_get_unknown_raises(self):
        registry = ToolRegistry()
        with pytest.raises(KeyError):
            registry.get("nonexistent")

    def test_priority_builtin_over_mcp(self):
        registry = ToolRegistry()
        builtin = ReadTool()
        # Simulate an MCP tool with same name
        mcp_tool = ReadTool()  # same name
        registry.register(builtin, source="builtin")
        registry.register(mcp_tool, source="mcp")
        assert registry.get("read") is builtin

    def test_list_tools(self):
        registry = ToolRegistry()
        registry.register(ReadTool(), source="builtin")
        registry.register(WriteTool(), source="builtin")
        tools = registry.list_tools()
        assert len(tools) == 2

    def test_list_schemas(self):
        registry = ToolRegistry()
        registry.register(ReadTool(), source="builtin")
        schemas = registry.list_schemas()
        assert len(schemas) == 1
        assert schemas[0]["name"] == "read"
        assert "parameters" in schemas[0]


# --- ReadTool ---


class TestReadTool:
    @pytest.mark.asyncio
    async def test_read_file(self, ctx, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("line 1\nline 2\nline 3\n")

        tool = ReadTool()
        from kalash.tools.fs import ReadParams

        args = ReadParams(path=str(test_file))
        result = await tool.execute(args, ctx)

        assert result.ok
        assert "line 1" in result.content
        assert "content_digest" in result.metadata

    @pytest.mark.asyncio
    async def test_read_nonexistent(self, ctx):
        tool = ReadTool()
        from kalash.tools.fs import ReadParams

        args = ReadParams(path="/nonexistent/file.txt")
        result = await tool.execute(args, ctx)

        assert not result.ok
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_read_with_offset(self, ctx, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("line 0\nline 1\nline 2\nline 3\n")

        tool = ReadTool()
        from kalash.tools.fs import ReadParams

        args = ReadParams(path=str(test_file), offset=2, limit=1)
        result = await tool.execute(args, ctx)

        assert result.ok
        assert "line 2" in result.content
        assert result.truncated  # not all lines shown


# --- WriteTool ---


class TestWriteTool:
    @pytest.mark.asyncio
    async def test_write_new_file(self, ctx, tmp_path):
        target = tmp_path / "new.txt"

        tool = WriteTool()
        from kalash.tools.fs import WriteParams

        args = WriteParams(path=str(target), content="hello world")
        result = await tool.execute(args, ctx)

        assert result.ok
        assert target.read_text() == "hello world"

    @pytest.mark.asyncio
    async def test_write_requires_digest_for_overwrite(self, ctx, tmp_path):
        target = tmp_path / "existing.txt"
        target.write_text("original")

        tool = WriteTool()
        from kalash.tools.fs import WriteParams

        # Without digest
        args = WriteParams(path=str(target), content="new content")
        result = await tool.execute(args, ctx)

        assert not result.ok
        assert "STALE_READ" in result.error.code


# --- EditTool ---


class TestEditTool:
    @pytest.mark.asyncio
    async def test_edit_exact_match(self, ctx, tmp_path):
        import hashlib

        target = tmp_path / "edit.txt"
        content = "hello world\ngoodbye world\n"
        target.write_text(content)
        digest = hashlib.sha256(content.encode()).hexdigest()

        tool = EditTool()
        from kalash.tools.fs import EditParams

        args = EditParams(
            path=str(target),
            old_str="hello world",
            new_str="hi world",
            digest=digest,
        )
        result = await tool.execute(args, ctx)

        assert result.ok
        assert target.read_text() == "hi world\ngoodbye world\n"


# --- TodoTool ---


class TestTodoTool:
    @pytest.mark.asyncio
    async def test_create_and_list(self, ctx):
        tool = TodoTool()
        from kalash.tools.todo import TaskDef, TodoParams

        # Create
        args = TodoParams(
            command="create",
            task_list_description="Test tasks",
            tasks=[TaskDef(task_description="Task 1"), TaskDef(task_description="Task 2")],
        )
        result = await tool.execute(args, ctx)
        assert result.ok
        assert "Task 1" in result.content

        # List
        args = TodoParams(command="list")
        result = await tool.execute(args, ctx)
        assert result.ok
        assert "Task 1" in result.content
        assert "Task 2" in result.content
