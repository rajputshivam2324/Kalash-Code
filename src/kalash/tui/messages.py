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
    """A tool invocation with its outcome."""

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

    def complete(self, ok: bool, detail: str) -> None:
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
        super().__init__(Text("\n".join(body), style="red" if error else "dim"))
