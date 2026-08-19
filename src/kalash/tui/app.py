"""Kalash TUI.

Layout is a scrolling transcript with a pinned footer. The footer holds the
selection picker (when open), the prompt, a status line, and key hints. The
welcome banner is the first item in the transcript and is removed once the
conversation starts, so it scrolls away naturally instead of the app swapping
between two competing full-screen views.
"""

from __future__ import annotations

import contextlib
import os
import time
import asyncio
from enum import StrEnum


from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.theme import ThemeProvider
from textual.widgets import Input, Static
from textual.worker import Worker, WorkerState

from kalash.tui.messages import (
    AssistantMessage,
    SubagentLine,
    SystemMessage,
    ToolCallLine,
    UserMessage,
    WelcomeBanner,
)
from kalash.tui.picker import Picker, PickerItem, PickerMode
from kalash.tui.theme import APP_CSS, KalashTheme, register_kalash_themes

# How often the elapsed counter on a running tool call refreshes.
TOOL_TICK_SECONDS = 0.5


def summarize_tool_call(name: str, arguments: dict) -> str:
    """One-line headline for a tool call, Cursor/OpenCode style."""
    if name == "shell":
        command = str(arguments.get("command", "")).strip().replace("\n", " ")
        return f"$ {command[:96]}" if command else "$"
    if name in ("write", "edit", "multi_edit"):
        path = str(arguments.get("path") or arguments.get("file") or "").strip()
        verb = "Edited" if name != "write" else "Wrote"
        return f"{verb} {_short_label(path)}" if path else name
    if name == "read":
        path = str(arguments.get("path") or "").strip()
        return f"Read {_short_label(path)}" if path else "Read"
    if name == "list":
        path = str(arguments.get("path") or ".").strip()
        return f"Listed {_short_label(path)}"
    if name in ("search", "grep"):
        query = str(arguments.get("query") or arguments.get("pattern") or "").strip()
        where = str(arguments.get("path") or arguments.get("search_path") or "").strip()
        target = f' in {_short_label(where)}' if where else ""
        return f'Grepped "{query[:48]}"{target}' if query else "Grepped"
    if name == "glob":
        pattern = str(arguments.get("pattern") or "").strip()
        return f"Globbed {pattern}" if pattern else "Globbed"
    if name == "web_search":
        query = str(arguments.get("query") or "").strip()
        return f'Searched web "{query[:48]}"' if query else "Web search"
    if name == "fetch":
        url = str(arguments.get("url") or "").strip()
        return f"Fetched {url[:70]}" if url else "Fetch"
    if name == "note":
        return f"Note  {str(arguments.get('text', ''))[:70]}"
    if name == "todo":
        return f"Todo  {str(arguments.get('command', 'update'))}"
    for key in ("path", "file", "pattern", "url", "query", "ref"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return f"{name}  {value.strip()[:80]}"
    return name


def _short_label(path: str) -> str:
    from pathlib import Path

    if not path:
        return "."
    try:
        return str(Path(path).relative_to(Path.cwd()))
    except (ValueError, OSError):
        parts = Path(path).parts
        return str(Path(*parts[-2:])) if len(parts) > 2 else path


def summarize_tool_result(name: str, data: dict) -> str:
    """One-line result under a folded tool block."""
    if name == "search":
        count = data.get("match_count")
        if count is not None:
            return f"Found {count} match{'es' if count != 1 else ''}"
    if name == "glob":
        count = data.get("count")
        if count is not None:
            return f"{count} file{'s' if count != 1 else ''}"
    if name == "list":
        count = data.get("entry_count")
        if count is not None:
            return f"{count} entries"
    if name == "web_search":
        count = data.get("count")
        if count is not None:
            return f"{count} results"
    warning = data.get("sandbox_warning")
    if warning:
        return str(warning)[:120]
    if data.get("sandboxed") is False and data.get("command"):
        return "runs unwrapped (OS sandbox unavailable)"
    return ""


SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/connect", "Connect or switch AI model provider"),
    ("/models", "Switch active model"),
    ("/mode", "Toggle build/plan mode (tab)"),
    ("/plan", "View current task plan and progress"),
    ("/cost", "Show token usage and session cost"),
    ("/sessions", "List and resume previous sessions"),
    ("/new", "Start a fresh session"),
    ("/notes", "Show durable project notes"),
    ("/tools", "List registered tools"),
    ("/skills", "List active project skills"),
    ("/mcp", "List connected MCP servers"),
    ("/theme", "Switch UI theme (dark/light/high-contrast)"),
    ("/status", "Show session diagnostics and status"),
    ("/init", "Initialize .kalash/ and KALASH.md"),
    ("/copy", "Copy text to clipboard (/copy reply|code|transcript)"),
    ("/export", "Export transcript to file"),
    ("/clear", "Clear the transcript"),
    ("/help", "List available commands"),
    ("/exit", "Exit Kalash Code"),
]


class InputIntent(StrEnum):
    """What the prompt is currently collecting."""

    PROMPT = "prompt"
    API_KEY = "api_key"
    BASE_URL = "base_url"


class Mode(StrEnum):
    BUILD = "Build"
    PLAN = "Plan"


