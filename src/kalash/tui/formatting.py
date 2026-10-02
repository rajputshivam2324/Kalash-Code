"""Compact labels for tool calls and observations."""

from __future__ import annotations

from typing import Any


def summarize_tool_call(name: str, arguments: dict[str, Any]) -> str:
    """One-line headline for a tool call, Cursor/OpenCode style."""
    if name == "shell":
        command = str(arguments.get("command", "")).strip().replace("\n", " ")
        return f"$ {command[:96]}" if command else "$"
    if name in ("write", "edit", "multi_edit"):
        path = str(arguments.get("path") or arguments.get("file") or "").strip()
        verb = "Edited" if name != "write" else "Wrote"
        return f"{verb} {_short_label(path)}" if path else name
    if name == "read":
        path = str(arguments.get("path") or "").strip()
        return f"Read {_short_label(path)}" if path else "Read"
    if name == "list":
        path = str(arguments.get("path") or ".").strip()
        return f"Listed {_short_label(path)}"
    if name in ("search", "grep"):
        query = str(arguments.get("query") or arguments.get("pattern") or "").strip()
        where = str(arguments.get("path") or arguments.get("search_path") or "").strip()
        target = f" in {_short_label(where)}" if where else ""
        return f'Grepped "{query[:48]}"{target}' if query else "Grepped"
    if name == "glob":
        pattern = str(arguments.get("pattern") or "").strip()
        return f"Globbed {pattern}" if pattern else "Globbed"
    if name == "web_search":
        query = str(arguments.get("query") or "").strip()
        return f'Searched web "{query[:48]}"' if query else "Web search"
    if name == "fetch":
        url = str(arguments.get("url") or "").strip()
        return f"Fetched {url[:70]}" if url else "Fetch"
    if name == "note":
        return f"Note  {str(arguments.get('text', ''))[:70]}"
    if name == "todo":
        return f"Todo  {str(arguments.get('command', 'update'))}"
    for key in ("path", "file", "pattern", "url", "query", "ref"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return f"{name}  {value.strip()[:80]}"
    return name


def _short_label(path: str) -> str:
    from pathlib import Path

    if not path:
        return "."
    try:
        return str(Path(path).relative_to(Path.cwd()))
    except (ValueError, OSError):
        parts = Path(path).parts
        return str(Path(*parts[-2:])) if len(parts) > 2 else path


def summarize_tool_result(name: str, data: dict[str, Any]) -> str:
    """One-line result under a folded tool block."""
    if name == "search":
        count = data.get("match_count")
        if count is not None:
            return f"Found {count} match{'es' if count != 1 else ''}"
    if name == "glob":
        count = data.get("count")
        if count is not None:
            return f"{count} file{'s' if count != 1 else ''}"
    if name == "list":
        count = data.get("entry_count")
        if count is not None:
            return f"{count} entries"
    if name == "web_search":
        count = data.get("count")
        if count is not None:
            return f"{count} results"
    warning = data.get("sandbox_warning")
    if warning:
        return str(warning)[:120]
    if data.get("sandboxed") is False and data.get("command"):
        return "runs unwrapped (OS sandbox unavailable)"
    return ""
