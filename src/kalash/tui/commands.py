"""Slash command dispatch and session diagnostics."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from kalash.tui.messages import SystemMessage, WelcomeBanner

if TYPE_CHECKING:
    from kalash.tui.app import KalashApp


async def run_command(app: KalashApp, text: str) -> None:
    from kalash.tui.app import SLASH_COMMANDS, InputIntent

    command = text.split()[0].lower()

    if command == "/exit":
        app.exit()
    elif command == "/help":
        await app._post(SystemMessage([f"{c:<10} {d}" for c, d in SLASH_COMMANDS]))
    elif command == "/clear":
        # Guard against accidental erasure of a multi-hour session (U-5).
        transcript = app._transcript()
        children = list(transcript.children)
        if len(children) > 1 and not getattr(app, "_clear_confirmed", False):
            app._clear_confirmed = True
            await app._post(
                SystemMessage(
                    "⚠ this will erase the entire transcript. run /clear again to confirm.",
                    error=True,
                )
            )
            return
        app._clear_confirmed = False
        await transcript.remove_children()
        app._banner_removed = True
    elif command == "/new":
        if app._stream_worker is not None:
            app._stream_worker.cancel()
        if app._agent is not None:
            app._agent.cancel()
            await app._agent.close()
            app._agent = None
            app._agent_signature = ""
        await app._transcript().remove_children()
        await app._transcript().mount(WelcomeBanner())
        app._banner_removed = False
        app._history.clear()
    elif command == "/status":
        await app._post(SystemMessage(app._status_lines()))
    elif command == "/connect":
        await app._start_connect()
    elif command == "/key":
        if app.provider_id:
            from kalash.models.catalog import get_provider

            provider = get_provider(app.provider_id)
            if provider and provider.requires_key:
                app._pending_provider = app.provider_id
                app._ask_for(InputIntent.API_KEY, f"paste your new {provider.name} API key")
            else:
                await app._post(SystemMessage("current provider does not require an API key"))
        else:
            await app._post(SystemMessage("no provider connected — run /connect", error=True))
    elif command == "/theme":
        await app._start_theme()
    elif command == "/models":
        await app._start_models()
    elif command == "/mode":
        app.action_toggle_mode()
        await app._post(SystemMessage(f"mode → {app.mode}"))
    elif command in {"/copy", "/cp"}:
        parts = text.split(maxsplit=1)
        subcmd = parts[1].lower().strip() if len(parts) > 1 else "reply"
        if subcmd in {"code", "block"}:
            await app._post(SystemMessage(app._copy_last_code()))
        elif subcmd in {"transcript", "all"}:
            await app._post(SystemMessage(app._copy_full_transcript()))
        else:
            await app._post(SystemMessage(app._copy_last_reply()))
    elif command == "/export":
        await app._post(SystemMessage(app._export_transcript()))
    elif command == "/plan":
        await app._show_plan_widget()
    elif command == "/tools":
        await app._post(SystemMessage(app._tool_lines()))
    elif command == "/mcp":
        await app._post(SystemMessage(app._mcp_lines()))
    elif command == "/skills":
        await app._post(SystemMessage(app._skill_lines()))
    elif command == "/notes":
        await app._post(SystemMessage(app._scratch_text(notes=True)))
    elif command == "/scratch":
        await app._post(SystemMessage(app._scratch_text(notes=False)))
    elif command == "/cost":
        await app._post(SystemMessage(app._cost_lines()))
    elif command == "/sessions":
        await app._start_sessions()
    elif command == "/init":
        await app._post(SystemMessage(app._write_starter_instructions()))
    else:
        await app._post(SystemMessage(f"unknown command: {command}", error=True))


async def show_plan_widget(app: KalashApp) -> None:
    from kalash.tools.todo import get_task_list
    from kalash.tui.messages import PlanMessage, SystemMessage

    agent = app._agent
    if agent is None:
        await app._post(SystemMessage("no session active — send a message to begin"))
        return
    task_list = get_task_list(agent.session_id)
    if task_list and task_list.tasks:
        tasks_data = [
            {"description": t.description, "completed": t.completed} for t in task_list.tasks
        ]
        await app._post(PlanMessage(description=task_list.description, tasks=tasks_data))
    else:
        await app._post(SystemMessage(app._plan_text()))


def status_lines(app: KalashApp) -> list[str]:
    from kalash.runtime.scratchpad import get_scratchpad

    lines = [
        f"mode      {app.mode}",
        f"theme     {app._theme.label}",
        f"provider  {app.provider_id or '—'}",
        f"model     {app.model_id or '—'}",
        f"cwd       {os.getcwd()}",
    ]
    agent = app._agent
    if agent is None:
        lines.append("session   not started (send a message to begin)")
        return lines

    lines.append(f"session   {agent.session_id}")
    lines.append(f"sandbox   {agent.host.sandbox_mode}")
    lines.append(f"tools     {len(agent.host.schemas())} available")
    if agent.instructions:
        lines.append(f"KALASH.md {len(agent.instructions)} file(s) loaded")
    stats = get_scratchpad(agent.session_id).stats()
    lines.append(f"scratch   {stats['refs']} refs")

    from kalash.tools.todo import get_task_list

    task_list = get_task_list(agent.session_id)
    if task_list and task_list.tasks:
        done = sum(1 for t in task_list.tasks if t.completed)
        lines.append(f"plan      {done}/{len(task_list.tasks)} complete  (/plan)")
    else:
        lines.append("plan      none recorded")
    return lines


def tool_lines(app: KalashApp) -> list[str]:
    agent = app._agent
    if agent is None:
        from kalash.tools.builtins import core_tools

        return [f"{t.name:<12} {t.description[:60]}" for t in core_tools()]
    return [f"{s['name']:<12} {s['description'][:60]}" for s in agent.host.schemas()]


def mcp_lines(app: KalashApp) -> list[str]:
    try:
        from kalash.mcp.registry import MCPRegistry

        servers = MCPRegistry().list_servers()
    except Exception as exc:
        return [f"could not load MCP config: {exc}"]
    if not servers:
        return ["no MCP servers configured — kalash mcp add <name> <url>"]
    lines = []
    for server in servers:
        lines.append(
            f"{server.name:<16} {server.transport:<8} {server.url or '—'}  ({server.scope})"
        )
    return lines


def skill_lines(app: KalashApp) -> list[str]:
    try:
        from kalash.skills.loader import SkillLoader

        entries = SkillLoader().discover_now()
    except Exception as exc:
        return [f"could not load skills: {exc}"]
    if not entries:
        return ["no skills found — kalash skills new <name>"]
    lines = []
    for entry in sorted(entries.values(), key=lambda e: e.metadata.name):
        lines.append(f"{entry.metadata.name:<20} {entry.metadata.description[:60]}")
    return lines


def scratch_text(app: KalashApp, *, notes: bool) -> str:
    from kalash.runtime.scratchpad import get_scratchpad

    agent = app._agent
    if agent is None:
        return "no session yet"
    pad = get_scratchpad(agent.session_id)
    text = pad.render_notes() if notes else pad.render_index()
    return text or ("no notes recorded" if notes else "scratchpad is empty")


def cost_lines(app: KalashApp) -> list[str]:
    agent = app._agent
    if agent is None:
        return ["no session yet"]
    budget = agent.budget
    return [
        f"tokens    {budget.tokens_used:,} / {budget.max_tokens:,}",
        f"turns     {budget.turns_used} / {budget.max_turns}",
        f"tools     {budget.tool_calls_used} / {budget.max_tool_calls}",
        f"cost      ${budget.cost_used} / ${budget.max_cost}",
    ]


def write_starter_instructions(app: KalashApp) -> str:

    target = Path.cwd() / "KALASH.md"
    if target.exists():
        return f"{target} already exists — edit it directly"
    target.write_text(
        "# Project instructions\n\n"
        "Kalash reads this file on every session in this directory.\n\n"
        "## Commands\n\n"
        "- build:\n- test:\n- lint:\n\n"
        "## Conventions\n\n"
        "- \n",
        encoding="utf-8",
    )
    return f"created {target} — describe your build, test, and conventions there"
