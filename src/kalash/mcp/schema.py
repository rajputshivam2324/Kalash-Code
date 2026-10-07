"""Validate external schemas locally, without schema-network retrieval."""

from __future__ import annotations

import copy
import importlib
import json
from typing import Any


def validator_for(schema: dict[str, Any]) -> Any:
    schema = copy.deepcopy(schema)
    if len(json.dumps(schema).encode()) > 128_000:
        raise ValueError("MCP schema exceeds 128000 bytes")
    pending = [(schema, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > 64:
            raise ValueError("MCP schema exceeds nesting limit")
        if isinstance(node, dict):
            reference = node.get("$ref")
            if reference is not None and not str(reference).startswith("#"):
                raise ValueError("MCP schemas may only reference local definitions")
            pending.extend((value, depth + 1) for value in node.values())
        elif isinstance(node, list):
            pending.extend((value, depth + 1) for value in node)
    validators = importlib.import_module("jsonschema.validators")
    referencing = importlib.import_module("referencing")
    cls = validators.validator_for(schema, default=validators.Draft202012Validator)
    cls.check_schema(schema)
    return cls(schema, registry=referencing.Registry())


def validate_arguments(validator: Any, value: Any) -> Any:
    if not isinstance(value, dict):
        raise ValueError("Tool arguments must be a JSON object")
    if len(json.dumps(value).encode()) > 1_000_000:
        raise ValueError("Tool arguments exceed 1000000 bytes")
    try:
        validator.validate(value)
    except Exception as error:
        raise ValueError(f"External tool schema validation failed: {error}") from error
    return value
