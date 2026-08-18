"""Main Kalash CLI application with all command groups."""

from __future__ import annotations

from typing import Annotated, Optional

import typer
from rich.console import Console

app = typer.Typer(name="kalash", help="Terminal-native agentic coding assistant")
console = Console()


def _launch_tui(pipe_mode: bool = False) -> None:
    """Launch the interactive TUI or pipe mode."""
    if pipe_mode:
        from kalash.cli._pipe import run_pipe_mode

        run_pipe_mode()
    else:
        from kalash.tui.app import KalashApp

        tui = KalashApp()
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

    if prompt is not None:
        run_pipe_mode(prompt)
        return

    # Piped input means there is no terminal to draw a TUI into.
    if not sys.stdin.isatty():
        run_pipe_mode()
        return

    _launch_tui(pipe_mode=False)


@app.command()
def resume(
    session_id: Annotated[
        Optional[str],
        typer.Argument(help="Session ID to resume. Defaults to most recent."),
    ] = None,
) -> None:
    """Resume the most recent (or specified) session."""
    from kalash.tui.app import KalashApp

    tui = KalashApp(resume_session=session_id)
    tui.run()


@app.command()
def rewind(
    steps: Annotated[
        int,
        typer.Argument(help="Number of turns to rewind"),
    ] = 1,
    session_id: Annotated[
        Optional[str],
        typer.Option("--session", "-s", help="Session ID. Defaults to most recent."),
    ] = None,
) -> None:
    """Rewind the conversation by N turns and re-enter the TUI."""
    from kalash.tui.app import KalashApp

    tui = KalashApp(resume_session=session_id, rewind_steps=steps)
    tui.run()


# ---------------------------------------------------------------------------
# Register sub-apps
# ---------------------------------------------------------------------------

from kalash.cli.session import session_app  # noqa: E402
from kalash.cli.memory_cmd import memory_app  # noqa: E402
from kalash.cli.config_cmd import config_app  # noqa: E402
from kalash.cli.cron_cmd import cron_app  # noqa: E402
from kalash.cli.mcp_cmd import mcp_app  # noqa: E402

app.add_typer(session_app, name="session", help="Session management")
app.add_typer(memory_app, name="memory", help="Memory management")
app.add_typer(config_app, name="config", help="Configuration management")
app.add_typer(cron_app, name="cron", help="Scheduled task management")
app.add_typer(mcp_app, name="mcp", help="MCP server management")

# Placeholder sub-apps for future implementation
provider_app = typer.Typer(help="Model provider management")
agents_app = typer.Typer(help="Agent management")
skills_app = typer.Typer(help="Skills management")
hooks_app = typer.Typer(help="Hook management")
plugin_app = typer.Typer(help="Plugin management")
serve_app = typer.Typer(help="Serve Kalash as API")

app.add_typer(provider_app, name="provider", help="Model provider management")
app.add_typer(agents_app, name="agents", help="Agent management")
app.add_typer(skills_app, name="skills", help="Skills management")
app.add_typer(hooks_app, name="hooks", help="Hook management")
app.add_typer(plugin_app, name="plugin", help="Plugin management")
app.add_typer(serve_app, name="serve", help="Serve Kalash as API")

# Doctor is a standalone command, not a sub-app
from kalash.cli.doctor import doctor  # noqa: E402

app.command(name="doctor")(doctor)


if __name__ == "__main__":
    app()
