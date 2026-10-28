"""Permission modes: named combinations of sandbox level and approval policy.

The mode can change while a session runs (Shift+Tab, ``/permissions``, plan approval). Changes
are logged and reach the model in the next message's runtime-context block (see
``agent/context_blocks.py``), never in the system prompt, so the cached prefix is never rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Mode:
    id: str
    label: str
    sandbox: str
    approval: str
    plan: bool
    description: str
    policy: str  # what the model may do in this mode (shown in its runtime context)


MODES: dict[str, Mode] = {
    m.id: m
    for m in (
        Mode(
            "ask",
            "Ask",
            "workspace-write",
            "ask",
            False,
            "edit files in the workspace; risky actions ask first",
            "You may change files inside the workspace; commands run in the sandbox; protected "
            "paths and escalations ask the user for approval.",
        ),
        Mode(
            "auto",
            "Auto",
            "workspace-write",
            "never",
            False,
            "no prompts; protected paths and escalations are denied",
            "You may change files inside the workspace and run sandboxed commands. Nothing prompts "
            "the user: anything that would need approval (protected paths, escalation) is "
            "rejected automatically, so do not try it.",
        ),
        Mode(
            "plan",
            "Plan",
            "read-only",
            "ask",
            True,
            "research only; propose a plan with exit_plan_mode",
            "Stay in plan mode until the user approves your plan through exit_plan_mode. Research "
            "the task with read-only tools (read, glob, grep, web_fetch, read-only commands). Do "
            "NOT try to change files or run mutating commands - they will be refused. When you "
            "have a complete plan, call exit_plan_mode with it and wait for the user's decision.",
        ),
        Mode(
            "read-only",
            "Read-only",
            "read-only",
            "never",
            False,
            "inspect only; every change is refused",
            "You can inspect files and run read-only commands; every change is refused. Describe "
            "changes instead of making them.",
        ),
        Mode(
            "full",
            "Full access",
            "danger-full-access",
            "never",
            False,
            "no sandbox and no prompts (dangerous)",
            "Commands and file changes are not sandboxed and nothing prompts the user. Be careful "
            "with destructive actions.",
        ),
    )
}

CYCLE = ("ask", "auto", "plan")  # Shift+Tab; full access is only reachable explicitly
ALIASES = {
    "default": "ask",
    "edit": "ask",
    "yolo": "full",
    "danger": "full",
    "readonly": "read-only",
}


def resolve_mode(name: str) -> Mode | None:
    key = name.strip().lower()
    return MODES.get(ALIASES.get(key, key))


def initial_mode(sandbox: str, approval: str) -> str:
    """The mode matching configured settings (the closest one when the pair is unusual)."""
    if sandbox == "danger-full-access":
        return "full"
    if sandbox == "read-only":
        return "read-only"
    return "auto" if approval == "never" else "ask"


def next_mode(current: str) -> str:
    if current not in CYCLE:
        return CYCLE[0]
    return CYCLE[(CYCLE.index(current) + 1) % len(CYCLE)]
