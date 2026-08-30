"""Transcript message widgets with Markdown formatting and Antigravity-style tool flows.

Pure developer-grade terminal interface:
- Zero emojis
- Monospaced tool badges ([read], [write], [edit], [shell], [memory], [subagent])
- Rich syntax-highlighted Markdown with code blocks
- Clean gutter-style output folding (│)
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.events import Click
from textual.widgets import Static

LOGO = """\
 ██╗  ██╗ █████╗ ██╗      █████╗ ███████╗██╗  ██╗
 ██║ ██╔╝██╔══██╗██║     ██╔══██╗██╔════╝██║  ██║
 █████╔╝ ███████║██║     ███████║███████╗███████║
 ██╔═██╗ ██╔══██║██║     ██╔══██║╚════██║██╔══██║
 ██║  ██╗██║  ██║███████╗██║  ██║███████║██║  ██║
 ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝\
"""

DIFF_PREVIEW_LINES = 14
OUTPUT_PREVIEW_LINES = 6

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

TOOL_BADGES: dict[str, str] = {
    # OpenCode style: no explicit textual badges, rely on the summarized label (e.g. `$ cmd`, `Edited file`)
}


class WelcomeBanner(Static):
    """Shown before the first message, then removed."""

    DEFAULT_CSS = """
    WelcomeBanner {
        height: auto;
        width: 100%;
        text-align: center;
        color: $text-muted;
        padding: 2 0 1 0;
    }
    """

    def __init__(self) -> None:
        body = Text(LOGO, style="bold cyan")
        body.append("\n\n")
        body.append("Kalash Code — Autonomous Terminal Coding Harness\n", style="bold")
        body.append("Press / for commands (/models, /eval, /plan, /copy) or type a prompt to begin", style="dim")
        super().__init__(body)


class UserMessage(Static):
    """A message from the user."""

    DEFAULT_CSS = """
    UserMessage {
        height: auto;
        width: 100%;
        padding: 0 0 0 1;
        margin: 1 0 0 0;
        border-left: thick $primary;
    }
    """

    def __init__(self, text: str) -> None:
        self.plain_text = text
        super().__init__(Text(text))


class AssistantMessage(Static):
    """A streaming model response rendered with rich Markdown and code syntax."""

    DEFAULT_CSS = """
    AssistantMessage {
        height: auto;
        width: 100%;
        padding: 0 0 0 1;
        margin: 1 0 0 0;
    }
    """

    def __init__(self) -> None:
        super().__init__(Text("…", style="dim"))
        self._chunks: list[str] = []
        self._done = False
        self._error: str | None = None
        self._last_repaint = 0.0

    @property
    def text(self) -> str:
        return "".join(self._chunks)

    @property
    def plain_text(self) -> str:
        return self.text

    def append(self, delta: str) -> None:
        """Add a streamed chunk and repaint in place (throttled for high-FPS smoothness)."""
        self._chunks.append(delta)
        now = time.monotonic()
        if now - self._last_repaint >= 0.035 or self._done:
            self._last_repaint = now
            self._repaint()

    def finish(self) -> None:
        self._done = True
        self._repaint()

    def set_working(self) -> None:
        """Shown when the model delegated to tools without saying anything first."""
        if not self._chunks and not self._done:
            self.update(Text("working…", style="yellow"))

    def set_error(self, message: str) -> None:
        self._done = True
        self._error = message
        self._repaint()

    def _repaint(self) -> None:
        """Rebuild the rendered body with Markdown."""
        if self._error is not None:
            self.update(Text.assemble(("✗ ", "bold red"), (self._error, "red")))
            return

        body = self.text
        if not body:
            # OpenCode style subtle thinking indicator
            self.update(Text("…", style="dim"))
            return

        try:
            md = Markdown(body, code_theme="monokai")
            self.update(md)
        except Exception:
            self.update(body)

class SystemMessage(Static):
    """Command output, help text, status lines."""

    DEFAULT_CSS = """
    SystemMessage {
        height: auto;
        width: 100%;
        padding: 0 0 0 1;
        margin: 1 0 0 0;
    }
    """

    def __init__(self, lines: list[str] | str, *, error: bool = False) -> None:
        body = [lines] if isinstance(lines, str) else lines
        style = "bold red" if error else "cyan"
        self.plain_text = "\n".join(body)
        super().__init__(Text(self.plain_text, style=style))


class ComparisonTableMessage(Static):
    """Renders rich benchmark and harness comparison tables."""

    DEFAULT_CSS = """
    ComparisonTableMessage {
        height: auto;
        width: 100%;
        margin: 1 0;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        title: str,
        columns: list[str],
        rows: list[list[str]],
        summary: str = "",
    ) -> None:
        table = Table(title=title, border_style="cyan", show_lines=True)
        for col in columns:
            table.add_column(col, style="bold")
        for row in rows:
            styled_row = list(row)
            if styled_row and "PASS" in styled_row[-1]:
                styled_row[-1] = f"[bold green]{styled_row[-1]}[/bold green]"
            elif styled_row and "FAIL" in styled_row[-1]:
                styled_row[-1] = f"[bold red]{styled_row[-1]}[/bold red]"
            table.add_row(*styled_row)

        self.plain_text = f"{title}\n" + "\n".join(["\t".join(r) for r in rows])
        if summary:
            self.plain_text += f"\n{summary}"
        super().__init__(table)


