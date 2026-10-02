"""Compact provider schemas while retaining validation rules and defaults."""

from __future__ import annotations

from typing import Any

from kalash.tools.base import Tool

# Auto-derived titles repeat field names; preserve all semantic schema fields.
_DROP_KEYS: frozenset[str] = frozenset({"title"})


def minify_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Strip non-semantic keys from a JSON Schema, recursively."""
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _DROP_KEYS:
            continue
        if isinstance(value, dict):
            out[key] = minify_schema(value)
        elif isinstance(value, list):
            out[key] = [minify_schema(item) if isinstance(item, dict) else item for item in value]
        else:
            out[key] = value
    return out


def tool_schema(tool: Tool, *, terse: bool = True) -> dict[str, Any]:
    """Build one provider-facing tool schema.

    ``input_schema`` is the Anthropic-native key; the OpenAI provider reads it
    too, so one shape serves both.
    """
    external = getattr(tool, "external_input_schema", None)
    if external is not None:
        raw = dict(external)
    else:
        raw = tool.params.model_json_schema()
    if terse:
        raw = minify_schema(raw)
        # The tool's own description already says what it does, so a
        # schema-level restatement is duplicated tokens on every request.
        raw.pop("description", None)
    description = tool.description
    return {
        "name": tool.name,
        "description": description,
        "input_schema": raw,
    }


def schema_tokens(schemas: list[dict[str, Any]]) -> int:
    """Measured token cost of a schema block."""
    import json

    from kalash.core.budget import estimate_tokens

    return estimate_tokens(json.dumps(schemas, separators=(",", ":")))
