"""Built-in tools in stable registration order."""

from __future__ import annotations

from kalash.tools.base import Tool
from kalash.tools.discovery import ToolSearchTool
from kalash.tools.fs import (
    EditTool,
    GlobTool,
    ListTool,
    MultiEditTool,
    ReadTool,
    WriteTool,
)
from kalash.tools.registry import ToolRegistry
from kalash.tools.scratch import ExpandTool, NoteTool
from kalash.tools.search import SearchTool
from kalash.tools.shell import ShellTool
from kalash.tools.skill import SkillTool
from kalash.tools.todo import TodoTool
from kalash.tools.web import FetchTool, WebSearchTool


def core_tools() -> list[Tool]:
    """Tools that work today, in the order they appear to the model.

    Ordering is not cosmetic: the schema block sits in the cached prefix, so it
    must be byte-stable across turns within a session. A set or a dict iteration
    order that varies between processes would silently break prompt caching.
    """
    return [
        # Reading and navigating
        ReadTool(),
        GlobTool(),
        SearchTool(),
        ListTool(),
        # Changing files
        WriteTool(),
        EditTool(),
        MultiEditTool(),
        # Running things
        ShellTool(),
        # Outside knowledge
        FetchTool(),
        WebSearchTool(),
        # Working state
        TodoTool(),
        NoteTool(),
        ExpandTool(),
        # Extensibility
        SkillTool(),
        ToolSearchTool(),
    ]


def memory_tools() -> list[Tool]:
    """Long-term memory tools backed by the local memory provider."""
    from kalash.tools.memory_tools import ForgetTool, RecallTool, RememberTool

    return [RecallTool(), RememberTool(), ForgetTool()]


def task_tools() -> list[Tool]:
    """Subagent delegation via the task tool."""
    from kalash.tools.task import TaskTool

    return [TaskTool()]


def default_registry(
    *,
    include_memory: bool = True,
    include_task: bool = True,
) -> ToolRegistry:
    """Build a registry populated with the built-in tools.

    ``include_task`` now defaults on: subagent execution is wired, so ``task``
    delegates for real instead of reporting work as queued and doing nothing.
    """
    registry = ToolRegistry()
    tools = list(core_tools())
    if include_memory:
        tools.extend(memory_tools())
    if include_task:
        tools.extend(task_tools())
    for tool in tools:
        registry.register(tool, source="builtin")
    return registry
