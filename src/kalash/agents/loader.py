"""Load subagent definitions from ``.kalash/agents/*.md``."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kalash.core.paths import agents_dirs


@dataclass
class AgentDefinition:
    """Parsed agent frontmatter + body."""

    name: str
    path: Path
    description: str = ""
    tools: list[str] = field(default_factory=list)
    model: str | None = None
    max_turns: int = 25
    mode: str = "build"
    body: str = ""


_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip("'\"") for item in inner.split(",") if item.strip()]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.isdigit():
        return int(value)
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text.strip()
    raw_yaml, body = match.group(1), match.group(2).strip()
    meta: dict[str, Any] = {}
    for line in raw_yaml.splitlines():
        if not line.strip() or line.strip().startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = _parse_scalar(value)
    return meta, body


def load_agent_definition(name: str) -> AgentDefinition | None:
    """Find and parse an agent definition by stem name."""
    for directory in agents_dirs():
        path = directory / f"{name}.md"
        if not path.is_file():
            continue
        meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
        tools = meta.get("tools", [])
        if not isinstance(tools, list):
            tools = [str(tools)]
        max_turns = meta.get("max_turns", 25)
        try:
            max_turns = int(max_turns)
        except (TypeError, ValueError):
            max_turns = 25
        return AgentDefinition(
            name=str(meta.get("name") or name),
            path=path,
            description=str(meta.get("description") or ""),
            tools=[str(t) for t in tools],
            model=str(meta["model"]) if meta.get("model") else None,
            max_turns=max_turns,
            mode=str(meta.get("mode") or "build"),
            body=body,
        )
    return None