class PlanMessage(Static):
    """Renders structured plan checklists with visual progress."""

    DEFAULT_CSS = """
    PlanMessage {
        height: auto;
        width: 100%;
        margin: 1 0;
        padding: 0 1;
    }
    """

    def __init__(self, description: str, tasks: list[dict[str, Any]]) -> None:
        table = Table(title=f"Plan: {description}", border_style="cyan")
        table.add_column("#", justify="right", style="dim", width=4)
        table.add_column("Status", width=12)
        table.add_column("Task Description", style="bold")

        completed_count = 0
        for idx, task in enumerate(tasks, start=1):
            is_done = task.get("completed", False)
            if is_done:
                completed_count += 1
                status = "[green][DONE][/green]"
            else:
                status = "[yellow][PENDING][/yellow]"
            table.add_row(str(idx), status, task.get("description", ""))

        pct = (completed_count / len(tasks) * 100) if tasks else 0
        summary = f"Progress: {completed_count}/{len(tasks)} tasks completed ({pct:.0f}%)"
        self.plain_text = f"Plan: {description}\n{summary}"
        super().__init__(table)


class ToolCallLine(Static):
    """Antigravity/Claude Code style tool call widget."""

    DEFAULT_CSS = """
    ToolCallLine {
        height: auto;
        width: 100%;
        padding: 0 0 0 1;
        margin: 0 0 1 0;
    }
    """

    SLOW_AFTER_SECONDS = 1.0

    def __init__(self, label: str, *, tool_name: str = "") -> None:
        self._label = label
        self._tool_name = tool_name
        self._done = False
        self._failed = False
        self._duration_ms = 0
        self._exit_code: int | None = None
        self._output: list[str] = []
        self._diff_text = ""
        self._diff_stat = ""
        self._path = ""
        self._summary = ""
        self._expanded = False
        self._spinner_idx = 0
        self._badge = TOOL_BADGES.get(tool_name, "")
        super().__init__(self._build_line())

    async def on_click(self, event: Click) -> None:
        if self.is_expandable:
            self.toggle_expand()

    def tick(self, elapsed: float) -> None:
        if self._done or elapsed < self.SLOW_AFTER_SECONDS:
            return
        self._spinner_idx = (self._spinner_idx + 1) % len(SPINNER_FRAMES)
        spinner = SPINNER_FRAMES[self._spinner_idx]
        text = Text()
        if self._badge:
            text.append(f"{spinner} {self._badge} ", style="bold cyan")
        else:
            text.append(f"{spinner} ", style="bold cyan")
        text.append(self._label, style="bold")
        text.append(f"  {elapsed:.0f}s", style="yellow")
        text.append("   ctrl+c to stop", style="dim")
        self.update(text)

    def finish(
        self,
        *,
        failed: bool = False,
        duration_ms: int = 0,
        exit_code: int | None = None,
    ) -> None:
        self._done = True
        self._failed = self._failed or failed
        if duration_ms:
            self._duration_ms = duration_ms
        if exit_code is not None:
            self._exit_code = exit_code
        self.update(self._build_line())

    def append_output(self, chunk: str) -> None:
        for line in chunk.splitlines(keepends=True):
            if line.endswith("\n"):
                self._output.append(line.rstrip("\n"))
            elif self._output:
                self._output[-1] += line
            else:
                self._output.append(line)
        self.update(self._build_line())

    def set_diff(self, path: str, diff_text: str, stat: str = "") -> None:
        self._path = path
        self._diff_text = diff_text
        self._diff_stat = stat
        self.update(self._build_line())

    def set_summary(self, summary: str) -> None:
        self._summary = summary.strip()
        self.update(self._build_line())

    def toggle_expand(self) -> None:
        self._expanded = not self._expanded
        self.update(self._build_line())

    @property
    def is_done(self) -> bool:
        return self._done

    @property
    def is_expandable(self) -> bool:
        return bool(self._output or self._diff_text or self._summary)

    @property
    def plain_output(self) -> str:
        return "\n".join(self._output) if self._output else self._diff_text

    def _build_line(self) -> Any:
        from rich.console import Group
        from rich.panel import Panel
        from rich.text import Text

        text = Text()
        failed = self._failed
        badge_part = f"{self._badge} " if self._badge else ""

        if not self._done:
            spinner = SPINNER_FRAMES[self._spinner_idx]
            text.append(f"{spinner} {badge_part}", style="bold cyan")
        elif failed:
            text.append(f"✗ {badge_part}", style="bold red")
        else:
            text.append(f"✓ {badge_part}", style="bold green")

        text.append(self._label, style="bold")

        meta: list[tuple[str, str]] = []
        if self._diff_stat:
            meta.append((self._diff_stat, "bold green" if not failed else "bold red"))
        if self._duration_ms > 0:
            if self._duration_ms >= 1000:
                meta.append((f"{self._duration_ms / 1000:.1f}s", "dim"))
            else:
                meta.append((f"{self._duration_ms}ms", "dim"))
        if self._exit_code not in (None, 0):
            meta.append((f"exit {self._exit_code}", "bold red"))

        for value, style in meta:
            text.append("  ")
            text.append(value, style=style)

        renderables = [text]

        if self._diff_text:
            diff_text = Text()
            _append_folded_diff(diff_text, self._diff_text, expanded=self._expanded)
            renderables.append(diff_text)
        elif self._output:
            visible = self._output if self._expanded else self._output[:OUTPUT_PREVIEW_LINES]
            output_text = Text("\n".join(visible))
            hidden = len(self._output) - len(visible)
            if hidden > 0 and not self._expanded:
                output_text.append(f"\n\n… {hidden} output lines hidden · ctrl+o to expand", style="italic")
            elif self._expanded and len(self._output) > OUTPUT_PREVIEW_LINES:
                output_text.append("\n\n… ctrl+o to fold output", style="italic")

            panel = Panel(
                output_text,
                style="dim",
                border_style="bright_black",
                padding=(0, 1),
            )
            renderables.append(panel)

        if self._summary:
            summary_text = Text()
            summary_text.append("  │ ")
            style = "yellow" if "unwrapped" in self._summary.lower() else "dim"
            summary_text.append(self._summary, style=style)
            renderables.append(summary_text)
            
        if len(renderables) == 1:
            return renderables[0]
        return Group(*renderables)


