"""The slash command registry: names, sections, aliases, ranking.

Typing just ``/`` lists *everything*, grouped into sections and scrollable - modes first, because
switching mode is the thing people reach for most - instead of an arbitrary first dozen. Typing
more ranks by name prefix, alias prefix, name substring, name subsequence ("pms" finds
``permissions``) and, from three characters, the description.
"""

from __future__ import annotations

from dataclasses import dataclass

SECTIONS = ("Modes", "Session", "Model & keys", "Info & tools", "Custom commands", "Skills")


@dataclass(frozen=True)
class SlashCommand:
    name: str
    description: str
    args: str = ""
    aliases: tuple[str, ...] = ()
    during_turn: bool = False  # may run while the agent is working
    section: str = "Info & tools"
    mode: str | None = None  # a permission-mode shortcut: the mode id it switches to


def builtin_commands() -> list[SlashCommand]:
    """Every built-in command, in the order the ``/`` menu shows them."""
    modes, session, setup, info = "Modes", "Session", "Model & keys", "Info & tools"
    return [
        SlashCommand("ask", "edit files in the workspace; risky actions ask first", "",
                     ("default", "edit"), True, modes, "ask"),
        SlashCommand("auto", "no prompts; protected paths and escalations are denied", "",
                     (), True, modes, "auto"),
        SlashCommand("plan", "research only, then review a plan before anything changes", "",
                     (), True, modes, "plan"),
        SlashCommand("read-only", "inspect only; every change is refused", "",
                     ("readonly",), True, modes, "read-only"),
        SlashCommand("full", "no sandbox and no prompts (asks you to confirm)", "",
                     ("yolo", "danger"), True, modes, "full"),
        SlashCommand("permissions", "pick a permission mode", "[ask|auto|plan|read-only|full]",
                     ("mode",), True, modes),
        SlashCommand("new", "start a fresh session", "", ("clear", "reset"), False, session),
        SlashCommand("resume", "resume an earlier session", "[id]",
                     ("sessions", "continue"), False, session),
        SlashCommand("compact", "summarise older context now", "", (), False, session),
        SlashCommand("undo", "revert the agent's last file changes", "",
                     ("rewind",), False, session),
        SlashCommand("diff", "show git changes and files the agent edited", "",
                     (), True, session),
        SlashCommand("copy", "copy the last answer", "", (), True, session),
        SlashCommand("export", "write the conversation to Markdown", "[path]",
                     (), False, session),
        SlashCommand("rename", "name this session", "[name]", (), True, session),
        SlashCommand("init", "create AGENTS.md for this project", "", (), False, session),
        SlashCommand("model", "switch model", "[provider/model]", (), True, setup),
        SlashCommand("effort", "set reasoning effort (this model's levels)",
                     "[level|auto|off]", (), True, setup),
        SlashCommand("provider", "add or replace a provider's API key", "[provider]",
                     ("login",), False, setup),
        SlashCommand("theme", "change colours", "[name]", (), True, setup),
        SlashCommand("status", "model, mode, sandbox, session", "", (), True, info),
        SlashCommand("context", "how full the context window is", "", (), True, info),
        SlashCommand("cost", "token and cost totals", "", ("usage",), True, info),
        SlashCommand("todos", "show the agent's task list", "", (), True, info),
        SlashCommand("memory", "edit AGENTS.md in $EDITOR", "", (), False, info),
        SlashCommand("mcp", "connected MCP servers", "", (), True, info),
        SlashCommand("skills", "available skills", "", (), True, info),
        SlashCommand("verbose", "show or hide tool details", "", ("details",), True, info),
        SlashCommand("doctor", "check the sandbox", "", (), True, info),
        SlashCommand("help", "keys, modes and commands", "", ("?",), True, info),
        SlashCommand("quit", "exit Kalash", "", ("exit", "q"), True, info),
    ]  # fmt: skip


def grouped(commands: list[SlashCommand]) -> list[tuple[str, list[SlashCommand]]]:
    """Commands bucketed by section, sections in menu order, empty ones dropped."""
    return [
        (section, [c for c in commands if c.section == section])
        for section in SECTIONS
        if any(c.section == section for c in commands)
    ]


def _subsequence(needle: str, haystack: str) -> bool:
    """Do the characters of ``needle`` appear in ``haystack`` in order?"""
    position = 0
    for char in needle:
        position = haystack.find(char, position) + 1
        if position == 0:
            return False
    return True


def match_commands(query: str, commands: list[SlashCommand]) -> list[SlashCommand]:
    """Rank commands for a typed query; the empty query is every command in menu order."""
    q = query.lower().strip()
    if not q:
        return list(commands)
    tiers: list[list[SlashCommand]] = [[], [], [], [], []]
    for command in commands:
        name = command.name.lower()
        if name.startswith(q):
            tiers[0].append(command)
        elif any(alias.startswith(q) for alias in command.aliases):
            tiers[1].append(command)
        elif q in name:
            tiers[2].append(command)
        elif len(q) >= 2 and _subsequence(q, name):
            tiers[3].append(command)
        elif len(q) >= 3 and q in command.description.lower():
            tiers[4].append(command)
    return [command for tier in tiers for command in tier]
