"""Tool definitions, execution context and the registry.

A tool declares a pydantic parameter model (unknown arguments are rejected with the list
of valid ones), whether it only reads (``read_only``: may run concurrently with other
read-only calls) and a ``run`` coroutine. Policy (permissions, sandbox mode) is applied
by the agent *before* ``run``; tools never decide whether they are allowed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

from kalash.llm.types import ToolSpec

if TYPE_CHECKING:
    from kalash.tools.jobs import JobManager
    from kalash.tools.observe import ObservationTracker


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class ToolResult:
    """What the model sees. ``is_error`` marks failures so the model (and logs) can tell."""

    text: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Effects:
    """What a call would touch, declared up front so policy can decide before it runs."""

    reads: list[Path] = field(default_factory=list)
    writes: list[Path] = field(default_factory=list)
    network: bool = False
    execute: bool = False  # runs a command (confined by the OS sandbox)
    escalate: bool = False  # asks to run outside the sandbox
    url: str | None = None


@dataclass
class ToolContext:
    cwd: Path
    root: Path
    session_id: str
    observations: ObservationTracker
    jobs: JobManager
    sandbox_mode: str
    network: bool
    writable_roots: list[Path]
    spill_dir: Path
    emit_output: Callable[[str], None] = lambda _text: None  # live output for UIs
    ask_user: Callable[[str], Awaitable[str]] | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""

    def resolve(self, raw: str, base: Path | None = None) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (base or self.cwd) / path
        return Path(_normalize(path))

    def display(self, path: Path) -> str:
        """Workspace-relative spelling when inside the project root, absolute otherwise."""
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)


def _normalize(path: Path) -> str:
    import os

    return os.path.normpath(str(path))


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    params: ClassVar[type[Params]]
    read_only: ClassVar[bool] = False
    needs_approval: ClassVar[bool] = False  # always ask first (e.g. untrusted MCP tools)
    concurrent_safe: ClassVar[bool | None] = None  # None: same as read_only

    @property
    def can_run_concurrently(self) -> bool:
        return self.read_only if self.concurrent_safe is None else self.concurrent_safe

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, json_schema(self.params))

    def parse(self, arguments: dict[str, Any]) -> Params:
        return self.params.model_validate(arguments)

    def permission_key(self, params: Params, ctx: ToolContext) -> str:
        """A short string describing this call to permission rules and approval prompts."""
        return self.name

    def effects(self, params: Params, ctx: ToolContext) -> Effects:
        return Effects()

    def describe(self, params: Params, ctx: ToolContext) -> str:
        """Human-readable detail for approval prompts (a diff, the full command...)."""
        return ""

    @abstractmethod
    async def run(self, params: Any, ctx: ToolContext) -> ToolResult: ...


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Compact JSON schema: no titles, defaults dropped, ``$ref`` inlined."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return clean(defs[node["$ref"].rsplit("/", 1)[-1]])
            out = {k: clean(v) for k, v in node.items() if k not in ("title", "default")}
            if "anyOf" in out:  # Optional[X] -> X (the field is simply not required)
                options = [o for o in out["anyOf"] if o.get("type") != "null"]
                if len(options) == 1:
                    merged = {k: v for k, v in out.items() if k != "anyOf"}
                    merged.update(options[0])
                    return merged
            return out
        if isinstance(node, list):
            return [clean(v) for v in node]
        return node

    result: dict[str, Any] = clean(schema)
    result.setdefault("properties", {})
    result["additionalProperties"] = False
    return result


def validation_message(tool: Tool, error: ValidationError) -> str:
    """An actionable argument error naming the problem fields and the valid parameters."""
    lines = []
    for item in error.errors():
        where = ".".join(str(p) for p in item["loc"]) or "arguments"
        if item["type"] == "extra_forbidden":
            lines.append(f"unknown parameter '{where}'")
        elif item["type"] == "missing":
            lines.append(f"missing required parameter '{where}'")
        else:
            lines.append(f"'{where}': {item['msg']}")
    props = ", ".join(tool.params.model_json_schema().get("properties", {}))
    return f"Invalid arguments for {tool.name}: {'; '.join(lines)}. Valid parameters: {props}."


class Registry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.add(tool)

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name {tool.name!r}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values()]

    def subset(self, names: set[str]) -> Registry:
        return Registry([t for t in self._tools.values() if t.name in names])

    def without(self, names: set[str]) -> Registry:
        return Registry([t for t in self._tools.values() if t.name not in names])

    def __len__(self) -> int:
        return len(self._tools)


def compact_tool_spec(spec: ToolSpec) -> ToolSpec:
    """Shorten documentation for small windows; keep the complete callable schema."""

    def compact(node: Any) -> Any:
        if isinstance(node, dict):
            result = {}
            for key, value in node.items():
                if key == "description":
                    continue
                if key in ("properties", "$defs", "definitions", "patternProperties"):
                    result[key] = {name: compact(schema) for name, schema in value.items()}
                else:
                    result[key] = compact(value)
            return result
        if isinstance(node, list):
            return [compact(item) for item in node]
        return node

    description = spec.description.split(". ", 1)[0]
    if len(description) > 200:
        description = description[:197] + "..."
    return ToolSpec(spec.name, description, compact(spec.parameters))
