"""Transcript, status and prompt widgets."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from kalash.tui.describe import command_hint, error_hint, header_parts, result_meta
from kalash.tui.theme import Palette

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
PASTE_TOKEN = re.compile(r"\[Pasted text #(\d+) \+(\d+) lines\]")
PASTE_MIN_LINES = 4
PASTE_MIN_CHARS = 800


# ----------------------------------------------------------------------------- diffs


def render_diff(diff_text: str, palette: Palette, max_lines: int | None = None) -> Text:
    """Unified diff with a line-number gutter and coloured added/removed lines."""
    out = Text(no_wrap=True, overflow="ellipsis")
    old_ln = new_ln = 0
    rows: list[tuple[str, int | None, str]] = []
    for raw in diff_text.splitlines():
        if raw.startswith("@@"):
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", raw)
            if match:
                old_ln, new_ln = int(match.group(1)), int(match.group(2))
            rows.append(("@", None, raw))
        elif raw.startswith("+"):
            rows.append(("+", new_ln, raw[1:]))
            new_ln += 1
        elif raw.startswith("-"):
            rows.append(("-", old_ln, raw[1:]))
            old_ln += 1
        else:
            rows.append((" ", new_ln, raw[1:] if raw.startswith(" ") else raw))
            old_ln += 1
            new_ln += 1
    width = max((len(str(n)) for _, n, _ in rows if n is not None), default=1)
    shown = rows if max_lines is None else rows[:max_lines]
    band = min(max((len(c) for _, _, c in shown), default=0) + 4, 160)
    for kind, number, content in shown:
        if kind == "@":
            out.append(f"{' ' * width}  {content}\n", style=palette.hunk)
            continue
        gutter = f"{number:>{width}} " if number is not None else " " * (width + 1)
        if kind == "+":
            style = f"{palette.added_fg} on {palette.added_bg}"
            out.append(f"{gutter}+ {content}".ljust(band + width) + "\n", style=style)
        elif kind == "-":
            style = f"{palette.removed_fg} on {palette.removed_bg}"
            out.append(f"{gutter}- {content}".ljust(band + width) + "\n", style=style)
        else:
            out.append(f"{gutter}  {content}\n", style=palette.muted)
    if max_lines is not None and len(rows) > max_lines:
        out.append(
            f"  … {len(rows) - max_lines} more lines (ctrl+o to expand)\n",
            style=f"italic {palette.muted}",
        )
    out.rstrip()
    return out


# ----------------------------------------------------------------------------- messages


class Transcript(VerticalScroll):
    """Pause follow synchronously before Textual consumes a wheel movement."""

    def __init__(
        self, *, on_user_scroll: Callable[[bool], None], widget_id: str | None = None
    ) -> None:
        super().__init__(id=widget_id)
        self._user_scroll_callback = on_user_scroll

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if not (event.ctrl or event.shift):
            self._user_scroll_callback(True)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if not (event.ctrl or event.shift):
            self._user_scroll_callback(False)


class UserMessage(Static):
    def __init__(self, text: str, *, queued: bool = False) -> None:
        super().__init__(Text(text))
        self.plain = text
        self.add_class("user")
        if queued:
            self.add_class("queued")

    def mark_sent(self) -> None:
        self.remove_class("queued")


class AssistantMessage(Vertical):
    """Streams Markdown incrementally and keeps the full text for copy/export."""

    def __init__(self) -> None:
        super().__init__()
        self.add_class("assistant")
        self._chunks: list[str] = []
        self._buffer: list[str] = []
        self._markdown: Markdown | None = None
        self._stream: Any = None
        self._writes: list[asyncio.Task[None]] = []

    def compose(self) -> ComposeResult:
        yield Markdown("")

    def on_mount(self) -> None:
        self._markdown = self.query_one(Markdown)
        self._stream = Markdown.get_stream(self._markdown)
        pending, self._buffer = self._buffer, []
        for chunk in pending:
            self._write(chunk)

    @property
    def text(self) -> str:
        return "".join(self._chunks)

    def append(self, chunk: str) -> None:
        self._chunks.append(chunk)
        if self._stream is None:
            self._buffer.append(chunk)
        else:
            self._write(chunk)

    def _write(self, chunk: str) -> None:
        self._writes.append(asyncio.create_task(self._stream.write(chunk)))

    async def on_unmount(self) -> None:
        if self._stream is not None:  # the app closed mid-answer: don't leak the stream task
            stream, self._stream = self._stream, None
            with contextlib.suppress(Exception):
                await stream.stop()

    async def finish(self) -> None:
        if self._stream is not None:
            await asyncio.gather(*self._writes, return_exceptions=True)
            await self._stream.stop()
            self._stream = None
        if not self.text.strip():
            self.remove()


class ReasoningRow(Static):
    """A dim 'thinking' line. The reasoning text itself is shown only in verbose mode."""

    def __init__(self) -> None:
        super().__init__("")
        self.add_class("reasoning")
        self._text: list[str] = []
        self.verbose = False
        self.done = False
        self._render_row()

    def add(self, text: str) -> None:
        self._text.append(text)
        if sum(len(t) for t in self._text) > 40_000:
            self._text = ["".join(self._text)[-30_000:]]
        self._render_row()

    def finish(self) -> None:
        self.done = True
        self._render_row()

    def set_verbose(self, verbose: bool) -> None:
        self.verbose = verbose
        self._render_row()

    def _render_row(self) -> None:
        body = "".join(self._text)
        words = len(body.split())
        first = next((ln.strip() for ln in reversed(body.splitlines()) if ln.strip()), "")
        if not self.done:  # while streaming, the latest line says what it is working out
            preview = f" · {first[:70]}…" if first else ""
        else:  # afterwards, how it began
            opening = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
            preview = f" · {opening[:70]}{'…' if len(opening) > 70 else ''}" if opening else ""
        head = f"✻ {'Thought' if self.done else 'Thinking…'}{preview} ({words} words)"
        if self.verbose and body.strip():
            self.update(Text(f"{head}\n{body.strip()[-4000:]}"))
        else:
            self.update(Text(head, no_wrap=True, overflow="ellipsis"))


@dataclass
class ToolData:
    call_id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    state: str = "pending"  # pending | running | ok | error | denied
    output: str = ""
    diff: dict[str, Any] | None = None
    started: float = 0.0
    elapsed: float = 0.0


class ToolBlock(Vertical):
    """One tool call: a header line (click to expand) and an optional body."""

    can_focus = False

    def __init__(self, data: ToolData, palette: Palette, cwd: Path | None = None) -> None:
        super().__init__()
        self.data = data
        self.palette = palette
        self.cwd = cwd
        self.group: ExploreGroup | None = None  # set when folded into a "read N files" row
        self.verbose = False
        self.expanded: bool | None = None  # None: automatic by tool/state
        self._frame = 0
        self.add_class("tool")
        self._head = Static("", classes="tool-head")
        self._body = Static("", classes="tool-body")

    def compose(self) -> ComposeResult:
        yield self._head
        yield self._body

    def on_mount(self) -> None:
        self.refresh_view()

    def on_click(self) -> None:
        self.expanded = not self._is_expanded()
        self.refresh_view()

    # -- state -------------------------------------------------------------------------

    def tick(self) -> None:
        if self.data.state == "running":
            self._frame = (self._frame + 1) % len(SPINNER)
            self._render_head()

    def set_palette(self, palette: Palette) -> None:
        self.palette = palette
        self.refresh_view()

    def set_verbose(self, verbose: bool) -> None:
        self.verbose = verbose
        self.refresh_view()

    def _is_expanded(self) -> bool:
        if self.expanded is not None:
            return self.expanded
        if self.verbose:
            return True
        d = self.data
        if d.state in ("error", "denied"):
            return True
        if d.diff is not None or d.name == "todo_write":
            return True
        return d.state == "running" and d.name in ("bash", "task")

    def refresh_view(self) -> None:
        self._render_head()
        self._render_body()

    # -- rendering ---------------------------------------------------------------------

    def _marker(self) -> tuple[str, str]:
        p, state = self.palette, self.data.state
        if state == "running":
            return SPINNER[self._frame], p.accent
        marks = {"pending": "○", "ok": "✓", "error": "✗", "denied": "⊘"}
        colors = {"pending": p.muted, "ok": p.ok, "error": p.error, "denied": p.warn}
        return marks[state], colors[state]

    def _render_head(self) -> None:
        d, p = self.data, self.palette
        marker, color = self._marker()
        verb, target = header_parts(d.name, d.args, self.cwd)
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append(f"{marker} ", style=f"bold {color}")
        text.append(verb, style="bold")
        if target:
            text.append(f"  {target}")
        hint = command_hint(d.name, d.args)
        if hint:
            text.append(f"  $ {hint}", style=p.muted)
        if d.diff:
            text.append(f"  +{d.diff['added']}", style=p.added_fg)
            text.append(f" -{d.diff['removed']}", style=p.removed_fg)
            if d.diff.get("new_file"):
                text.append("  new file", style=p.muted)
            elif d.diff.get("whitespace_only"):
                text.append("  whitespace only", style=f"italic {p.warn}")
        meta = result_meta(d.name, d.output, d.state == "error")
        if meta:
            text.append(f"  {meta}", style=p.muted)
        if d.state == "denied":
            text.append("  denied", style=f"bold {p.warn}")
        if d.state == "running" and d.started:
            text.append(f"  {d.elapsed:.0f}s" if d.elapsed >= 2 else "", style=p.muted)
        self._head.update(text)

    def _render_body(self) -> None:
        d, p = self.data, self.palette
        expanded = self._is_expanded()
        limit = None if self.verbose else (16 if d.diff else 6)
        body: Text | None = None
        if d.diff is not None and (expanded or d.state in ("ok",)):
            body = render_diff(str(d.diff["text"]), p, limit)
        elif d.name == "todo_write":
            body = self._todo_text()
        elif d.state == "running" and d.output.strip():
            lines = [ln for ln in d.output.strip().splitlines() if ln.strip()]
            body = Text("\n".join(f"  {ln[:200]}" for ln in lines[-4:]), style=p.muted)
        elif d.name == "bash" and d.state == "ok" and not expanded and d.output.strip():
            lines = [
                ln
                for ln in d.output.strip().splitlines()
                if ln.strip() and not ln.startswith("[exit code")
            ]
            body = Text("\n".join(f"  {ln[:200]}" for ln in lines[-3:]), style=p.muted)
        elif d.output.strip() and (expanded or d.state in ("error", "denied")):
            lines = [ln for ln in d.output.strip().splitlines() if ln.strip()]
            if d.state in ("error", "denied"):
                lines = lines[:3] if not self.verbose and not expanded else lines
                style = p.error if d.state == "error" else p.warn
            else:
                style = p.muted
            cap = 60 if (self.verbose or self.expanded) else 8
            shown = lines[:cap] if d.name != "bash" else lines[-cap:]
            body = Text("\n".join(f"  {ln[:300]}" for ln in shown), style=style)
            if len(lines) > cap:
                body.append(f"\n  … {len(lines) - cap} more lines", style=f"italic {p.muted}")
        has_body = body is not None and bool(str(body).strip())
        self._body.display = has_body
        self.set_class(has_body, "has-body")
        if has_body and body is not None:
            self._body.update(body)

    def _todo_text(self) -> Text | None:
        todos = self.data.args.get("todos") or []
        if not todos:
            return None
        p = self.palette
        out = Text()
        marks = {
            "completed": ("☑", p.ok),
            "in_progress": ("◐", p.accent),
            "pending": ("☐", p.muted),
        }
        for i, todo in enumerate(todos):
            mark, color = marks.get(todo.get("status"), ("☐", p.muted))
            style = f"strike {p.muted}" if todo.get("status") == "completed" else ""
            out.append(f"  {mark} ", style=color)
            out.append(str(todo.get("content", "")), style=style)
            if i < len(todos) - 1:
                out.append("\n")
        return out


GROUPABLE = ("read", "glob", "grep")  # finished, read-only calls that fold into one row


class ExploreGroup(Static):
    """One line standing for a run of finished read-only tool calls (click to unfold them).

    Six `Read path` lines in a row tell the user nothing the summary does not; the calls stay in
    the transcript, hidden, and Ctrl+O (or a click) shows them again.
    """

    can_focus = False

    def __init__(self, palette: Palette, *, unfolded: bool = False) -> None:
        super().__init__("")
        self.palette = palette
        self.members: list[ToolBlock] = []
        self.open = unfolded
        self.add_class("explore")

    def add(self, block: ToolBlock) -> None:
        block.group = self
        self.members.append(block)
        block.display = self.open
        self.refresh_view()

    def set_open(self, value: bool) -> None:
        self.open = value
        for block in self.members:
            block.display = value
        self.refresh_view()

    def on_click(self) -> None:
        self.set_open(not self.open)

    def set_palette(self, palette: Palette) -> None:
        self.palette = palette
        self.refresh_view()

    def summary(self) -> str:
        reads = [b for b in self.members if b.data.name == "read"]
        files = list(dict.fromkeys(str(b.data.args.get("file_path", "")) for b in reads))
        searches = sum(1 for b in self.members if b.data.name == "grep")
        globs = sum(1 for b in self.members if b.data.name == "glob")
        parts = []
        if files:
            parts.append(f"read {len(files)} file{'s' if len(files) != 1 else ''}")
        if searches:
            parts.append(f"{searches} search{'es' if searches != 1 else ''}")
        if globs:
            parts.append(f"{globs} file listing{'s' if globs != 1 else ''}")
        return " · ".join(parts)

    def refresh_view(self) -> None:
        p = self.palette
        reads = [b for b in self.members if b.data.name == "read"]
        names = list(
            dict.fromkeys(str(b.data.args.get("file_path", "")).rsplit("/", 1)[-1] for b in reads)
        )
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append("✓ ", style=f"bold {p.ok}")
        text.append("Explored", style="bold")
        text.append(f"  {self.summary()}")
        if names and not self.open:
            shown = ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
            text.append(f"  {shown}", style=p.muted)
        text.append("  ▾" if self.open else "  ▸", style=p.muted)
        self.update(text)

    @property
    def plain(self) -> str:
        return str(self.content)


class TurnFooter(Static):
    """What a finished turn did, in one dim line: time, calls, tokens, files changed."""

    def __init__(self, text: str) -> None:
        super().__init__(Text(text, no_wrap=True, overflow="ellipsis"))
        self.plain = text
        self.add_class("turn-footer")


class NoticeRow(Static):
    def __init__(self, text: str, level: str = "info") -> None:
        icon = {"info": "ℹ", "warning": "⚠", "error": "✗"}.get(level, "ℹ")
        super().__init__(Text(f"{icon} {text}"))
        self.plain = text
        self.add_class("notice", f"-{level}")


class ErrorCard(Static):
    """A failed turn: what happened, and the next step."""

    def __init__(self, title: str, message: str, hint: str = "") -> None:
        text = Text()
        text.append(f"✗ {title}\n", style="bold")
        text.append(message.strip()[:1200])
        if hint:
            text.append(f"\n→ {hint}", style="italic")
        super().__init__(text)
        self.plain = f"{title}\n{message}\n{hint}"
        self.add_class("error-card")

    @classmethod
    def for_result(cls, reason: str, error: str | None, code: str | None) -> ErrorCard:
        title = {
            "error": "The turn failed",
            "limit": "Stopped at a limit",
            "stuck": "Stopped: the same failure kept repeating",
            "refused": "The model declined",
            "max_tokens": "Output limit reached",
            "cancelled": "Cancelled",
        }.get(reason, f"Turn ended ({reason})")
        return cls(title, error or reason, error_hint(code, error))


class WelcomeBanner(Static):
    def __init__(self, lines: list[tuple[str, str]], version: str, palette: Palette) -> None:
        super().__init__("")
        self.add_class("welcome")
        self.lines, self.version, self.palette = lines, version, palette
        self.refresh_view()

    def refresh_view(self) -> None:
        p = self.palette
        title = Text()
        title.append("⚱  Kalash", style=f"bold {p.accent}")
        title.append(f"  v{self.version}", style=p.muted)
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style=p.muted, no_wrap=True)
        grid.add_column(ratio=1)
        for key, value in self.lines:
            grid.add_row(key, value)
        self.update(Group(title, Text(""), grid))

    @property
    def plain(self) -> str:
        rows = [f"{key} {value}".strip() for key, value in self.lines]
        return "\n".join([f"Kalash v{self.version}", *rows])


# ----------------------------------------------------------------------------- bottom area


@dataclass
class StatusData:
    mode_id: str = "ask"
    mode_label: str = "Ask"
    model: str = ""
    effort: str = ""
    cwd: str = ""
    branch: str = ""
    dirty: int = 0
    used: int = 0
    window: int = 0
    cost: float | None = None
    tokens: int = 0
    todos: tuple[int, int] = (0, 0)
    verbose: bool = False


def _tail(path: str, limit: int) -> str:
    """The end of a long path, which is the part that tells projects apart."""
    return path if len(path) <= limit else "…" + path[-(limit - 1) :]


class StatusBar(Static):
    def __init__(self, palette: Palette) -> None:
        super().__init__("")
        self.palette = palette
        self.data = StatusData()

    def set_data(self, data: StatusData) -> None:
        self.data = data
        self.refresh_view()

    def on_resize(self) -> None:
        self.refresh_view()

    def refresh_view(self) -> None:
        p, d = self.palette, self.data
        width = self.size.width or 100
        mode_color = {
            "ask": p.accent,
            "auto": p.warn,
            "plan": p.plan,
            "read-only": p.muted,
            "full": p.error,
        }.get(d.mode_id, p.accent)

        def segment(*parts: tuple[str, str]) -> Text:
            t = Text()
            for content, style in parts:
                t.append(content, style=style)
            return t

        pct = (100 * d.used // d.window) if d.window else 0
        bar_color = p.ok if pct < 60 else (p.warn if pct < 85 else p.error)
        filled = min(10, round(pct / 10))
        segments: list[tuple[int, Text]] = [
            (0, segment((f" {d.mode_label.upper()} ", f"bold reverse {mode_color}"))),
            (
                0,
                segment(
                    (d.model or "no model", "bold"), (f" · {d.effort}" if d.effort else "", p.muted)
                ),
            ),
        ]
        if d.cwd:
            segments.append((5, segment((_tail(d.cwd, 32), p.muted))))
        if d.branch:
            dirty = f" ±{d.dirty}" if d.dirty else ""
            segments.append((4, segment((f" {d.branch}{dirty}", p.info))))
        if d.window:
            segments.append(
                (
                    3,
                    segment(
                        ("▰" * filled, bar_color),
                        ("▱" * (10 - filled), p.muted),
                        (f" {pct}%", p.muted),
                    ),
                )
            )
        if d.todos[1]:
            segments.append((1, segment((f"☑ {d.todos[0]}/{d.todos[1]}", p.muted))))
        if d.cost is not None:
            segments.append((2, segment((f"${d.cost:.2f}", p.muted))))
        hint = "ctrl+o details" if not d.verbose else "ctrl+o collapse"
        segments.append((6, segment((f"{hint} · ? help", p.muted))))
        # drop the least important segments until the line fits
        while True:
            total = sum(len(s.plain) for _, s in segments) + 3 * (len(segments) - 1)
            if total <= width or len(segments) <= 2:
                break
            drop = max(range(len(segments)), key=lambda i: segments[i][0])
            if segments[drop][0] == 0:
                break
            segments.pop(drop)
        line = Text()
        for i, (_, seg) in enumerate(segments):
            if i:
                line.append("  ")
            line.append_text(seg)
        self.update(line)


class ActivityBar(Static):
    """Spinner, what is happening, elapsed time and tokens - only while a turn runs."""

    def __init__(self, palette: Palette) -> None:
        super().__init__("")
        self.palette = palette
        self.active = False
        self.verb = "Working"
        self.started = 0.0
        self.tokens = 0
        self.queued = 0
        self.note = ""
        self.behind = False  # newer output exists below the part the user scrolled to
        self._frame = 0

    def begin(self, now: float) -> None:
        self.active, self.started, self.tokens, self.verb, self.note = True, now, 0, "Working", ""
        self.refresh_view(now)

    def end(self) -> None:
        self.active = False
        self.refresh_view(self.started)

    def tick(self, now: float) -> None:
        if self.active:
            self._frame = (self._frame + 1) % len(SPINNER)
            self.refresh_view(now)

    def refresh_view(self, now: float) -> None:
        p = self.palette
        if not self.active:
            idle = Text()
            if self.queued:
                idle.append(f"  {self.queued} message(s) queued", style=p.muted)
            if self.behind:
                idle.append("  ↓ new output below · ctrl+end", style=f"bold {p.accent}")
            self.update(idle)
            return
        elapsed = max(0, int(now - self.started))
        text = Text()
        text.append(f"{SPINNER[self._frame]} ", style=f"bold {p.accent}")
        text.append(f"{self.verb}…", style=p.accent)
        meta = [f"{elapsed}s"]
        if self.tokens:
            meta.append(f"↓ {self.tokens:,} tokens")
        if self.queued:
            meta.append(f"{self.queued} queued")
        text.append(f"  ({' · '.join(meta)} · esc to interrupt)", style=p.muted)
        if self.note:
            text.append(f"  {self.note}", style=p.warn)
        if self.behind:
            text.append("  ↓ new output · ctrl+end", style=f"bold {p.accent}")
        self.update(text)


@dataclass(frozen=True)
class Suggestion:
    """One row of the popup above the prompt."""

    value: str  # what Tab inserts / Enter runs, e.g. "/model" or "@src/app.py"
    label: str
    hint: str = ""  # argument hint, dim: "[provider/model]"
    detail: str = ""  # description, dim
    tag: str = ""  # accent badge, e.g. "current"
    section: str | None = None  # a header row is drawn above the first row of each section


class Suggestions(OptionList):
    """Slash-command and @file popup that sits directly above the prompt.

    Every row is selectable and the list scrolls; section headers are disabled rows, so the
    arrow keys skip them. Nothing is capped: ``/`` shows all commands.
    """

    def show_items(self, items: list[Suggestion], subtitle: str = "") -> None:
        self.clear_options()
        self.border_subtitle = subtitle
        if not items:
            self.display = False
            return
        label_w = max(len(i.label) for i in items)
        tag_w = max((len(i.tag) + 4 for i in items if i.tag), default=0)  # "● " + tag + gap
        options: list[Option] = []
        section: str | None = None
        for item in items:
            if item.section and item.section != section:
                section = item.section
                options.append(Option(Text(f" {section.upper()}"), id=None, disabled=True))
            row = Text(no_wrap=True, overflow="ellipsis")
            row.append(item.label.ljust(label_w + 2), style="bold")
            if tag_w:
                row.append(f"● {item.tag}".ljust(tag_w) if item.tag else " " * tag_w, style="bold")
            row.append(item.detail, style="dim")
            if item.hint:
                row.append(f"  {item.hint}", style="italic dim")
            options.append(Option(row, id=item.value))
        self.add_options(options)
        self.highlighted = next(
            (n for n, option in enumerate(options) if not option.disabled), None
        )
        self.display = True

    def hide(self) -> None:
        self.display = False

    def selected_value(self) -> str | None:
        if not self.display or self.highlighted is None:
            return None
        option = self.get_option_at_index(self.highlighted)
        return None if option.disabled or option.id is None else str(option.id)


class PromptArea(TextArea):
    """Enter sends; Ctrl+J / Shift+Enter inserts a newline; Up/Down walk the history; Tab
    completes the popup; big pastes collapse into a token that expands on send."""

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class Accept(Message):
        """Tab/Enter while the suggestion popup is open. Enter also runs a chosen command."""

        def __init__(self, *, submit: bool) -> None:
            super().__init__()
            self.submit = submit

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            show_line_numbers=False,
            soft_wrap=True,
            tab_behavior="focus",
            compact=True,
            highlight_cursor_line=False,
            placeholder="Message Kalash…",
            **kwargs,
        )
        self.suggestions: Suggestions | None = None
        self.prompt_history: list[str] = []
        self._history_index: int | None = None
        self._draft = ""
        self._pastes: dict[int, str] = {}
        self._paste_counter = 0

    # -- helpers -----------------------------------------------------------------------

    def expanded_text(self) -> str:
        def restore(match: re.Match[str]) -> str:
            return self._pastes.get(int(match.group(1)), match.group(0))

        return PASTE_TOKEN.sub(restore, self.text)

    def reset(self) -> None:
        self.clear()
        self._pastes.clear()
        self._history_index = None

    def set_text(self, text: str) -> None:
        self.load_text(text)
        self.move_cursor(self.document.end)

    def _popup_open(self) -> bool:
        return self.suggestions is not None and bool(self.suggestions.display)

    # -- events ------------------------------------------------------------------------

    async def _on_paste(self, event: events.Paste) -> None:
        event.stop()
        event.prevent_default()
        text = event.text.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.count("\n") + 1
        if lines >= PASTE_MIN_LINES or len(text) >= PASTE_MIN_CHARS:
            self._paste_counter += 1
            self._pastes[self._paste_counter] = text
            self.insert(f"[Pasted text #{self._paste_counter} +{lines} lines]")
        else:
            self.insert(text)

    async def _on_key(self, event: events.Key) -> None:
        key = event.key
        if self._popup_open() and self.suggestions is not None:
            if key in ("up", "down"):
                event.stop()
                event.prevent_default()
                (
                    self.suggestions.action_cursor_up
                    if key == "up"
                    else self.suggestions.action_cursor_down
                )()
                return
            if key in ("tab", "enter"):
                event.stop()
                event.prevent_default()
                self.post_message(self.Accept(submit=key == "enter"))
                return
            if key == "escape":
                event.stop()
                event.prevent_default()
                self.suggestions.hide()
                return
        if event.character == "?" and not self.text:  # "?" on an empty prompt opens help
            event.stop()
            event.prevent_default()
            self.app.action_help()  # type: ignore[attr-defined]
            return
        if key == "enter":
            event.stop()
            event.prevent_default()
            text = self.expanded_text().strip()
            if text:
                self.post_message(self.Submitted(text))
            return
        if key in ("ctrl+j", "shift+enter"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        row = self.cursor_location[0]
        if key == "up" and row == 0 and self.prompt_history:
            event.stop()
            event.prevent_default()
            self._walk_history(-1)
            return
        if (
            key == "down"
            and row == self.document.line_count - 1
            and self._history_index is not None
        ):
            event.stop()
            event.prevent_default()
            self._walk_history(1)
            return
        await super()._on_key(event)

    def _walk_history(self, step: int) -> None:
        if self._history_index is None:
            self._draft = self.text
            self._history_index = len(self.prompt_history)
        index = self._history_index + step
        if index >= len(self.prompt_history):
            self._history_index = None
            self.set_text(self._draft)
            return
        self._history_index = max(0, index)
        self.set_text(self.prompt_history[self._history_index])