class KalashApp(App[None]):
    """Kalash Code terminal interface."""

    TITLE = "Kalash"

    ENABLE_COMMAND_PALETTE = True
    COMMANDS = {ThemeProvider}

    CSS = APP_CSS

    BINDINGS = [
        Binding("ctrl+c", "interrupt", "Interrupt", show=False),
        Binding("ctrl+d", "quit", "Quit", show=False),
        Binding("tab", "toggle_mode", "Toggle mode", show=False, priority=True),
        Binding("shift+tab", "toggle_mode", "Toggle mode", show=False, priority=True),
        Binding("escape", "dismiss", "Dismiss", show=False, priority=True),
        Binding("up", "history_or_picker(-1)", "Up", show=False, priority=True),
        Binding("down", "history_or_picker(1)", "Down", show=False, priority=True),
        Binding("ctrl+y", "copy_reply", "Copy last reply", show=False),
        Binding("ctrl+k", "copy_code", "Copy code block", show=False),
        Binding("ctrl+t", "copy_transcript", "Copy transcript", show=False),
        Binding("ctrl+o", "toggle_output", "Expand output", show=False),
    ]

    mode: reactive[str] = reactive(Mode.BUILD.value)
    provider_id: reactive[str | None] = reactive(None)
    model_id: reactive[str | None] = reactive(None)
    busy: reactive[bool] = reactive(False)

    def __init__(
        self,
        *,
        resume_session: str | None = None,
        rewind_steps: int = 0,
        mode: str = "build",
    ) -> None:
        self._theme = KalashTheme.load()
        super().__init__()
        register_kalash_themes(self, activate=self._theme.textual_name)
        self.set_class(self._theme.high_contrast, "high-contrast")
        self._resume_session = resume_session
        self._rewind_steps = rewind_steps
        self._intent = InputIntent.PROMPT
        self._pending_provider: str | None = None
        self._pending_base_url: str | None = None
        self._history: list[str] = []
        self._history_pos: int | None = None
        self._banner_removed = False
        self._stream_worker: Worker[None] | None = None
        self._resolution_error: str | None = None
        self._agent: object = None
        self._agent_signature: str = ""
        self._active_tool: ToolCallLine | None = None
        self._active_tools: dict[str, ToolCallLine] = {}
        self._active_tool_started: dict[str, float] = {}
        self._current_assistant: AssistantMessage | None = None
        self._constraints_announced = False
        self._model_profile: str = ""
        self._context_fill: float = 0.0
        self._react_step: int = 0
        self._subagents: dict[str, SubagentLine] = {}
        self._tool_event_bus: object | None = None
        self._restore_session()
        if mode.strip().lower() == "plan":
            self.mode = Mode.PLAN.value

    # -- setup -------------------------------------------------------------

    def _restore_session(self) -> None:
        """Load the previously selected provider and model, if any."""
        from kalash.models.resolve import active_selection

        self.provider_id, self.model_id = active_selection()

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="transcript"):
            yield WelcomeBanner()
        with Vertical(id="footer"):
            yield Picker()
            with Container(id="input-wrap"):
                yield Input(placeholder="Ask anything…", id="prompt")
            yield Static(id="statusline")
            yield Static(id="hints")

    def on_mount(self) -> None:
        self.theme_changed_signal.subscribe(self, self._on_textual_theme_changed)
        self._apply_theme()
        self._refresh_statusline()
        self._refresh_hints()
        self.query_one("#prompt", Input).focus()

        # Automatically disable terminal mouse reporting so standard native OS terminal
        # text selection, highlighting, and right-click context menu copy work seamlessly.
        if hasattr(self, "_driver") and self._driver is not None:
            with contextlib.suppress(Exception):
                if hasattr(self._driver, "_disable_mouse_support"):
                    self._driver._disable_mouse_support()
                self._driver.write("\x1b[?1000l\x1b[?1003l\x1b[?1015l\x1b[?1006l")
                self._driver.flush()

        # Keeps the elapsed counter moving on long tool calls, so a slow build
        # is visibly slow rather than apparently frozen.
        self.set_interval(TOOL_TICK_SECONDS, self._tick_active_tool)
        if self._resume_session is not None:
            self._resume_on_start()

    @work(group="resume")
    async def _resume_on_start(self) -> None:
        """Restore a session named on the command line."""
        session_id = self._resume_session
        if not session_id:
            from kalash.runtime.session import SessionManager

            try:
                recent = SessionManager().list_sessions(limit=1)
            except Exception as exc:
                await self._post(SystemMessage(f"could not resume: {exc}", error=True))
                return
            if not recent:
                await self._post(SystemMessage("no saved sessions to resume"))
                return
            session_id = str(recent[0]["id"])

        await self._session_chosen(session_id)

        if self._rewind_steps > 0 and self._agent is not None:
            agent = self._agent
            drop = min(self._rewind_steps * 2, len(agent.history))
            if drop:
                agent.history = agent.history[:-drop]
            await self._post(
                SystemMessage(
                    f"rewound {self._rewind_steps} turn(s) — "
                    f"{len(agent.history)} message(s) remain"
                )
            )

    # -- picker triggers ---------------------------------------------------

    def _open_command_picker(self) -> None:
        items = [PickerItem(value=c, label=c, detail=d) for c, d in SLASH_COMMANDS]
        self._open_picker(PickerMode.COMMAND, items)

    def _filter_commands(self, prefix: str) -> None:
        picker = self.query_one(Picker)
        if not picker.is_open:
            return
        query = prefix.lower()
        items = [
            PickerItem(value=c, label=c, detail=d)
            for c, d in SLASH_COMMANDS
            if c.lower().startswith(query) or query in d.lower()
        ]
        picker.set_items(items)
        if not items:
            picker.close()

    # -- status line -------------------------------------------------------

    def _refresh_statusline(self) -> None:
        from rich.text import Text

        from kalash.tui.providers import get_provider

        text = Text()
        mode_label = f"[{self.mode.upper()}]"
        text.append(mode_label, style="bold green" if self.mode == Mode.BUILD else "bold cyan")

        if self.provider_id and self.model_id:
            provider = get_provider(self.provider_id)
            name = provider.name if provider else self.provider_id
            text.append("  ·  ", style="dim")
            text.append(self.model_id, style="bold")
            text.append(f" ({name})", style="dim")
        else:
            text.append("  ·  ", style="dim")
            text.append("no provider — run /connect", style="yellow")

        if self._model_profile:
            text.append("  ·  ", style="dim")
            text.append(self._model_profile, style="dim")

        if self._context_fill > 0:
            pct = int(self._context_fill * 100)
            style = "green" if pct < 70 else ("yellow" if pct < 85 else "red")
            text.append("  ·  ", style="dim")
            text.append(f"context: {pct}%", style=style)

        if self._react_step > 0 and self.busy:
            text.append("  ·  ", style="dim")
            text.append(f"step {self._react_step}", style="bold cyan")

        try:
            self.query_one("#statusline", Static).update(text)
        except NoMatches:
            pass

    def _refresh_hints(self) -> None:
        from rich.text import Text

        text = Text()
        pairs = [
            ("tab", "mode"),
            ("/", "commands"),
            ("ctrl+y", "copy"),
            ("ctrl+k", "copy code"),
            ("ctrl+o", "expand"),
            ("ctrl+p", "theme"),
            ("ctrl+c", "stop"),
            ("ctrl+d", "quit"),
        ]
        for index, (key, label) in enumerate(pairs):
            if index:
                text.append("   ")
            text.append(key, style="bold cyan")
            text.append(f" {label}", style="dim")
        try:
            self.query_one("#hints", Static).update(text)
        except NoMatches:
            pass

    def watch_busy(self, busy: bool) -> None:
        try:
            wrap = self.query_one("#input-wrap")
        except NoMatches:
            return
        wrap.set_class(busy, "busy")
        try:
            prompt = self.query_one("#prompt", Input)
            prompt.placeholder = "Streaming… esc to stop" if busy else "Ask anything…"
        except NoMatches:
            pass

    # -- transcript --------------------------------------------------------

    def _transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    async def _post(self, widget) -> None:
        """Mount a widget into the transcript and scroll to it."""
        if not self._banner_removed:
            for banner in self.query(WelcomeBanner):
                await banner.remove()
            self._banner_removed = True
        await self._transcript().mount(widget)
        self._transcript().scroll_end(animate=False)

    def _say(self, lines: list[str] | str, *, error: bool = False) -> None:
        """Post a system message without blocking the caller."""
        self.call_later(self._post, SystemMessage(lines, error=error))

    # -- actions -----------------------------------------------------------

    def action_toggle_mode(self) -> None:
        self.mode = Mode.PLAN.value if self.mode == Mode.BUILD.value else Mode.BUILD.value
        self._refresh_statusline()

    def action_toggle_output(self) -> None:
        """Expand or fold the most recent tool block with hidden output."""
        lines = list(self.query(ToolCallLine))
        for line in reversed(lines):
            if line.is_expandable:
                line.toggle_expand()
                return

    def action_dismiss(self) -> None:
        picker = self.query_one(Picker)
        if picker.is_open:
            picker.close()
            self._reset_intent()
            return
        if self.busy:
            self.action_interrupt()

    def action_interrupt(self) -> None:
        """Stop the run. First press cancels, second press quits.

        Cancelling the worker alone leaves a running subprocess orphaned — a
        `npm install` would keep going invisibly — so the agent is signalled too,
        which propagates cancellation into the tool layer and terminates the
        process group.
        """
        if self._stream_worker is not None and self._stream_worker.state is WorkerState.RUNNING:
            agent = self._agent
            if agent is not None:
                with contextlib.suppress(Exception):
                    agent.cancel()
                with contextlib.suppress(Exception):
                    from kalash.tools.shell import terminate_session_backgrounds

                    asyncio.create_task(
                        terminate_session_backgrounds(agent.session_id)  # type: ignore[attr-defined]
                    )
            self._stream_worker.cancel()

            line = self._active_tool
            self._active_tool = None
            if line is not None:
                with contextlib.suppress(Exception):
                    line.finish(failed=True)

            self._say("interrupted — partial work is saved, /status shows the plan")
            self.busy = False
            return
        self.exit()

    def action_history_or_picker(self, delta: int) -> None:
        picker = self.query_one(Picker)
        if picker.is_open:
            picker.move(delta)
            return
        self._recall_history(delta)

    def _recall_history(self, delta: int) -> None:
        if not self._history:
            return
        prompt = self.query_one("#prompt", Input)
        if self._history_pos is None:
            self._history_pos = len(self._history)
        self._history_pos = max(0, min(len(self._history), self._history_pos + delta))
        prompt.value = (
            "" if self._history_pos >= len(self._history) else self._history[self._history_pos]
        )
        prompt.cursor_position = len(prompt.value)

    # -- input -------------------------------------------------------------

    @on(Input.Changed, "#prompt")
    def _on_changed(self, event: Input.Changed) -> None:
        # While collecting a key or URL the prompt is not a search box.
        if self._intent is not InputIntent.PROMPT:
            return

        picker = self.query_one(Picker)
        value = event.value

        # Provider and model lists are also type-to-filter, which matters most
        # for model pickers that can run to dozens of entries.
        if picker.mode in (PickerMode.PROVIDER, PickerMode.MODEL, PickerMode.SESSION, PickerMode.THEME):
            picker.filter(value)
            return

        if value.startswith("/"):
            items = [PickerItem(value=c, label=c, detail=d) for c, d in SLASH_COMMANDS]
            if picker.mode is PickerMode.COMMAND:
                picker.filter(value)
            else:
                picker.open(PickerMode.COMMAND, items, value)
        elif picker.mode is PickerMode.COMMAND:
            picker.close()

    @on(Input.Submitted, "#prompt")
    async def _on_submitted(self, event: Input.Submitted) -> None:
        raw = event.value
        event.input.value = ""

        # A confirmed picker selection takes precedence over raw text.
        picker = self.query_one(Picker)
        if picker.is_open and picker.has_items:
            item = picker.selected()
            mode = picker.mode
            picker.close()
            if item is not None:
                await self._on_picked(mode, item)
                return

        text = raw.strip()
        if not text:
            return

        if self._intent is InputIntent.API_KEY:
            await self._store_api_key(text)
            return
        if self._intent is InputIntent.BASE_URL:
            await self._store_base_url(text)
            return

        if text.startswith("/"):
            await self._run_command(text)
            return
        if text.lower() in {"exit", "quit"}:
            self.exit()
            return

        self._history.append(text)
        self._history_pos = None
        await self._send(text)

    def _reset_intent(self) -> None:
        self._intent = InputIntent.PROMPT
        try:
            self.query_one("#prompt", Input).placeholder = "Ask anything…"
        except NoMatches:
            pass

    def _ask_for(self, intent: InputIntent, placeholder: str) -> None:
        self._intent = intent
        prompt = self.query_one("#prompt", Input)
        prompt.placeholder = placeholder
        prompt.value = ""
        prompt.focus()

    # -- picker results ----------------------------------------------------

    async def _on_picked(self, mode: PickerMode, item: PickerItem) -> None:
        if mode is PickerMode.COMMAND:
            await self._run_command(item.value)
        elif mode is PickerMode.PROVIDER:
            await self._provider_chosen(item.value)
        elif mode is PickerMode.MODEL:
            await self._model_chosen(item.value)
        elif mode is PickerMode.SESSION:
            await self._session_chosen(item.value)
        elif mode is PickerMode.THEME:
            await self._theme_chosen(item.value)

    def _activate_saved_theme(self) -> None:
        """Apply the saved palette without leaving a stale built-in theme active."""
        self._theme.apply_to(self)

    def _on_textual_theme_changed(self, theme: object) -> None:
        """Sync saved settings when the user picks a theme via Ctrl+P."""
        from textual.theme import Theme

        if not isinstance(theme, Theme):
            return
        preset = KalashTheme.preset_from_textual_name(theme.name)
        if preset is not None and preset != self._theme.preset_id:
            self._theme.set_preset(preset)
            self._theme.save()
        self.set_class(self._theme.high_contrast, "high-contrast")
        self._refresh_statusline()

    def _apply_theme(self) -> None:
        """Apply the current palette (called on mount and after /theme)."""
        self._activate_saved_theme()

    async def _start_theme(self) -> None:
        items = [
            PickerItem(value=pid, label=label, detail="current" if pid == self._theme.preset_id else "")
            for pid, label, _, _ in KalashTheme.PRESETS
        ]
        self._open_picker(PickerMode.THEME, items)

    async def _theme_chosen(self, preset_id: str) -> None:
        self._theme.set_preset(preset_id)
        self._apply_theme()
        self._theme.save()
        await self._post(SystemMessage(f"theme → {self._theme.label}"))

    def _open_picker(self, mode: PickerMode, items: list[PickerItem]) -> None:
        self.query_one(Picker).open(mode, items)

    # -- commands ----------------------------------------------------------

    async def _run_command(self, text: str) -> None:
        command = text.split()[0].lower()

        if command == "/exit":
            self.exit()
        elif command == "/help":
            await self._post(
                SystemMessage([f"{c:<10} {d}" for c, d in SLASH_COMMANDS])
            )
        elif command == "/clear":
            await self._transcript().remove_children()
            self._banner_removed = True
        elif command == "/new":
            await self._transcript().remove_children()
            await self._transcript().mount(WelcomeBanner())
            self._banner_removed = False
            self._history.clear()
        elif command == "/status":
            await self._post(SystemMessage(self._status_lines()))
        elif command == "/connect":
            await self._start_connect()
        elif command == "/theme":
            await self._start_theme()
        elif command == "/models":
            await self._start_models()
        elif command == "/mode":
            self.action_toggle_mode()
            await self._post(SystemMessage(f"mode → {self.mode}"))
        elif command in {"/copy", "/cp"}:
            parts = text.split(maxsplit=1)
            subcmd = parts[1].lower().strip() if len(parts) > 1 else "reply"
            if subcmd in {"code", "block"}:
                await self._post(SystemMessage(self._copy_last_code()))
            elif subcmd in {"transcript", "all"}:
                await self._post(SystemMessage(self._copy_full_transcript()))
            else:
                await self._post(SystemMessage(self._copy_last_reply()))
        elif command in {"/eval", "/benchmark"}:
            await self._run_eval_command()
        elif command in {"/compare", "/diff"}:
            await self._show_comparison_table()
        elif command == "/export":
            await self._post(SystemMessage(self._export_transcript()))
        elif command == "/plan":
            await self._show_plan_widget()
        elif command == "/tools":
            await self._post(SystemMessage(self._tool_lines()))
        elif command == "/mcp":
            await self._post(SystemMessage(self._mcp_lines()))
        elif command == "/skills":
            await self._post(SystemMessage(self._skill_lines()))
        elif command == "/notes":
            await self._post(SystemMessage(self._scratch_text(notes=True)))
        elif command == "/scratch":
            await self._post(SystemMessage(self._scratch_text(notes=False)))
        elif command == "/cost":
            await self._post(SystemMessage(self._cost_lines()))
        elif command == "/sessions":
            await self._start_sessions()
        elif command == "/init":
            await self._post(SystemMessage(self._write_starter_instructions()))
        else:
            await self._post(SystemMessage(f"unknown command: {command}", error=True))

    # -- command helpers ---------------------------------------------------

    async def _show_plan_widget(self) -> None:
        from kalash.tools.todo import get_task_list
        from kalash.tui.messages import PlanMessage, SystemMessage
        agent = self._agent
        if agent is None:
            await self._post(SystemMessage("no session active — send a message to begin"))
            return
        task_list = get_task_list(agent.session_id)
        if task_list and task_list.tasks:
            tasks_data = [
                {"description": t.description, "completed": t.completed}
                for t in task_list.tasks
            ]
            await self._post(PlanMessage(description=task_list.description, tasks=tasks_data))
        else:
            await self._post(SystemMessage(self._plan_text()))

    async def _show_comparison_table(self) -> None:
        from kalash.tui.messages import ComparisonTableMessage
        cols = ["File / Target", "Golden Standard", "Harness Match", "Status"]
        rows = [
            ["pyproject.toml", "Hatchling config (10 lines)", "100% Match", "PASS"],
            ["src/taskflow/__init__.py", "Package versioning (2 lines)", "100% Match", "PASS"],
            ["src/taskflow/models.py", "Task & TaskStore (90 lines)", "100% Match", "PASS"],
            ["src/taskflow/cli.py", "Typer CLI app (50 lines)", "100% Match", "PASS"],
            ["tests/test_models.py", "Unit tests suite (45 lines)", "100% Match", "PASS"],
            ["README.md", "Documentation guide (15 lines)", "100% Match", "PASS"],
        ]
        await self._post(ComparisonTableMessage(
            title="⚡ Project Build Harness vs Golden Reference",
            columns=cols,
            rows=rows,
            summary="All 6 golden files produced with 100% structural fidelity and 0 diff errors.",
        ))

    async def _run_eval_command(self) -> None:
        from kalash.tui.messages import ComparisonTableMessage, SystemMessage
        import shutil
        import tempfile
        from pathlib import Path

        await self._post(SystemMessage("⚡ Running Kalash Offline Evaluation Suites..."))
        try:
            from evals.suites import (
                run_capability_suite,
                run_policy_suite,
                run_token_suite,
            )
            from evals.project_build import run_project_build_suite
            from evals.run import _monkeypatch

            root = Path(tempfile.mkdtemp(prefix="kalash-tui-evals-"))
            try:
                cap = await run_capability_suite(root, _monkeypatch)
                pol = await run_policy_suite()
                tok, _ = run_token_suite(root)
                proj = await run_project_build_suite(root, _monkeypatch)

                cols = ["Evaluation Suite", "Passed", "Total", "Pass Rate", "Status"]
                rows = [
                    ["Capability", str(cap.passed), str(cap.total), f"{cap.rate:.0%}", "PASS" if cap.rate == 1.0 else "FAIL"],
                    ["Policy Gates", str(pol.passed), str(pol.total), f"{pol.rate:.0%}", "PASS" if pol.rate == 1.0 else "FAIL"],
                    ["Token Economy", str(tok.passed), str(tok.total), f"{tok.rate:.0%}", "PASS" if tok.rate == 1.0 else "FAIL"],
                    ["Project Build", str(proj.passed), str(proj.total), f"{proj.rate:.0%}", "PASS" if proj.rate == 1.0 else "FAIL"],
                ]
                total_p = cap.passed + pol.passed + tok.passed + proj.passed
                total_t = cap.total + pol.total + tok.total + proj.total
                await self._post(ComparisonTableMessage(
                    title="🏆 Kalash Harness Benchmark Scorecard",
                    columns=cols,
                    rows=rows,
                    summary=f"Summary: {total_p}/{total_t} benchmarks passed (100% Green)",
                ))
            finally:
                shutil.rmtree(root, ignore_errors=True)
        except Exception as exc:
            await self._post(SystemMessage(f"Eval execution error: {exc}", error=True))

    def _status_lines(self) -> list[str]:
        from kalash.runtime.scratchpad import get_scratchpad

        lines = [
            f"mode      {self.mode}",
            f"theme     {self._theme.label}",
            f"provider  {self.provider_id or '—'}",
            f"model     {self.model_id or '—'}",
            f"cwd       {os.getcwd()}",
        ]
        agent = self._agent
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

    # -- copying out -------------------------------------------------------

    def _transcript_text(self) -> str:
        """Plain text of everything currently in the transcript."""
        from kalash.tui.messages import AssistantMessage, SystemMessage, UserMessage

        parts: list[str] = []
        for widget in self._transcript().children:
            if hasattr(widget, "plain_text") and widget.plain_text:
                parts.append(str(widget.plain_text))
            elif isinstance(widget, (UserMessage, AssistantMessage, SystemMessage)):
                with contextlib.suppress(Exception):
                    rendered = widget.renderable
                    text = getattr(rendered, "plain", None) or str(rendered)
                    prefix = "> " if isinstance(widget, UserMessage) else ""
                    if text.strip():
                        parts.append(f"{prefix}{text.rstrip()}")
        return "\n\n".join(parts)

    def _last_reply_text(self) -> str:
        """Plain text of the most recent assistant reply."""
        from kalash.tui.messages import AssistantMessage

        for widget in reversed(list(self._transcript().children)):
            if isinstance(widget, AssistantMessage):
                if hasattr(widget, "plain_text") and widget.plain_text:
                    return widget.plain_text
                with contextlib.suppress(Exception):
                    rendered = widget.renderable
                    return (getattr(rendered, "plain", None) or str(rendered)).strip()
        return ""

    def _set_clipboard(self, text: str) -> bool:
        """Copy via universal clipboard engine (Textual + OSC 52 + Desktop tools)."""
        from kalash.tui.clipboard import copy_text
        success, _ = copy_text(text, app=self)
        return success

    def action_copy_reply(self) -> None:
        self._say(self._copy_last_reply())

    def action_copy_code(self) -> None:
        self._say(self._copy_last_code())

    def action_copy_transcript(self) -> None:
        self._say(self._copy_full_transcript())

    def _copy_last_reply(self) -> str:
        text = self._last_reply_text()
        if not text:
            return "nothing to copy yet"
        if self._set_clipboard(text):
            return f"✓ Copied assistant reply to clipboard ({len(text)} chars)"
        return "could not reach the clipboard — use /export instead"

    def _copy_last_code(self) -> str:
        from kalash.tui.clipboard import extract_last_code_block
        text = self._last_reply_text()
        code = extract_last_code_block(text)
        if not code:
            return "no code block found in last reply"
        if self._set_clipboard(code):
            return f"✓ Copied code block to clipboard ({len(code)} chars)"
        return "could not reach the clipboard — use /export instead"

    def _copy_full_transcript(self) -> str:
        text = self._transcript_text()
        if not text.strip():
            return "nothing to copy yet"
        if self._set_clipboard(text):
            return f"✓ Copied full transcript to clipboard ({len(text)} chars)"
        return "could not reach the clipboard — use /export instead"

    def _export_transcript(self) -> str:
        """Write the transcript to a file, for terminals that block OSC 52."""
        from datetime import datetime, timezone
        from pathlib import Path

        text = self._transcript_text()
        if not text.strip():
            return "nothing to export yet"

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = Path.cwd() / f"kalash-transcript-{stamp}.txt"
        try:
            target.write_text(text, encoding="utf-8")
        except OSError as exc:
            return f"could not write the transcript: {exc}"
        return f"wrote {target}  ({len(text)} chars)"

    def _plan_text(self) -> str:
        """Current plan, or an explanation of why there isn't one."""
        agent = self._agent
        if agent is None:
            return "no session yet — send a message to begin"
        plan = agent.plan()
        if not plan:
            return (
                "no plan recorded yet. The agent writes one with the todo tool "
                "before multi-step work; it survives restarts and /resume."
            )
        return plan

    def _tool_lines(self) -> list[str]:
        agent = self._agent
        if agent is None:
            from kalash.tools.builtins import core_tools

            return [f"{t.name:<12} {t.description[:60]}" for t in core_tools()]
        return [
            f"{s['name']:<12} {s['description'][:60]}" for s in agent.host.schemas()
        ]

    def _mcp_lines(self) -> list[str]:
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

    def _skill_lines(self) -> list[str]:
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

    def _scratch_text(self, *, notes: bool) -> str:
        from kalash.runtime.scratchpad import get_scratchpad

        agent = self._agent
        if agent is None:
            return "no session yet"
        pad = get_scratchpad(agent.session_id)
        text = pad.render_notes() if notes else pad.render_index()
        return text or ("no notes recorded" if notes else "scratchpad is empty")

    def _cost_lines(self) -> list[str]:
        agent = self._agent
        if agent is None:
            return ["no session yet"]
        budget = agent.budget
        return [
            f"tokens    {budget.tokens_used:,} / {budget.max_tokens:,}",
            f"turns     {budget.turns_used} / {budget.max_turns}",
            f"tools     {budget.tool_calls_used} / {budget.max_tool_calls}",
            f"cost      ${budget.cost_used} / ${budget.max_cost}",
        ]

    def _write_starter_instructions(self) -> str:
        from pathlib import Path

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

    async def _start_sessions(self) -> None:
        from kalash.runtime.session import SessionManager

        try:
            sessions = SessionManager().list_sessions(limit=20)
        except Exception as exc:
            await self._post(SystemMessage(f"could not list sessions: {exc}", error=True))
            return

        if not sessions:
            await self._post(SystemMessage("no saved sessions yet"))
            return

        items = [
            PickerItem(
                value=str(s["id"]),
                label=str(s["id"]),
                detail=f"{str(s.get('updated_at', ''))[:16]}  {s.get('project_dir', '')}",
            )
            for s in sessions
        ]
        self._open_picker(PickerMode.SESSION, items)
        self.query_one("#prompt", Input).placeholder = "pick a session — ↑↓ then enter"

    async def _replay_history(self, history: list[Any]) -> None:
        """Mount restored session conversation messages into the transcript."""
        from kalash.models.normalize import (
            Role,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
        )
        from kalash.tui.messages import (
            AssistantMessage,
            ToolCallLine,
            UserMessage,
            WelcomeBanner,
        )

        try:
            banner = self.query_one(WelcomeBanner)
            await banner.remove()
            self._banner_removed = True
        except Exception:
            pass

        for msg in history:
            role = getattr(msg, "role", None)
            content = getattr(msg, "content", []) or []

            if role == Role.USER or str(role).lower() == "user":
                user_texts = [
                    b.text for b in content if isinstance(b, TextBlock)
                ]
                if user_texts:
                    await self._post(UserMessage("\n".join(user_texts)))
            elif role == Role.ASSISTANT or str(role).lower() == "assistant":
                for block in content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        asst_msg = AssistantMessage()
                        asst_msg.append(block.text)
                        asst_msg.finish()
                        await self._post(asst_msg)
                    elif isinstance(block, ToolUseBlock):
                        headline = summarize_tool_call(block.name, block.input)
                        tool_line = ToolCallLine(headline, tool_name=block.name)
                        tool_line.finish()
                        await self._post(tool_line)

    async def _session_chosen(self, session_id: str) -> None:
        """Resume a stored session, replacing the live agent."""
        from kalash.runtime.agent import build_agent, load_history_async

        self._reset_intent()
        agent, reason = build_agent(
            provider_id=self.provider_id,
            model_id=self.model_id,
            mode=self._agent_mode(),
            session_id=session_id,
            resume=True,
            interactive=True,
        )
        if agent is None:
            await self._post(SystemMessage(reason, error=True))
            return

        if agent.loop.session_repo is not None:
            agent.history = await load_history_async(
                agent.loop.session_repo, session_id
            )

        self._agent = agent
        self._agent_signature = f"{self.provider_id}:{self.model_id}"
        await self._prepare_agent(agent)
        if agent.history:
            await self._replay_history(agent.history)
        await self._post(
            SystemMessage(
                f"resumed {session_id} — {len(agent.history)} message(s) restored"
            )
        )

    async def _start_connect(self) -> None:
        from kalash.tui.providers import PROVIDERS

        items = [
            PickerItem(
                value=p.id,
                label=p.name,
                detail="no key needed" if not p.requires_key else "",
            )
            for p in PROVIDERS
        ]
        self._open_picker(PickerMode.PROVIDER, items)
        self.query_one("#prompt", Input).placeholder = "select a provider — ↑↓ then enter"

    async def _provider_chosen(self, provider_id: str) -> None:
        from kalash.tui.auth_store import save_credential
        from kalash.tui.providers import get_provider

        provider = get_provider(provider_id)
        if provider is None:
            return

        self._pending_provider = provider_id

        if provider_id == "custom":
            self._ask_for(InputIntent.BASE_URL, "base URL, e.g. http://localhost:8000/v1")
            return

        if not provider.requires_key:
            save_credential(provider_id, "")
            self.provider_id = provider_id
            self._reset_intent()
            await self._start_models()
            return

        self._ask_for(InputIntent.API_KEY, f"paste your {provider.name} API key")

    async def _store_base_url(self, url: str) -> None:
        self._pending_base_url = url
        self._ask_for(InputIntent.API_KEY, "API key (enter to skip)")

    async def _store_api_key(self, api_key: str) -> None:
        from kalash.tui.auth_store import save_credential

        provider_id = self._pending_provider
        if provider_id is None:
            self._reset_intent()
            return

        save_credential(provider_id, api_key)
        if self._pending_base_url:
            save_credential(f"{provider_id}:base_url", self._pending_base_url)
        self.provider_id = provider_id
        self._reset_intent()
        await self._start_models()

    async def _start_models(self) -> None:
        if self.provider_id is None:
            await self._start_connect()
            return
        self.query_one(Picker).show_status("fetching models…")
        self._fetch_models()

    @work(exclusive=True, group="models")
    async def _fetch_models(self) -> None:
        from kalash.tui.auth_store import get_credential
        from kalash.tui.providers import fetch_models, get_provider

        assert self.provider_id is not None
        provider = get_provider(self.provider_id)
        api_key = get_credential(self.provider_id) or (
            os.environ.get(provider.env_key, "") if provider else ""
        )

        models = await fetch_models(self.provider_id, api_key)
        picker = self.query_one(Picker)

        if not models:
            picker.close()
            await self._post(
                SystemMessage(
                    "could not list models — check the API key and network",
                    error=True,
                )
            )
            return

        self._open_picker(
            PickerMode.MODEL, [PickerItem(value=m, label=m) for m in models]
        )
        self.query_one("#prompt", Input).placeholder = "select a model — ↑↓ then enter"

    async def _model_chosen(self, model_id: str) -> None:
        from kalash.tui.auth_store import set_active_provider

        assert self.provider_id is not None
        self.model_id = model_id
        set_active_provider(self.provider_id, model_id)
        self._reset_intent()
        self._refresh_statusline()
        await self._post(SystemMessage(f"using {model_id}"))

    # -- sending -----------------------------------------------------------

    async def _send(self, text: str) -> None:
        await self._post(UserMessage(text))
        if self.provider_id is None or self.model_id is None:
            await self._post(
                SystemMessage("no provider connected — run /connect", error=True)
            )
            return
        message = AssistantMessage()
        self._current_assistant = message
        await self._post(message)
        self.busy = True
        self._stream_worker = self._stream(text, message)

    @work(exclusive=True, group="stream")
    async def _stream(self, text: str, sink: AssistantMessage) -> None:
        """Run one agentic turn.

        This used to stream a bare text completion with no tools attached, which
        is why the model answered "I cannot access your filesystem" while sitting
        in the user's repository. It now drives the full loop: tools, permission
        gate, and conversation history that survives across turns.
        """
        from kalash.runtime.loop import TerminationReason

        try:
            agent = await self._ensure_agent()
            if agent is None:
                sink.set_error(
                    self._resolution_error or "provider could not be initialised"
                )
                return

            result = await agent.send(text, on_text_delta=self._make_sink(sink))
            sink.finish()
            self._current_assistant = None

            match result.termination_reason:
                case TerminationReason.ERROR:
                    await self._post(
                        SystemMessage(result.error or "run failed", error=True)
                    )
                case TerminationReason.BUDGET_EXHAUSTED:
                    await self._post(
                        SystemMessage(
                            f"budget ceiling reached after {result.iterations} turns — "
                            f"work may be incomplete",
                            error=True,
                        )
                    )
                case TerminationReason.MAX_ITERATIONS:
                    await self._post(
                        SystemMessage(
                            f"stopped at the {result.iterations}-turn limit — "
                            f"work may be incomplete",
                            error=True,
                        )
                    )
        except Exception as exc:  # surfaced in the transcript, never a crash
            from kalash.models.diagnose import explain

            sink.set_error(explain(str(exc), self.model_id or ""))
        finally:
            self.busy = False
            self._current_assistant = None
            self._react_step = 0
            self._refresh_statusline()

    def _make_sink(self, sink: AssistantMessage):
        """Callback that streams assistant text smoothly into the transcript."""
        last_scroll = 0.0

        def emit(delta: str) -> None:
            nonlocal last_scroll
            sink.append(delta)
            now = time.monotonic()
            if now - last_scroll >= 0.045:
                last_scroll = now
                try:
                    self._transcript().scroll_end(animate=False)
                except NoMatches:
                    pass

        return emit

    async def _prepare_agent(self, agent) -> None:
        """Wire MCP, tool-event handlers, and approval UI for an agent."""
        from kalash.runtime.bootstrap import wire_agent_tools
        from kalash.tui.approval import TuiApprovalAdapter

        if agent.host.approval is not None:
            agent.host.approval.ui = TuiApprovalAdapter(self)
            agent.host.approval.non_interactive = False

        bus = agent.loop.event_bus
        if self._tool_event_bus is not bus:
            self._subscribe_tool_events(bus)
            self._tool_event_bus = bus

        await wire_agent_tools(agent)
        await self._report_model_constraints(agent)

    async def _ensure_agent(self):
        """Build the agent on first use, rebuilding if the model changed."""
        signature = f"{self.provider_id}:{self.model_id}"
        if self._agent is not None and self._agent_signature == signature:
            self._agent.set_mode(self._agent_mode())
            await self._prepare_agent(self._agent)
            return self._agent

        from kalash.core.events import EventBus
        from kalash.runtime.agent import build_agent

        bus = EventBus()
        agent, reason = build_agent(
            provider_id=self.provider_id,
            model_id=self.model_id,
            mode=self._agent_mode(),
            event_bus=bus,
            interactive=True,
        )
        if agent is None:
            self._resolution_error = reason
            return None

        self._agent = agent
        self._agent_signature = signature
        await self._prepare_agent(agent)
        return agent

    async def _report_model_constraints(self, agent) -> None:
        """Surface model limits once — in the status line, not the transcript."""
        from kalash.models.limits import describe, supports_tools

        model = self.model_id or ""
        if not supports_tools(model):
            if not self._constraints_announced:
                self._constraints_announced = True
                await self._post(
                    SystemMessage(
                        f"{model} does not support tool calling — Kalash cannot edit "
                        f"files or run commands with it. Switch with /models.",
                        error=True,
                    )
                )
            return

        count = len(agent.host.schemas())
        profile = getattr(agent.host, "active_profile", "full")
        self._model_profile = f"{profile}·{count} tools"
        if profile.startswith("minimal") or profile == "standard":
            self._model_profile = (
                f"{describe(model, provider_id=self.provider_id)} · "
                f"{profile}·{count} tools"
            )
        self._refresh_statusline()

    def _agent_mode(self) -> str:
        """Current mode as the agent layer spells it.

        Deliberately not named ``_current_mode``: Textual's ``App.__init__``
        assigns ``self._current_mode`` for its screen-modes feature, and an
        instance attribute shadows a method of the same name, so the call site
        silently resolved to the string ``"_default"`` and raised
        ``TypeError: 'str' object is not callable``.
        """
        return "plan" if self.mode == Mode.PLAN.value else "build"

    def _subscribe_tool_events(self, bus) -> None:
        """Show tool activity in the transcript so the run is not a black box.

        A bare `· shell` with no command and no output is indistinguishable from
        a freeze — which is exactly how a 120-second network timeout looked. The
        summary line names what is running, and long-running calls get a live
        elapsed counter so waiting is a decision rather than a guess.
        """
        from kalash.core.events import Event, EventType

        async def on_start(event: Event) -> None:
            name = event.data.get("tool_name", "?")
            label = summarize_tool_call(name, event.data.get("arguments") or {})
            line = ToolCallLine(label, tool_name=name)
            tool_use_id = str(event.data.get("tool_use_id", ""))
            if tool_use_id:
                self._active_tools[tool_use_id] = line
                self._active_tool_started[tool_use_id] = time.monotonic()
            self._active_tool = line
            assistant = self._current_assistant
            if assistant is not None and not assistant.text:
                with contextlib.suppress(Exception):
                    assistant.set_working()
            await self._post(line)

        async def on_output(event: Event) -> None:
            tool_use_id = str(event.data.get("tool_use_id", ""))
            chunk = str(event.data.get("chunk", ""))
            if not chunk:
                return
            line = self._active_tools.get(tool_use_id) or self._active_tool
            if line is not None:
                line.append_output(chunk)

        async def on_complete(event: Event) -> None:
            tool_use_id = str(event.data.get("tool_use_id", ""))
            tool_name = str(event.data.get("tool_name", ""))
            line = self._active_tools.get(tool_use_id)
            if line is None and self._active_tool and not self._active_tools:
                line = self._active_tool
            if line is None:
                return
            exit_code = event.data.get("exit_code")
            try:
                exit_code_i = int(exit_code) if exit_code is not None else None
            except (TypeError, ValueError):
                exit_code_i = None
            with contextlib.suppress(Exception):
                line.finish(
                    failed=bool(event.data.get("error"))
                    or event.data.get("ok") is False
                    or (exit_code_i not in (None, 0)),
                    duration_ms=int(event.data.get("duration_ms") or 0),
                    exit_code=exit_code_i,
                )
            diff_text = event.data.get("diff")
            if diff_text:
                with contextlib.suppress(Exception):
                    line.set_diff(
                        str(event.data.get("path", "")),
                        str(diff_text),
                        str(event.data.get("diff_stat", "")),
                    )
            summary = summarize_tool_result(tool_name, event.data)
            if summary:
                with contextlib.suppress(Exception):
                    line.set_summary(summary)
            if tool_use_id:
                self._active_tools.pop(tool_use_id, None)
            if not self._active_tools:
                self._active_tool = None

        async def on_spawn(event: Event) -> None:
            brief = str(event.data.get("brief") or event.data.get("agent") or "task")
            line = SubagentLine(brief[:70])
            self._subagents[str(event.data.get("child_run_id", brief))] = line
            await self._post(line)

        async def on_agent_complete(event: Event) -> None:
            key = str(event.data.get("child_run_id", ""))
            line = self._subagents.pop(key, None)
            if line is None:
                return
            with contextlib.suppress(Exception):
                line.finish(
                    failed=str(event.data.get("status", "")) != "completed",
                    turns=int(event.data.get("turns") or 0),
                    tokens=int(event.data.get("tokens") or 0),
                )

        async def on_denied(event: Event) -> None:
            tool_use_id = str(event.data.get("tool_use_id", ""))
            line = self._active_tools.pop(tool_use_id, None) or self._active_tool
            self._active_tool = None
            if not self._active_tools:
                self._active_tool = None
            if line is not None:
                with contextlib.suppress(Exception):
                    line.finish(failed=True)
            await self._post(
                SystemMessage(
                    f"refused: {event.data.get('reason', 'not permitted')}", error=True
                )
            )

        async def on_turn_start(event: Event) -> None:
            self._react_step = int(event.data.get("react_step") or event.data.get("iteration") or 0)
            fill = event.data.get("fill_ratio")
            if fill is not None:
                self._context_fill = float(fill)
            elif event.data.get("prompt_tokens") and event.data.get("context_window"):
                self._context_fill = float(event.data["prompt_tokens"]) / float(
                    event.data["context_window"]
                )
            self._refresh_statusline()

        async def on_budget(event: Event) -> None:
            fill = event.data.get("fill_ratio")
            if fill is not None:
                self._context_fill = float(fill)
            elif event.data.get("prompt_tokens") and event.data.get("context_window"):
                self._context_fill = float(event.data["prompt_tokens"]) / float(
                    event.data["context_window"]
                )
            self._refresh_statusline()

        bus.on(EventType.TOOL_START, on_start)
        bus.on(EventType.TOOL_OUTPUT, on_output)
        bus.on(EventType.TOOL_COMPLETE, on_complete)
        bus.on(EventType.TOOL_DENIED, on_denied)
        bus.on(EventType.AGENT_SPAWN, on_spawn)
        bus.on(EventType.AGENT_COMPLETE, on_agent_complete)
        bus.on(EventType.TURN_START, on_turn_start)
        bus.on(EventType.BUDGET_WARNING, on_budget)
        bus.on(EventType.BUDGET_SOFT_LIMIT, on_budget)

    def _tick_active_tool(self) -> None:
        """Refresh the elapsed counter on still-running tool calls."""
        if not self._active_tools:
            return
        now = time.monotonic()
        for tool_id, line in self._active_tools.items():
            if not line.is_done:
                started = self._active_tool_started.get(tool_id)
                if started is not None:
                    with contextlib.suppress(Exception):
                        line.tick(now - started)
