"""Workspace-specific subagent definitions with project-over-user precedence."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from kalash.core.frontmatter import read_body, read_metadata
from kalash.core.paths import agents_dirs


@dataclass
class AgentDefinition:
    name: str
    path: Path
    description: str = ""
    tools: list[str] = field(default_factory=list)
    model: str | None = None
    max_turns: int = 25
    mode: str = "build"
    body: str = ""


def load_agent_definition(name: str, cwd: Path | None = None) -> AgentDefinition | None:
    if not re.fullmatch(r"[a-z0-9-]{1,64}", name):
        raise ValueError("Agent name must match [a-z0-9-] (1–64 characters)")
    for directory in reversed(agents_dirs(cwd)):
        path = directory / f"{name}.md"
        if not path.is_file():
            continue
        metadata = read_metadata(path)
        tools = metadata.get("tools") or []
        if not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
            raise ValueError(f"Agent tools must be a list of names: {path}")
        max_turns = int(metadata.get("max_turns", 25))
        mode = str(metadata.get("mode", "build"))
        if not 1 <= max_turns <= 200 or mode not in {"plan", "build"}:
            raise ValueError(f"Invalid agent mode or max_turns: {path}")
        return AgentDefinition(
            name=name,
            path=path,
            description=str(metadata.get("description") or ""),
            tools=tools,
            model=str(metadata["model"]) if metadata.get("model") else None,
            max_turns=max_turns,
            mode=mode,
            body=read_body(path),
        )
    return None
