from __future__ import annotations

from typing import Annotated, Any

import typer
from rich.console import Console

from kalash.core.logging import configure_logging

configure_logging()

app = typer.Typer(name="kalash", help="Terminal-native agentic coding assistant")
console = Console()


def _launch_tui(
    pipe_mode: bool = False,
    *,
    resume_session: str | None = None,
    mode: str = "build",
) -> None:
    """Launch TUI or Pipe mode(Headess mode)"""
    if pipe_mode:
        from kalash.cli._pipe import run_pipe_mode

        run_pipe_mode(mode=mode, resume_session=resume_session)
    else:
        from kalash.tui.app import KalashApp

        tui = KalashApp(
            resume_session=resume_session,
            mode=mode,
        )
        tui.run()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    prompt: Annotated[
        str | None,
        typer.Option(
            "-p",
            "--print",
            help="Run one prompt headlessly and print the result to stdout.",
        ),
    ] = None,
    resume_id: Annotated[
        str | None,
        typer.Option(
            "--resume",
            "-r",
            help="Resume a session by id, restoring its conversation.",
        ),
    ] = None,
    continue_last: Annotated[
        bool,
        typer.Option(
            "--continue",
            "-c",
            help="Continue the most recent session in this directory.",
        ),
    ] = False,
    plan: Annotated[
        bool,
        typer.Option("--plan", help="Start in plan mode: read-only, no edits."),
    ] = False,
    output_format: Annotated[
        str,
        typer.Option(
            "--output-format",
            help="Headless output: text, json, or stream-json.",
        ),
    ] = "text",
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            "-m",
            help="Model override (provider/model-id).",
        ),
    ] = None,
    sandbox: Annotated[
        str | None,
        typer.Option(
            "--sandbox",
            help="Sandbox mode: read-only, workspace-write, danger-full-access.",
        ),
    ] = None,
    approval: Annotated[
        str | None,
        typer.Option(
            "--approval",
            help="Approval policy: untrusted, on-request, never.",
        ),
    ] = None,
) -> None:
    """Launch Kalash.

    With no arguments this opens the TUI. `-p "…"` runs a single prompt and
    prints the answer, and piped stdin is detected automatically so
    `echo "…" | kalash` also runs headlessly.
    """
    if ctx.invoked_subcommand is not None:
        return

    import sys

    from kalash.cli._pipe import run_pipe_mode

    mode = "plan" if plan else "build"
    session = resume_id if resume_id else None
    if continue_last and session is None:
        session = _most_recent_session()

    # Keyword arguments for pipe mode
    pipe_kwargs: dict[str, Any] = {
        "mode": mode,
        "resume_session": session,
        "output_format": output_format,
        "model": model,
        "sandbox": sandbox,
        "approval": approval,
    }

    if prompt is not None:
        run_pipe_mode(prompt, **pipe_kwargs)
        return

    # Piped input means there is no terminal to draw a TUI into.
    if not sys.stdin.isatty():
        run_pipe_mode(**pipe_kwargs)
        return

    _launch_tui(
        pipe_mode=False,
        resume_session=session if (resume_id or continue_last) else None,
        mode=mode,
    )


def _most_recent_session() -> str | None:
    """Id of the newest session, or None when there are none."""
    from kalash.runtime.session import SessionManager

    try:
        recent = SessionManager().list_sessions(limit=1)
    except Exception:
        return None
    return str(recent[0]["id"]) if recent else None


@app.command()
def resume(
    session_id: Annotated[
        str | None,
        typer.Argument(help="Session ID to resume. Defaults to most recent."),
    ] = None,
) -> None:
    """Resume the most recent (or specified) session."""
    _launch_tui(resume_session=session_id or "")


# Register sub-apps

# noqa means no quality assuarance  to that line , when we run lint check
# E402 means specific rule and module level import


from kalash.cli.config_cmd import config_app  # noqa: E402
from kalash.cli.cron_cmd import cron_app  # noqa: E402
from kalash.cli.mcp_cmd import mcp_app  # noqa: E402
from kalash.cli.memory_cmd import memory_app  # noqa: E402
from kalash.cli.session import session_app  # noqa: E402

# Seperate module for apps and loose coupling implemented here
app.add_typer(session_app, name="session", help="Session management")
app.add_typer(memory_app, name="memory", help="Memory management")
app.add_typer(config_app, name="config", help="Configuration management")
app.add_typer(cron_app, name="cron", help="Scheduled task management")
app.add_typer(mcp_app, name="mcp", help="MCP server management")

# Additional CLI command groups
from kalash.cli.agents_cmd import agents_app  # noqa: E402
from kalash.cli.hooks_cmd import hooks_app  # noqa: E402
from kalash.cli.provider_cmd import provider_app  # noqa: E402
from kalash.cli.serve_cmd import serve_app  # noqa: E402
from kalash.cli.skills_cmd import skills_app  # noqa: E402
from kalash.cli.status_cmd import status_app  # noqa: E402

app.add_typer(status_app, name="status", help="Project and session status")
app.add_typer(provider_app, name="provider", help="Model provider management")
app.add_typer(agents_app, name="agents", help="Agent management")
app.add_typer(skills_app, name="skills", help="Skills management")
app.add_typer(hooks_app, name="hooks", help="Hook management")
app.add_typer(serve_app, name="serve", help="Run the schedule daemon")


# Doctor is a standalone command, not a sub-app
from kalash.cli.doctor import doctor  # noqa: E402

app.command(name="doctor")(doctor)


@app.command("version")
def version_cmd() -> None:
    """Print the Kalash version."""
    from kalash import __version__

    console.print(f"kalash {__version__}")


# This is project initialization command what it does it , it scaffolds a new project and agents read from there.
@app.command("init")
def init() -> None:
    """Scaffold .kalash/ and a starter KALASH.md for this project."""
    from pathlib import Path

    root = Path.cwd()
    kalash_dir = root / ".kalash"
    kalash_dir.mkdir(exist_ok=True)
    (kalash_dir / "settings.json").touch(exist_ok=True)

    agents_dir = kalash_dir / "agents"
    agents_dir.mkdir(exist_ok=True)
    skills_dir = kalash_dir / "skills"
    skills_dir.mkdir(exist_ok=True)

    kalash_md = root / "KALASH.md"
    if not kalash_md.exists():
        kalash_md.write_text(
            "# Project instructions for Kalash\n\n"
            "- Describe stack, conventions, and commands here.\n"
            "- The agent reads this file hierarchy on every turn.\n",
            encoding="utf-8",
        )
        console.print(f"[green]Created[/green] {kalash_md}")
    else:
        console.print(f"[dim]Already exists:[/dim] {kalash_md}")

    console.print(f"[green]Initialized[/green] {kalash_dir}/")


if __name__ == "__main__":
    app()
