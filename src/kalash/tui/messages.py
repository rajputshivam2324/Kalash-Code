"""Transcript message widgets.

Each entry in the conversation is a Static subclass that renders a Rich
renderable. Assistant messages accumulate streamed chunks and re-render in
place, which keeps streaming inside the public widget API.

Model output is rendered via rich.text.Text rather than console markup, so
text containing square brackets renders literally instead of being parsed
as markup.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static


LOGO = """\
 ██╗  ██╗ █████╗ ██╗      █████╗ ███████╗██╗  ██╗
 ██║ ██╔╝██╔══██╗██║     ██╔══██╗██╔════╝██║  ██║
 █████╔╝ ███████║██║     ███████║███████╗███████║
 ██╔═██╗ ██╔══██║██║     ██╔══██║╚════██║██╔══██║
 ██║  ██╗██║  ██║███████╗██║  ██║███████║██║  ██║
 ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝\
"""

# Compact transcript: show a handful of lines, fold the rest.
DIFF_PREVIEW_LINES = 12
OUTPUT_PREVIEW_LINES = 4


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
        body = Text(LOGO, style="bold")
        body.append("\n\n")
        body.append("Ask anything about this project, or press / for commands", style="dim")
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
        super().__init__(Text(text, style="bold"))


class AssistantMessage(Static):
    """A streaming model response."""

    DEFAULT_CSS = """
    AssistantMessage {
        height: auto;
        width: 100%;
        padding: 0 0 0 1;
        margin: 1 0 0 0;
    }
    """

    def __init__(self) -> None:
        super().__init__(Text("● …", style="dim"))
        self._chunks: list[str] = []
        self._done = False
        self._error: str | None = None

    @property
    def text(self) -> str:
        return "".join(self._chunks)

    def append(self, delta: str) -> None:
        """Add a streamed chunk and repaint in place."""
        self._chunks.append(delta)
        self._repaint()

    def finish(self) -> None:
        self._done = True
        self._repaint()

    def set_working(self) -> None:
        """Shown when the model delegated to tools without saying anything first."""
        if not self._chunks and not self._done:
            self.update(Text("● working…", style="yellow"))

    def set_error(self, message: str) -> None:
        self._done = True
        self._error = message
        self._repaint()

    def _repaint(self) -> None:
        """Rebuild the rendered body.

        Named `_repaint` rather than `_render`/`refresh` to avoid shadowing
        Textual's internal widget API.
        """
        if self._error is not None:
            self.update(Text.assemble(("✗ ", "bold red"), (self._error, "red")))
            return
        body = self.text
        if not body:
            self.update(Text("● …", style="dim"))
            return
        marker = ("● ", "bold green" if self._done else "bold yellow")
        self.update(Text.assemble(marker, (body, "")))


class ToolCallMessage(Static):
    """A tool invocation with its outcome (legacy; prefer ToolCallLine)."""

    DEFAULT_CSS = """
    ToolCallMessage {
        height: auto;
        width: 100%;
        padding: 0 0 0 1;
        margin: 1 0 0 0;
    }
    """

    def __init__(self, tool_name: str, summary: str) -> None:
        self._tool_name = tool_name
        self._summary = summary
        self._state = "running"
        self._detail = "running…"
        super().__init__(self._build())

    def mark_done(self, ok: bool, detail: str) -> None:
        self._state = "ok" if ok else "error"
        self._detail = detail
        self.update(self._build())

    def _build(self) -> Text:
        colour = {"running": "yellow", "ok": "green", "error": "red"}[self._state]
        text = Text()
        text.append("⏺ ", style=colour)
        text.append(self._tool_name, style="bold")
        text.append("(", style="dim")
        text.append(self._summary, style="cyan")
        text.append(")", style="dim")
        text.append("\n  ⎿ ", style="dim")
        text.append(self._detail, style="dim" if self._state != "error" else "red")
        return text


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
        style = "red" if error else ""
        super().__init__(Text("\n".join(body), style=style))


class ToolCallLine(Static):
    """One tool call: headline, optional folded output, optional compact diff.

    Mirrors Cursor/OpenCode: a `$ command` or `Edited path` line, a handful of
    preview lines, then `… N lines hidden · ctrl+o to expand` instead of dumping
    the whole observation into the transcript.
    """

    DEFAULT_CSS = """
    ToolCallLine {
        height: auto;
        width: 100%;
        padding: 0 0 0 1;
        margin: 0 0 1 0;
    }
    """

    SLOW_AFTER_SECONDS = 2.0

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
        super().__init__(self._build_line())

    def tick(self, elapsed: float) -> None:
        """Refresh the elapsed counter while the call is still running."""
        if self._done or elapsed < self.SLOW_AFTER_SECONDS:
            return
        text = Text()
        text.append("· ", style="")
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
        """Mark the call complete. Safe to call more than once."""
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
        return len(self._output) > OUTPUT_PREVIEW_LINES or (
            len(self._diff_text.splitlines()) > DIFF_PREVIEW_LINES
        )

    def _build_line(self) -> Text:
        text = Text()
        failed = self._failed
        mark = "✗ " if failed else ("✓ " if self._done else "· ")
        text.append(mark, style="red" if failed else ("green" if self._done else ""))
        text.append(self._label, style="bold")

        meta: list[tuple[str, str]] = []
        if self._diff_stat:
            meta.append((self._diff_stat, "green" if not failed else "red"))
        if self._duration_ms >= 1000:
            meta.append((f"{self._duration_ms / 1000:.1f}s", "dim"))
        if self._exit_code not in (None, 0):
            meta.append((f"exit {self._exit_code}", "red"))
        for value, style in meta:
            text.append("  ")
            text.append(value, style=style)
        text.append("\n")

        if self._diff_text:
            _append_folded_diff(text, self._diff_text, expanded=self._expanded)
        elif self._output:
            _append_folded_output(text, self._output, expanded=self._expanded)

        if self._summary:
            text.append("  ")
            style = "yellow" if "unwrapped" in self._summary.lower() else ""
            text.append(self._summary, style=style)
            text.append("\n")
        return text


class DiffView(Static):
    """Inline unified diff for a file the agent just changed."""

    DEFAULT_CSS = """
    DiffView {
        height: auto;
        width: 100%;
        padding: 0 0 0 3;
        margin: 0 0 1 0;
    }
    """

    def __init__(self, path: str, diff_text: str, stat: str = "") -> None:
        super().__init__(self._render_diff(path, diff_text, stat))

    @staticmethod
    def _render_diff(path: str, diff_text: str, stat: str) -> Text:
        text = Text()
        text.append(_short_path(path), style="bold cyan")
        if stat:
            added, _, removed = stat.partition("/")
            text.append("  ")
            text.append(added, style="green")
            text.append("/", style="")
            text.append(removed, style="red")
        text.append("\n")
        _append_folded_diff(text, diff_text, expanded=False)
        return text


class ToolOutputView(Static):
    """Folded stdout/stderr. Prefer attaching output to ToolCallLine."""

    DEFAULT_CSS = """
    ToolOutputView {
        height: auto;
        width: 100%;
        padding: 0 0 0 3;
        margin: 0 0 1 0;
    }
    """

    MAX_LINES = OUTPUT_PREVIEW_LINES

    def __init__(self, label: str) -> None:
        self._label = label
        self._lines: list[str] = []
        super().__init__(self._build_output())

    def append(self, chunk: str) -> None:
        for line in chunk.splitlines(keepends=True):
            if line.endswith("\n"):
                self._lines.append(line.rstrip("\n"))
            elif self._lines:
                self._lines[-1] += line
            else:
                self._lines.append(line)
        self.update(self._build_output())

    def _build_output(self) -> Text:
        text = Text()
        if self._label:
            text.append(f"  ⎿ {self._label}\n", style="bold")
        _append_folded_output(text, self._lines, expanded=False)
        return text


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
        text.append("└─ subagent ", style="cyan")
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
        if line.startswith("+"):
            text.append("  " + line + "\n", style="green")
        elif line.startswith("-"):
            text.append("  " + line + "\n", style="red")
        else:
            text.append("  " + line + "\n", style="dim")
    hidden = len(lines) - len(visible)
    if hidden > 0 and not expanded:
        text.append(
            f"  … truncated ({hidden} more lines) · ctrl+o to expand\n",
            style="dim italic",
        )
    elif expanded and len(lines) > DIFF_PREVIEW_LINES:
        text.append("  … ctrl+o to fold\n", style="dim italic")


def _append_folded_output(text: Text, lines: list[str], *, expanded: bool) -> None:
    if not lines:
        return
    visible = lines if expanded else lines[:OUTPUT_PREVIEW_LINES]
    for line in visible:
        text.append("  ")
        text.append(line + "\n", style="dim")
    hidden = len(lines) - len(visible)
    if hidden > 0 and not expanded:
        text.append(
            f"  … {hidden} output lines hidden · ctrl+o to expand\n",
            style="dim italic",
        )
    elif expanded and len(lines) > OUTPUT_PREVIEW_LINES:
        text.append("  … ctrl+o to fold\n", style="dim italic")


def _short_path(path: str) -> str:
    """Trim a path to something that fits a terminal line."""
    from pathlib import Path

    try:
        return str(Path(path).relative_to(Path.cwd()))
    except (ValueError, OSError):
        parts = Path(path).parts
        return str(Path(*parts[-3:])) if len(parts) > 3 else path
