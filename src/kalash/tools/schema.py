"""Tool schema minification and profiles.

Two problems this solves, both discovered by running against a real small model
rather than a fake provider that accepted anything.

**Pydantic's JSON Schema is verbose.** ``model_json_schema()`` emits a ``title``
for every field (``"path"`` gets ``"title": "Path"``), a schema-level
``description`` that duplicates the tool's own description, and ``default``
values the model does not need in order to call the tool. Across 15 tools that
was 3,888 tokens of schema, most of it carrying no information the model uses.

**Not every model can afford every tool.** A provider with an 8,000
tokens-per-minute ceiling cannot receive 5,000 tokens of fixed prefix before the
user has typed anything. ``docs/context-budget.md`` specified a reduced tool
profile for small windows; :func:`select_profile` implements it.

Minification is lossless with respect to what a model needs to emit a valid
call: names, types, nesting, enums, and required-ness are all preserved.
"""

from __future__ import annotations

from typing import Any

from kalash.tools.base import Tool

# Keys that carry no information the model needs to construct a valid call.
# `title` is auto-derived from the field name, which the model already has.
# `title` is auto-derived from the field name the model already has.
# `default` is not needed to construct a call — an omitted optional field gets
# its default from Pydantic during server-side validation, so advertising it is
# duplicated information.
_DROP_KEYS: frozenset[str] = frozenset({"title", "additionalProperties", "default"})

# Field descriptions are worth keeping but not at unbounded length.
MAX_FIELD_DESCRIPTION = 100
MAX_TOOL_DESCRIPTION = 200

# The core set that can accomplish almost any coding task. Ordered for
# byte-stability across turns, which prompt caching depends on.
#
# `todo` is included deliberately even though it is one of the larger schemas:
# without it a small model has no way to plan multi-step work, so it dives
# straight into a build with no visible plan and no way to resume. Planning is
# not a luxury feature that gets dropped first under budget pressure.
MINIMAL_TOOLS: tuple[str, ...] = (
    "read",
    "write",
    "edit",
    "search",
    "shell",
    "list",
    "todo",
    # Outside knowledge is a capability users assume exists. Omitting these made
    # the agent answer "I don't have a web search tool", which is technically
    # accurate and completely unacceptable.
    "web_search",
    "fetch",
)

# Adds discovery and durable working state. Still well under the full set.
STANDARD_TOOLS: tuple[str, ...] = (
    *MINIMAL_TOOLS,
    "glob",
    "multi_edit",
    "note",
    "expand",
)


def minify_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Strip non-semantic keys from a JSON Schema, recursively."""
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _DROP_KEYS:
            continue
        if key == "description" and isinstance(value, str):
            out[key] = value[:MAX_FIELD_DESCRIPTION]
            continue
        if isinstance(value, dict):
            out[key] = minify_schema(value)
        elif isinstance(value, list):
            out[key] = [
                minify_schema(item) if isinstance(item, dict) else item
                for item in value
            ]
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
    if terse:
        description = description[:MAX_TOOL_DESCRIPTION]
    return {
        "name": tool.name,
        "description": description,
        "input_schema": raw,
    }


def select_profile(
    tools: list[Tool],
    *,
    budget_tokens: int,
    prompt_tokens: int,
    reserve_output: int = 0,
    terse: bool = True,
) -> tuple[list[Tool], str]:
    """Choose the largest tool set that fits the budget.

    Returns the selected tools and a profile name for reporting. Selection is
    by measured size rather than a guess, because schema cost varies widely per
    tool — ``todo`` alone cost more than ``read`` and ``write`` combined.

    ``reserve_output`` is subtracted before fitting. Without it the tool block
    expands to fill the whole allowance and squeezes the reply down to almost
    nothing, which is the wrong trade: an agent that can call fifteen tools but
    only emit a sentence cannot finish a task.
    """
    import json

    from kalash.core.budget import estimate_tokens

    by_name = {tool.name: tool for tool in tools}

    def cost(selection: list[Tool]) -> int:
        blob = json.dumps(
            [tool_schema(t, terse=terse) for t in selection], separators=(",", ":")
        )
        return estimate_tokens(blob)

    def resolve(names: tuple[str, ...]) -> list[Tool]:
        return [by_name[name] for name in names if name in by_name]

    available = max(0, budget_tokens - prompt_tokens - reserve_output)

    candidates: list[tuple[str, list[Tool]]] = [
        ("full", list(tools)),
        ("standard", resolve(STANDARD_TOOLS)),
        ("minimal", resolve(MINIMAL_TOOLS)),
    ]

    for name, selection in candidates:
        if selection and cost(selection) <= available:
            return selection, name

    # Even the minimal set does not fit. Return it anyway and let the caller
    # report the squeeze: a model this constrained cannot act, and saying so is
    # more useful than silently sending nothing.
    minimal = resolve(MINIMAL_TOOLS)
    return (minimal or list(tools)), "minimal-overflow"


def schema_tokens(schemas: list[dict[str, Any]]) -> int:
    """Measured token cost of a schema block."""
    import json

    from kalash.core.budget import estimate_tokens

    return estimate_tokens(json.dumps(schemas, separators=(",", ":")))
