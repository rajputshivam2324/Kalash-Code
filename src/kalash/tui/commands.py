"""Slash command dispatch and session diagnostics."""

from __future__ import annotations

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
        await app._post(SystemMessage([f"{c:<10} {d}" for c, d in SLASH_COMMANDS]))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    elif command == "/clear":
        # Guard against accidental erasure of a multi-hour session (U-5).
        transcript = app._transcript()  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        children = list(transcript.children)
        if len(children) > 1 and not getattr(app, "_clear_confirmed", False):
            app._clear_confirmed = True  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
            await app._post(  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
                SystemMessage(
                    "⚠ this will erase the entire transcript. run /clear again to confirm.",
                    error=True,
                )
            )
            return
        app._clear_confirmed = False  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        await transcript.remove_children()
        transcript.clear_archive()
        app._banner_removed = True  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    elif command == "/new":
        if app._stream_worker is not None:  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
            app._stream_worker.cancel()  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        if app._agent is not None:  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
            app._agent.cancel()  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
            await app._agent.close()  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
            app._agent = None  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
            app._agent_signature = ""  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        await app._transcript().remove_children()  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        app._transcript().clear_archive()  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        await app._transcript().mount(WelcomeBanner())  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        app._banner_removed = False  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        app._history.clear()  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    elif command == "/status":
        await app._post(SystemMessage(app._status_lines()))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    elif command == "/connect":
        await app._start_connect()  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    elif command == "/key":
        if app.provider_id:
            from kalash.models.catalog import get_provider

            provider = get_provider(app.provider_id)
            if provider and provider.requires_key:
                app._pending_provider = app.provider_id  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
                app._ask_for(InputIntent.API_KEY, f"paste your new {provider.name} API key")  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
            else:
                await app._post(SystemMessage("current provider does not require an API key"))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        else:
            await app._post(SystemMessage("no provider connected — run /connect", error=True))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    elif command == "/theme":
        await app._start_theme()  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    elif command == "/models":
        await app._start_models()  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    elif command == "/mode":
        app.action_toggle_mode()
        await app._post(SystemMessage(f"mode → {app.mode}"))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    elif command in {"/copy", "/cp"}:
        parts = text.split(maxsplit=1)
        subcmd = parts[1].lower().strip() if len(parts) > 1 else "reply"
        if subcmd in {"code", "block"}:
            await app._post(SystemMessage(app._copy_last_code()))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        elif subcmd in {"transcript", "all"}:
            await app._post(SystemMessage(app._copy_full_transcript()))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        else:
            await app._post(SystemMessage(app._copy_last_reply()))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    elif command == "/export":
        await app._post(SystemMessage(app._export_transcript()))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    elif command == "/plan":
        await app._show_plan_widget()  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    elif command == "/tools":
        await app._post(SystemMessage(app._tool_lines()))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    elif command == "/mcp":
        await app._post(SystemMessage(app._mcp_lines()))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    elif command == "/skills":
        await app._post(SystemMessage(app._skill_lines()))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    elif command == "/notes":
        await app._post(SystemMessage(app._scratch_text(notes=True)))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    elif command == "/scratch":
        await app._post(SystemMessage(app._scratch_text(notes=False)))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    elif command == "/cost":
        await app._post(SystemMessage(app._cost_lines()))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    elif command == "/sessions":
        await app._start_sessions()  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    elif command == "/init":
        await app._post(SystemMessage(app._write_starter_instructions()))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    else:
        await app._post(SystemMessage(f"unknown command: {command}", error=True))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.


async def show_plan_widget(app: KalashApp) -> None:
    from kalash.tools.todo import get_task_list
    from kalash.tui.messages import PlanMessage, SystemMessage

    agent = app._agent  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    if agent is None:
        await app._post(SystemMessage("no session active — send a message to begin"))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        return
    task_list = get_task_list(agent.session_id)
    if task_list and task_list.tasks:
        tasks_data = [
            {"description": t.description, "completed": t.completed} for t in task_list.tasks
        ]
        await app._post(PlanMessage(description=task_list.description, tasks=tasks_data))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    else:
        await app._post(SystemMessage(app._plan_text()))  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.


def status_lines(app: KalashApp) -> list[str]:
    from kalash.runtime.scratchpad import get_scratchpad

    lines = [
        f"mode      {app.mode}",
        f"theme     {app._theme.label}",  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
        f"provider  {app.provider_id or '—'}",
        f"model     {app.model_id or '—'}",
        f"cwd       {Path.cwd()}",
    ]
    agent = app._agent  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
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
    agent = app._agent  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    if agent is None:
        from kalash.tools.builtins import core_tools

        return [f"{t.name:<12} {t.description[:60]}" for t in core_tools()]
    return [f"{s['name']:<12} {s['description'][:60]}" for s in agent.host.schemas()]


def mcp_lines(app: KalashApp) -> list[str]:  # noqa: ARG001 - Preserve callback/keyword compatibility.
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


def skill_lines(app: KalashApp) -> list[str]:  # noqa: ARG001 - Preserve callback/keyword compatibility.
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

    agent = app._agent  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    if agent is None:
        return "no session yet"
    pad = get_scratchpad(agent.session_id)
    text = pad.render_notes() if notes else pad.render_index()
    return text or ("no notes recorded" if notes else "scratchpad is empty")


def cost_lines(app: KalashApp) -> list[str]:
    agent = app._agent  # noqa: SLF001 - Internal collaborator or deliberate fault-injection boundary.
    if agent is None:
        return ["no session yet"]
    budget = agent.budget
    return [
        f"tokens    {budget.tokens_used:,} / "
        + (f"{budget.max_tokens:,}" if budget.max_tokens is not None else "no cap"),
        f"turns     {budget.turns_used} / {budget.max_turns}",
        f"tools     {budget.tool_calls_used} / {budget.max_tool_calls}",
        f"cost      ${budget.cost_used} / "
        + (f"${budget.max_cost}" if budget.max_cost is not None else "no cap"),
    ]


def write_starter_instructions(app: KalashApp) -> str:  # noqa: ARG001 - Preserve callback/keyword compatibility.

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
