"""Tool protocol, schema generation, and result envelope.

Every tool returns a ToolEnvelope — never raises to the caller.
Side effects are declared statically and recorded at runtime.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SideEffect(enum.Enum):
    """Declared side-effect level of a tool invocation."""

    NONE = "none"
    READ = "read"
    WRITE = "write"
    EXEC = "exec"


# ---------------------------------------------------------------------------
# Supporting dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TruncationInfo:
    """Metadata about output truncation."""

    total_lines: int
    shown_lines: int
    total_bytes: int
    retrieval_hint: str = ""


@dataclass(frozen=True, slots=True)
class ToolError:
    """Structured error within a ToolEnvelope."""

    code: str
    message: str
    recoverable: bool = False
    remediation: str = ""


@dataclass(frozen=True, slots=True)
class SideEffectRecord:
    """Record of a side effect that occurred during execution."""

    kind: str  # created | modified | deleted | executed
    path: str
    digest: str = ""


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Contextual information passed to every tool execution."""

    session_id: str
    run_id: str
    cwd: Path
    writable_roots: tuple[Path, ...] = ()
    capabilities: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolEnvelope:
    """Immutable result from any tool execution.

    Tools never raise to the caller — they return an envelope with
    ok=False and a populated error field on failure.
    """

    ok: bool
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    truncation: TruncationInfo | None = None
    error: ToolError | None = None
    side_effects: tuple[SideEffectRecord, ...] = ()

    @staticmethod
    def success(
        content: str = "",
        *,
        metadata: dict[str, Any] | None = None,
        truncated: bool = False,
        truncation: TruncationInfo | None = None,
        side_effects: tuple[SideEffectRecord, ...] = (),
    ) -> ToolEnvelope:
        """Convenience constructor for successful results."""
        return ToolEnvelope(
            ok=True,
            content=content,
            metadata=metadata or {},
            truncated=truncated,
            truncation=truncation,
            side_effects=side_effects,
        )

    @staticmethod
    def fail(
        code: str,
        message: str,
        *,
        recoverable: bool = False,
        remediation: str = "",
    ) -> ToolEnvelope:
        """Convenience constructor for failures."""
        return ToolEnvelope(
            ok=False,
            error=ToolError(
                code=code,
                message=message,
                recoverable=recoverable,
                remediation=remediation,
            ),
        )


# ---------------------------------------------------------------------------
# Tool protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Tool(Protocol):
    """Protocol that all tools must satisfy.

    Tools declare their metadata as class-level attributes and implement
    an async execute() method that returns a ToolEnvelope.
    """

    @property
    def name(self) -> str:
        """Unique tool name (e.g. 'read', 'shell')."""
        ...

    @property
    def version(self) -> str:
        """SemVer tool version."""
        ...

    @property
    def description(self) -> str:
        """Human-readable description for LLM schema."""
        ...

    @property
    def params(self) -> type[BaseModel]:
        """Pydantic model describing accepted parameters."""
        ...

    @property
    def side_effect(self) -> SideEffect:
        """Declared static side-effect level."""
        ...

    @property
    def capabilities(self) -> frozenset[str]:
        """Static capabilities required to use this tool."""
        ...

    @property
    def timeout_s(self) -> float:
        """Default timeout in seconds."""
        ...

    @property
    def max_output_bytes(self) -> int:
        """Maximum output size before truncation."""
        ...

    @property
    def idempotent(self) -> bool:
        """Whether repeated calls with same args are safe."""
        ...

    @property
    def cancellable(self) -> bool:
        """Whether the tool supports cooperative cancellation."""
        ...

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:
        """Capabilities needed based on specific arguments (runtime)."""
        ...

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        """Execute the tool and return a result envelope.

        Must never raise — all errors go into ToolEnvelope.error.
        """
        ...


# ---------------------------------------------------------------------------
# Schema generation helpers
# ---------------------------------------------------------------------------


def tool_json_schema(tool: Tool) -> dict[str, Any]:
    """Generate JSON Schema for a tool's parameters (for LLM function calling)."""
    schema = tool.params.model_json_schema()
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": schema,
    }


def tools_manifest(tools: list[Tool]) -> list[dict[str, Any]]:
    """Generate a complete tools manifest for schema negotiation."""
    return [tool_json_schema(t) for t in tools]