class SubagentLine(Static):
    """Status line for a delegated child run."""

    DEFAULT_CSS = """
    SubagentLine {
        height: auto;
        width: 100%;
        padding: 0 0 0 3;
        color: $accent;
    }
    """

    def __init__(self, brief: str) -> None:
        self._brief = brief
        self._done = False
        super().__init__(self._compose_text("running"))

    def _compose_text(self, state: str, detail: str = "") -> Text:
        text = Text()
        text.append("└─ [subagent] ", style="bold cyan")
        text.append(self._brief, style="dim")
        text.append(f"  [{state}]", style="cyan" if not self._done else "green")
        if detail:
            text.append(f"  {detail}", style="dim")
        return text

    def finish(self, *, failed: bool = False, turns: int = 0, tokens: int = 0) -> None:
        self._done = True
        detail = f"{turns} turns, {tokens:,} tok" if turns else ""
        self.update(self._compose_text("failed" if failed else "done", detail))


def _append_folded_diff(text: Text, diff_text: str, *, expanded: bool) -> None:
    lines = diff_text.splitlines()
    visible = lines if expanded else lines[:DIFF_PREVIEW_LINES]
    for line in visible:
        if line.startswith("+++") or line.startswith("---"):
            text.append("  │ " + line + "\n", style="bold cyan")
        elif line.startswith("@@"):
            text.append("  │ " + line + "\n", style="bold magenta")
        elif line.startswith("+"):
            text.append("  │ " + line + "\n", style="green")
        elif line.startswith("-"):
            text.append("  │ " + line + "\n", style="red")
        else:
            text.append("  │ " + line + "\n", style="dim")
    hidden = len(lines) - len(visible)
    if hidden > 0 and not expanded:
        text.append(
            f"  … truncated ({hidden} more lines) · ctrl+o to expand\n",
            style="dim italic cyan",
        )
    elif expanded and len(lines) > DIFF_PREVIEW_LINES:
        text.append("  … ctrl+o to fold diff\n", style="dim italic")


