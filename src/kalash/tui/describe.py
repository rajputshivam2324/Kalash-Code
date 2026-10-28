"""Human-friendly one-line descriptions of tool calls and their results."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# (verb, target) shown in a tool block's header. The model-facing names stay unchanged.
_VERBS = {
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "glob": "Glob",
    "grep": "Grep",
    "bash": "Run",
    "job_output": "Job output",
    "job_kill": "Stop job",
    "web_fetch": "Fetch",
    "todo_write": "Tasks",
    "task": "Task",
    "skill": "Skill",
    "exit_plan_mode": "Plan",
}


def _short(value: Any, limit: int = 80) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def relative_path(path: str, cwd: Path | None) -> str:
    """A path as short as it can honestly be: relative to the working directory, else ``~/...``."""
    if not path or not path.startswith("/"):
        return path
    target = Path(path)
    for base, prefix in ((cwd, ""), (Path.home(), "~/")):
        if base is None:
            continue
        try:
            rel = target.relative_to(base)
        except ValueError:
            continue
        return (prefix + str(rel)) if str(rel) != "." else (prefix or ".")
    return path


def pretty_action(summary: str) -> str:
    """An approval summary for people: ``mcp__time__now {...}`` -> ``MCP time · now  {...}``."""
    if summary.startswith("mcp__"):
        head, _, rest = summary.partition(" ")
        parts = head.split("__", 2)
        if len(parts) == 3:
            return f"MCP {parts[1]} · {parts[2]}" + (f"  {rest}" if rest else "")
    return summary


def mcp_server_of(action: str) -> str | None:
    """The server in ``mcp__server__tool...`` (a tool call or a denial line), if it is one."""
    if action.startswith("mcp__"):
        parts = action.split(" ", 1)[0].split(":", 1)[0].split("__", 2)
        return parts[1] if len(parts) == 3 else None
    return None


def command_preview(args: dict[str, Any], limit: int = 100) -> str:
    """The first line of a bash command, shortened."""
    lines = str(args.get("command", "")).strip().splitlines()
    return _short(lines[0] if lines else "", limit)


def command_hint(name: str, args: dict[str, Any]) -> str:
    """The raw command to show (dim) after a description, or '' when the header already is it."""
    if name == "bash" and _short(args.get("description", "")):
        return command_preview(args, 70)
    return ""


def header_parts(name: str, args: dict[str, Any], cwd: Path | None = None) -> tuple[str, str]:
    """(verb, target) for the header, e.g. ("Edit", "src/app.py")."""
    if name == "bash":
        described = _short(args.get("description", ""), 100)
        if described:  # the model's own one-line summary reads better than a pipeline
            return "Run", described
        return "Run", command_preview(args, 100)
    if name in ("read", "write", "edit"):
        return _VERBS[name], relative_path(str(args.get("file_path", "")), cwd)
    if name == "glob":
        return "Glob", str(args.get("pattern", ""))
    if name == "grep":
        where = f"  in {relative_path(str(args['path']), cwd)}" if args.get("path") else ""
        return "Grep", f"{args.get('pattern', '')}{where}"
    if name == "web_fetch":
        return "Fetch", str(args.get("url", ""))
    if name == "task":
        return "Task", _short(args.get("description", ""))
    if name in ("job_output", "job_kill"):
        return _VERBS[name], str(args.get("job_id", ""))
    if name == "skill":
        return "Skill", str(args.get("name", ""))
    if name == "todo_write":
        todos = args.get("todos") or []
        done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
        return "Tasks", f"{done}/{len(todos)} done"
    if name == "exit_plan_mode":
        return "Plan", "ready for review"
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        server, tool = (parts[1], parts[2]) if len(parts) == 3 else ("mcp", name)
        preview = _short(json.dumps(args, sort_keys=True), 60) if args else ""
        return f"{server}", f"{tool} {preview}".strip()
    preview = _short(json.dumps(args, sort_keys=True), 70) if args else ""
    return _VERBS.get(name, name), preview


_READ_TOTAL = re.compile(r"total (\d+) lines|of (\d+)\)")


def result_meta(name: str, text: str, is_error: bool) -> str:
    """A short, dim suffix summarising a finished call ("120 lines", "exit 1"...)."""
    body = text.strip()
    if is_error:
        return ""
    if name == "read":
        match = _READ_TOTAL.search(body)
        total = (match.group(1) or match.group(2)) if match else None
        return f"{total} lines" if total else ""
    if name == "glob":
        if body.startswith("No files"):
            return "no matches"
        lines = [ln for ln in body.splitlines() if ln and not ln.startswith("(")]
        return f"{len(lines)} files"
    if name == "grep":
        if body.startswith("No matches"):
            return "no matches"
        lines = [ln for ln in body.splitlines() if ln and not ln.startswith("(")]
        return f"{len(lines)} matches"
    if name == "bash":
        match = re.search(r"\[exit code: (-?\d+)\]\s*$", body)
        return f"exit {match.group(1)}" if match and match.group(1) != "0" else ""
    if name == "web_fetch":
        return f"{len(body):,} chars"
    return ""


def error_hint(code: str | None, message: str | None) -> str:
    """The next step for a failed turn, by error code (never by parsing message text)."""
    hints = {
        "AUTH": "Check the API key: /provider, or set the provider's environment variable.",
        "QUOTA": "The provider reports no quota or a spend limit. Check billing, then retry.",
        "RATE_LIMIT": "Still rate limited after retries. Wait a moment or try /model.",
        "CONTEXT_OVERFLOW": "The conversation no longer fits. Try /compact or start with /new.",
        "INVALID_REQUEST": "The provider rejected the request; its message is shown above.",
        "INVALID_TOOL_CALL": "The model wrote a tool call the provider rejected. Try again.",
        "SERVER": "The provider had a problem. Try again in a moment, or switch with /model.",
        "TIMEOUT": "The provider stopped responding. Try again, or switch with /model.",
        "TRANSPORT": "The connection dropped. Check your network and try again.",
        "EMPTY_RESPONSE": "The model returned nothing. Try again, or rephrase.",
        "MALFORMED_RESPONSE": "The provider sent something unexpected. Try again or /model.",
        "LIMIT_STEPS": "Retry to continue. Current sessions have no spending or step caps.",
        "LIMIT_TOKENS": "Retry to continue. Current sessions have no spending or step caps.",
        "LIMIT_COST": "Retry to continue. Current sessions have no spending or step caps.",
        "STUCK": "Tell the model what to try instead, undo with /undo, or switch with /model.",
    }
    return hints.get(code or "", "")