def _append_folded_output(text: Text, lines: list[str], *, expanded: bool) -> None:
    if not lines:
        return
    visible = lines if expanded else lines[:OUTPUT_PREVIEW_LINES]
    for line in visible:
        text.append("  │ ")
        text.append(line + "\n", style="dim")
    hidden = len(lines) - len(visible)
    if hidden > 0 and not expanded:
        text.append(
            f"  … {hidden} output lines hidden · ctrl+o to expand\n",
            style="dim italic cyan",
        )
    elif expanded and len(lines) > OUTPUT_PREVIEW_LINES:
        text.append("  … ctrl+o to fold output\n", style="dim italic")


def _short_path(path: str) -> str:
    """Trim a path to something that fits a terminal line."""
    try:
        return str(Path(path).relative_to(Path.cwd()))
    except (ValueError, OSError):
        parts = Path(path).parts
        return str(Path(*parts[-3:])) if len(parts) > 3 else path


from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button

class DiffReview(Static):
    """An interactive diff review widget."""
    def __init__(self, diff_text: str) -> None:
        self.diff_text = diff_text
        super().__init__()

    def compose(self) -> ComposeResult:
        text = Text()
        _append_folded_diff(text, self.diff_text, expanded=True)
        yield Static(text)
        with Horizontal(classes="buttons"):
            yield Button("Accept", variant="success", id="btn-accept")
            yield Button("Reject", variant="error", id="btn-reject")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-accept":
            self.styles.border = ("round", "green")
            self.query_one(".buttons").remove()
        elif event.button.id == "btn-reject":
            self.styles.border = ("round", "red")
            self.query_one(".buttons").remove()


class MetricsMessage(Static):
    """Rich visualizations for stats/cost."""
    def __init__(self, title: str, metric: str, value: int, max_value: int) -> None:
        self.title = title
        self.metric = metric
        self.value = value
        self.max_value = max_value
        super().__init__()
        
    def compose(self) -> ComposeResult:
        from rich.bar import Bar
        from rich.console import Group
        
        header = Text(f"{self.title} | {self.metric}: {self.value}/{self.max_value}", style="bold cyan")
        bar = Bar(size=self.max_value, begin=0, end=self.value, color="cyan", bgcolor="black")
        yield Static(Group(header, bar))
