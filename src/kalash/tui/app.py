"""Interactive terminal UI.

Rules that keep it from crashing mid-run (each one was a real bug):

* Widgets are held by reference, never queried through the active screen (an approval dialog
  replaces it).
* Never reuse a Textual attribute name; ``tests/tui`` checks this against a bare ``App``.
* Commands that open modals run as tasks. Awaiting a modal inside a message handler would block
  that widget's message pump, so the modal could never receive a key.
* Streaming text goes through Textual's ``MarkdownStream``; the agent turn is an ordinary task
  that cancellation can always reach.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import subprocess
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Static, TextArea

from kalash import __version__, commands, gitinfo, history
from kalash.agent import context as ctxmod
from kalash.agent import effort as effort_mod
from kalash.agent.build import Runtime, create_runtime
from kalash.agent.context_blocks import instruction_files
from kalash.agent.events import (
    AgentEvent,
    Compacted,
    ContextUpdate,
    ModeChanged,
    Notice,
    ReasoningChunk,
    Retry,
    TextChunk,
    ToolDone,
    ToolOutput,
    ToolPending,
    ToolRunning,
    TurnEnd,
    TurnResult,
    TurnStart,
)
from kalash.config import ConfigError, Settings, settings_path, update_setting
from kalash.credentials import CredentialError, save_credential, set_selected_model
from kalash.llm.http import Adapter
from kalash.llm.providers import (
    ProviderError,
    ProviderSpec,
    all_specs,
    catalog_for,
    has_credential,
    model_problem,
    parse_model,
)
from kalash.modes import MODES, next_mode, resolve_mode
from kalash.paths import project_root
from kalash.permissions import Answer, ApprovalRequest
from kalash.session.log import SessionError, SessionLog, find_session, list_sessions
from kalash.skills import Skill, discover_skills
from kalash.tui import theme as themes
from kalash.tui.describe import mcp_server_of
from kalash.tui.screens import (
    ApprovalScreen,
    ConfirmScreen,
    HelpScreen,
    KeyScreen,
    PickerScreen,
    PlanScreen,
    TextScreen,
)
from kalash.tui.slash import SlashCommand, builtin_commands, grouped, match_commands
from kalash.tui.widgets import (
    GROUPABLE,
    ActivityBar,
    AssistantMessage,
    ErrorCard,
    ExploreGroup,
    NoticeRow,
    PromptArea,
    ReasoningRow,
    StatusBar,
    StatusData,
    Suggestion,
    Suggestions,
    ToolBlock,
    ToolData,
    Transcript,
    TurnFooter,
    UserMessage,
    WelcomeBanner,
)

INIT_PROMPT = (
    "Analyze this codebase and create an AGENTS.md at the repository root that tells a coding "
    "agent how to work here: the project layout, the exact build, test and lint commands, code "
    "style conventions, and anything surprising. Read the real files and configs first; do not "
    "guess commands. Keep it under 80 lines. If an AGENTS.md already exists, improve it "
    "instead of replacing it."
)
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache"}


def _tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    return f"{count / 1000:.1f}k" if count >= 1000 else str(count)


def _provider_status(spec: ProviderSpec) -> str:
    """One line for the provider picker: where the key comes from, or what is missing."""
    if not spec.requires_key:
        return f"{spec.name}  no key needed"
    env = next((name for name in spec.env_keys if os.environ.get(name)), None)
    if env:
        return f"{spec.name}  ✓ key from {env}"
    if has_credential(spec):
        return f"{spec.name}  ✓ key stored"
    return f"{spec.name}  needs {spec.env_keys[0] if spec.env_keys else 'a key'}"


def _popup_description(command: SlashCommand, query: str) -> str:
    """The popup's description, noting the alias that matched when the name itself did not."""
    if query and not command.name.startswith(query):
        alias = next((a for a in command.aliases if a.startswith(query)), None)
        if alias is not None:
            return f"{command.description}  (/{alias})"
    return command.description


class KalashApp(App[int]):
    TITLE = "Kalash"
    CSS_PATH = "app.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("escape", "interrupt", "Cancel", show=False),
        Binding("ctrl+c", "ctrl_c", "Cancel", show=False, priority=True),
        Binding("ctrl+d", "quit_app", "Quit", show=False, priority=True),
        Binding("shift+tab", "cycle_mode", "Mode", show=False, priority=True),
        Binding("ctrl+t", "cycle_effort", "Effort", show=False, priority=True),
        Binding("ctrl+o", "toggle_verbose", "Details", show=False, priority=True),
        Binding("pageup", "page_up", show=False, priority=True),
        Binding("pagedown", "page_down", show=False, priority=True),
        Binding("ctrl+end", "follow", show=False, priority=True),
        Binding("f1", "help", "Help", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        cwd: Path,
        settings: Settings,
        model: str | None = None,
        session_ref: str | None = None,
        continue_latest: bool = False,
        adapter: Adapter | None = None,
    ) -> None:
        super().__init__()
        self.cwd = cwd
        self.settings = settings
        self.model_arg = model
        self.session_ref = session_ref
        self.continue_latest = continue_latest
        self._adapter = adapter
        self.runtime: Runtime | None = None
        self._turn: asyncio.Task[Any] | None = None
        self._command_task: asyncio.Task[Any] | None = None
        self._turn_active = False
        self._interrupted = False
        self._assistant: AssistantMessage | None = None
        self._reasoning: ReasoningRow | None = None
        self._tools: dict[str, ToolBlock] = {}
        self._queue: list[tuple[str, UserMessage]] = []
        self._bg: set[asyncio.Task[Any]] = set()
        self._verbose = bool(settings.tui.get("verbose", False))
        self._files: list[str] | None = None
        self._history = history.load()
        self._custom: dict[str, commands.CustomCommand] = {}
        self._skills: dict[str, Skill] = {}
        self._commands: list[SlashCommand] = []
        self._last_ctrl_c = 0.0
        self._ctx_used = 0
        self._ctx_window = 0
        self._git: gitinfo.GitStatus | None = None
        self._welcome: WelcomeBanner | None = None
        self._stick_value = True  # keep the newest output in view until the user scrolls away
        self._follow_revision = 0  # newer input invalidates deferred follow decisions
        self._turn_edits: dict[str, tuple[int, int]] = {}  # path -> (+added, -removed) this turn

    # -- layout ------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Transcript(on_user_scroll=self._transcript_scrolled, widget_id="transcript")
        yield Static("", id="activity")
        yield Suggestions(id="suggestions")
        yield PromptArea(id="prompt")
        yield Static("", id="status")

    async def on_mount(self) -> None:
        themes.register(self)
        self.theme = themes.resolve(
            self.settings.tui.get("theme", themes.DEFAULT_THEME), self.available_themes
        )
        self.palette = themes.palette_for(self)
        self._transcript = self.query_one("#transcript", VerticalScroll)
        self._prompt = self.query_one("#prompt", PromptArea)
        self._suggest = self.query_one("#suggestions", Suggestions)
        self._prompt.suggestions = self._suggest
        self._prompt.prompt_history = self._history
        old_activity = self.query_one("#activity", Static)
        old_status = self.query_one("#status", Static)
        self._activity = ActivityBar(self.palette)
        self._activity.id = "activity"
        self._status = StatusBar(self.palette)
        self._status.id = "status"
        await old_activity.remove()
        await old_status.remove()
        await self.mount(self._activity, before=self._suggest)
        await self.mount(self._status)
        self._prompt.focus()
        self.watch(self, "theme", self._on_theme_changed)
        self.set_interval(0.1, self._tick)
        self.set_interval(5.0, self._poll_git)
        self._commands = builtin_commands()
        await self._start_runtime(self.model_arg, self.session_ref, self.continue_latest)
        self._poll_git()

    def _on_theme_changed(self, _name: str) -> None:
        self.palette = themes.palette_for(self)
        for widget in (self._activity, self._status):
            widget.palette = self.palette
        for block in self.query(ToolBlock):
            block.set_palette(self.palette)
        for group in self.query(ExploreGroup):
            group.set_palette(self.palette)
        if self._welcome is not None:
            self._welcome.palette = self.palette
            self._welcome.refresh_view()
        self._refresh_status()

    # -- runtime lifecycle -------------------------------------------------------------

    async def _start_runtime(
        self,
        model: str | None,
        session_ref: str | None,
        continue_latest: bool,
        session: SessionLog | None = None,
    ) -> bool:
        root = project_root(self.cwd)
        self._custom = commands.discover(root)
        self._skills = discover_skills(root)
        try:
            if session is None and session_ref:
                session = SessionLog.open(find_session(root, session_ref))
            elif session is None and continue_latest:
                found = list_sessions(root)
                if found:
                    session = SessionLog.open(found[0].path)
            self.runtime = await create_runtime(
                cwd=self.cwd,
                settings=self.settings,
                model=model,
                session=session,
                approver=self._approve,
                emit=self._on_event,
                adapter=self._adapter,
            )
        except (ProviderError, ConfigError, SessionError, CredentialError) as error:
            self.runtime = None
            self._show_welcome(onboarding=str(error))
            self._refresh_status()
            return False
        self.runtime.agent.plan_decider = self._plan_decision
        self._transcript.remove_children()
        self._tools.clear()
        self._welcome = None
        self._show_welcome()
        self._render_history(self.runtime.session)
        agent = self.runtime.agent  # the meter should be right before the first request, too
        self._ctx_window = agent.window
        self._ctx_used = ctxmod.context_tokens(
            agent.session, agent.system_prompt, agent.tool_specs()
        )
        self.sub_title = f"{self.runtime.ref}"
        self._refresh_status()
        return True

    def _show_welcome(self, onboarding: str | None = None) -> None:
        if self._welcome is not None and self._welcome.is_attached:
            self._welcome.remove()  # re-shown after a key or model change: never stack banners
        palette = self.palette
        lines: list[tuple[str, str]] = []
        if self.runtime is None:
            specs = all_specs(self.settings.providers)
            ready = [s.name for s in specs.values() if s.requires_key and has_credential(s)]
            fresh = onboarding is None or onboarding.startswith("no model selected")
            if ready:
                lines = [
                    ("status", "Pick a model to start." if fresh else str(onboarding)),
                    ("keys", ", ".join(ready)),
                    ("next", "/model  choose a model\n/provider  add another provider's key"),
                ]
            else:
                lines = [
                    ("status", "Add an API key to start." if fresh else str(onboarding)),
                    (
                        "next",
                        "/provider  store a key for a provider\n"
                        "or export ANTHROPIC_API_KEY, OPENAI_API_KEY or GEMINI_API_KEY\n"
                        "then /model to choose a model",
                    ),
                ]
            lines.append(("help", "/help  keys and commands"))
        else:
            rt = self.runtime
            mode = MODES[rt.agent.mode]
            effort = rt.agent.effort_display
            files = instruction_files(project_root(self.cwd), self.cwd)
            git = self._git
            where = gitinfo.shorten_path(self.cwd) + (f"  ({git.branch})" if git else "")
            tools = len(rt.agent.registry)
            extras = [f"{tools} tools"]
            if rt.mcp and rt.mcp.clients:
                extras.append(f"{len(rt.mcp.clients)} MCP")
            if self._custom:
                extras.append(f"{len(self._custom)} custom commands")
            lines = [
                ("model", f"{rt.ref}" + (f"  ·  {effort}" if effort else "")),
                ("mode", f"{mode.label} - {mode.description}"),
                ("folder", where),
                (
                    "context",
                    ("AGENTS.md loaded" if files else "no AGENTS.md (try /init)")
                    + "  ·  "
                    + "  ·  ".join(extras),
                ),
                ("", ""),
                ("tips", "/ commands · @ file · !cmd shell · shift+tab mode · ctrl+o details"),
            ]
        self._welcome = WelcomeBanner(lines, __version__, palette)
        self._transcript.mount(self._welcome)

    def _render_history(self, session: SessionLog) -> None:
        messages = session.messages()
        if not messages:
            return
        for message in messages:
            if message.role == "user":
                text = message.text
                if text.startswith("This is an automatically generated checkpoint"):
                    self._mount(NoticeRow("earlier conversation summarised"))
                elif message.visible_text.strip():  # harness-only nudges are not the user's words
                    self._mount(UserMessage(message.visible_text.strip()[:4000]))
            elif message.role == "assistant":
                if message.text.strip():
                    widget = AssistantMessage()
                    widget.append(message.text)
                    self._mount(widget)
                    self._spawn(widget.finish())
                for call in message.tool_calls:
                    try:
                        args = json.loads(call.arguments or "{}")
                    except ValueError:
                        args = {}
                    data = ToolData(call.id, call.name, args if isinstance(args, dict) else {})
                    data.state = "ok"
                    block = ToolBlock(data, self.palette, self.cwd)
                    self._tools[call.id] = block
                    self._mount(block)
            elif message.role == "tool" and message.tool_call_id in self._tools:
                block = self._tools[message.tool_call_id]
                block.data.output = message.text
                block.data.state = "error" if message.is_error else "ok"
                block.refresh_view()
                self._fold_exploration(block)
        self._note(f"Resumed {session.id} · {len(messages)} messages")

    # -- helpers -----------------------------------------------------------------------

    @property
    def _stick(self) -> bool:
        return self._stick_value

    @_stick.setter
    def _stick(self, value: bool) -> None:
        self._follow_revision += 1
        self._stick_value = value
        activity = getattr(self, "_activity", None)
        if value and activity is not None and activity.behind:
            activity.behind = False
            activity.refresh_view(time.monotonic())

    def _mark_behind(self) -> None:
        """Output arrived below the part of the transcript the user scrolled to."""
        if not self._stick and not self._activity.behind:
            self._activity.behind = True
            self._activity.refresh_view(time.monotonic())

    def _fold_exploration(self, block: ToolBlock) -> None:
        """Fold a finished read/glob/grep into the run of them just above it (one summary row)."""
        if block.data.name not in GROUPABLE or block.data.state != "ok" or block.group:
            return
        children = list(self._transcript.children)
        if block not in children:
            return
        i = children.index(block)
        j = i - 1
        while j >= 0:  # hidden members of a group sit between its header and the new block
            member = children[j]
            if not (isinstance(member, ToolBlock) and member.group is not None):
                break
            j -= 1
        header = children[j] if j >= 0 else None
        if isinstance(header, ExploreGroup) and all(
            isinstance(c, ToolBlock) and c.group is header for c in children[j + 1 : i]
        ):
            header.add(block)
            return
        previous = children[i - 1] if i else None
        if (
            isinstance(previous, ToolBlock)
            and previous.group is None
            and previous.data.name in GROUPABLE
            and previous.data.state == "ok"
        ):
            group = ExploreGroup(self.palette, unfolded=self._verbose)
            self._transcript.mount(group, before=previous)
            group.add(previous)
            group.add(block)

    def _spawn(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def _mount(self, widget: Static | AssistantMessage | ToolBlock) -> None:
        self._transcript.mount(widget)
        if self._stick:
            self._transcript.call_after_refresh(self._follow_output)
        else:
            self._mark_behind()

    def _follow_output(self) -> None:
        # A wheel gesture can arrive between mounting output and the refresh.
        # Recheck follow state here; scroll_end's default would defer a second,
        # unguarded scroll beyond this check.
        if self._stick:
            self._transcript.scroll_end(animate=False, immediate=True)

    def _note(self, text: str, level: str = "info") -> None:
        self._mount(NoticeRow(text, level))

    def toast(self, message: str, severity: str = "information") -> None:
        self.notify(message, severity=severity, timeout=3)  # type: ignore[arg-type]

    def _tick(self) -> None:
        if self._stick and not self._at_bottom():
            self._follow_output()  # streamed text grows between mounts
        now = time.monotonic()
        self._activity.tick(now)
        for block in self._tools.values():
            if block.data.state == "running":
                block.data.elapsed = now - block.data.started
                block.tick()

    def _poll_git(self) -> None:
        self._spawn(self._refresh_git())

    async def _refresh_git(self) -> None:
        self._git = await gitinfo.status(self.cwd)
        self._refresh_status()

    def _status_data(self) -> StatusData:
        rt = self.runtime
        data = StatusData(verbose=self._verbose, cwd=gitinfo.shorten_path(self.cwd))
        if rt is not None:
            mode = MODES[rt.agent.mode]
            data.mode_id, data.mode_label = mode.id, mode.label
            data.model = str(rt.ref)
            data.effort = rt.agent.effort_display
            data.used, data.window = self._ctx_used, self._ctx_window or rt.agent.window
            data.cost = rt.session.cost_total()
            data.tokens = rt.session.usage_total().total
            todos = rt.session.todos()
            data.todos = (sum(1 for t in todos if t.get("status") == "completed"), len(todos))
        if self._git is not None:
            data.branch, data.dirty = self._git.branch, self._git.dirty
        return data

    def _refresh_status(self) -> None:
        data = self._status_data()
        self._status.set_data(data)
        for name in ("ask", "auto", "plan", "read-only", "full"):
            self.screen.set_class(data.mode_id == name and self.runtime is not None, f"mode-{name}")

    # -- agent events ------------------------------------------------------------------

    def _on_event(self, event: AgentEvent) -> None:
        if isinstance(event, TextChunk):
            self._end_reasoning()
            if self._assistant is None:
                self._assistant = AssistantMessage()
                self._mount(self._assistant)
            self._assistant.append(event.text)
            self._activity.verb = "Responding"
            self._activity.tokens += max(1, len(event.text) // 4)
        elif isinstance(event, ReasoningChunk):
            if self._reasoning is None:
                self._reasoning = ReasoningRow()
                self._reasoning.set_verbose(self._verbose)
                self._mount(self._reasoning)
            self._reasoning.add(event.text)
            self._activity.verb = "Thinking"
            self._activity.tokens += max(1, len(event.text) // 4)
        elif isinstance(event, ToolPending):
            self._end_reasoning()
            self._end_assistant()
            self._tool_block(event.call_id, event.name)
        elif isinstance(event, ToolRunning):
            self._end_assistant()
            block = self._tool_block(event.call_id, event.name)
            block.data.args, block.data.summary = event.arguments, event.summary
            block.data.state, block.data.started = "running", time.monotonic()
            block.refresh_view()
            self._activity.verb = f"Running {block.data.name}"
        elif isinstance(event, ToolOutput):
            existing = self._tools.get(event.call_id)
            if existing is not None:
                existing.data.output = (existing.data.output + event.text)[-6000:]
                existing.refresh_view()
        elif isinstance(event, ToolDone):
            self._end_assistant()
            block = self._tool_block(event.call_id, event.name)
            block.data.state = "denied" if event.denied else ("error" if event.is_error else "ok")
            block.data.output, block.data.diff = event.text, event.diff
            block.refresh_view()
            self._fold_exploration(block)
            if event.diff:
                added, removed = self._turn_edits.get(str(event.diff["path"]), (0, 0))
                self._turn_edits[str(event.diff["path"])] = (
                    added + int(event.diff["added"]),
                    removed + int(event.diff["removed"]),
                )
            self._activity.verb = "Working"
            if event.name == "todo_write":
                self._refresh_status()
        elif isinstance(event, Notice):
            self._note(event.text, event.level)
        elif isinstance(event, Retry):
            self._activity.note = f"retry {event.attempt} in {event.delay:.0f}s"
            self._note(
                f"Retrying in {event.delay:.1f}s (attempt {event.attempt}): {event.error}",
                "warning",
            )
        elif isinstance(event, Compacted):
            self._activity.verb = "Working"
            self._note(
                f"Context compacted: ~{event.tokens_before:,} → ~{event.tokens_after:,} tokens"
            )
        elif isinstance(event, ContextUpdate):
            self._ctx_used, self._ctx_window = event.used, event.window
            self._activity.note = ""
            self._refresh_status()
        elif isinstance(event, ModeChanged):
            self._refresh_status()
        elif isinstance(event, TurnStart):
            self._activity.verb = "Working"
        elif isinstance(event, TurnEnd):
            self._end_reasoning()
            self._end_assistant()
            result = event.result
            footer = self._turn_footer(result)
            if footer:
                self._mount(TurnFooter(footer))
            if result.reason in ("error", "limit", "refused", "max_tokens", "stuck"):
                self._mount(ErrorCard.for_result(result.reason, result.error, result.error_code))
            elif result.reason == "cancelled":
                self._note(
                    "Interrupted. The conversation is intact; send a new message.", "warning"
                )
            for denial in result.denials:
                server = mcp_server_of(denial)
                hint = (
                    f"  → /mcp trust {server} lets its tools run without asking" if server else ""
                )
                self._note(f"Denied: {denial.split('. Do not retry')[0]}{hint}", "warning")

    def _turn_footer(self, result: TurnResult) -> str | None:
        """One dim line summarising a completed turn (time, calls, tokens, files changed)."""
        if result.reason != "completed":
            return None
        elapsed = time.monotonic() - self._activity.started if self._activity.started else 0.0
        if elapsed < 5 and not result.tool_calls and not self._turn_edits:
            return None
        parts = [f"{elapsed:.0f}s" if elapsed >= 1 else "<1s"]
        if result.tool_calls:
            parts.append(f"{result.tool_calls} tool call{'s' if result.tool_calls != 1 else ''}")
        used_in, used_out = result.usage.input_tokens, result.usage.output_tokens
        if used_in or used_out:
            parts.append(f"{_tokens(used_in or 0)} in · {_tokens(used_out or 0)} out")
        if result.cost is not None:
            parts.append(f"${result.cost:.3f}")
        if self._turn_edits:
            plus = sum(a for a, _ in self._turn_edits.values())
            minus = sum(r for _, r in self._turn_edits.values())
            files = len(self._turn_edits)
            parts.append(f"edited {files} file{'s' if files != 1 else ''} +{plus} −{minus}")
        return "✓ " + " · ".join(parts)

    def _tool_block(self, call_id: str, name: str) -> ToolBlock:
        block = self._tools.get(call_id)
        if block is None:
            block = ToolBlock(ToolData(call_id, name), self.palette, self.cwd)
            block.set_verbose(self._verbose)
            self._tools[call_id] = block
            self._mount(block)
        return block

    def _end_assistant(self) -> None:
        if self._assistant is not None:
            self._spawn(self._assistant.finish())
            self._assistant = None

    def _end_reasoning(self) -> None:
        if self._reasoning is not None:
            self._reasoning.finish()
            self._reasoning = None

    # -- modals ------------------------------------------------------------------------

    async def _modal(self, screen: Any) -> Any:
        """Show a modal and return its result (usable from any task, not only workers)."""
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        def done(result: Any) -> None:
            if not future.done():
                future.set_result(result)

        self.push_screen(screen, callback=done)
        return await future

    async def _approve(self, request: ApprovalRequest) -> Answer:
        if self.settings.tui.get("bell", False):
            self.bell()
        screen = ApprovalScreen(request, self.palette)
        future: asyncio.Future[Answer] = asyncio.get_running_loop().create_future()

        def done(answer: Answer | None) -> None:
            if not future.done():
                future.set_result(answer or "deny")

        self.push_screen(screen, callback=done)
        try:
            return await future
        finally:
            if not future.done() or screen.is_attached:
                with contextlib.suppress(Exception):
                    screen.dismiss("deny")

    async def _plan_decision(self, plan: str) -> tuple[str, str]:
        verdict, feedback = await self._modal(PlanScreen(plan))
        return str(verdict), str(feedback)

    # -- input -------------------------------------------------------------------------

    async def on_prompt_area_submitted(self, message: PromptArea.Submitted) -> None:
        text = message.text.strip()
        if not text:
            return
        plain_message = not text.startswith(("/", "!"))
        if plain_message and self.runtime is None:  # keep what the user typed; nothing was sent
            self._note("No model is configured. Use /provider and /model first.", "warning")
            return
        self._history = history.add(self._history, text)
        self._prompt.prompt_history = self._history
        self._prompt.reset()
        self._suggest.hide()
        if text.startswith("/"):
            self._command_task = asyncio.create_task(self._run_command(text))
        elif text.startswith("!"):
            self._command_task = asyncio.create_task(self._run_user_shell(text[1:].strip()))
        else:
            await self._send(text)

    async def _send(self, text: str, shown: str | None = None) -> None:
        if self.runtime is None:
            self._note("No model is configured. Use /provider and /model first.", "warning")
            return
        if self._turn_active:
            widget = UserMessage(shown or text, queued=True)
            self._mount(widget)
            self._queue.append((text, widget))
            self._activity.queued = len(self._queue)
            self.toast("Queued - it will be sent when the current turn ends")
            return
        self._begin_turn(text, UserMessage(shown or text))

    def _begin_turn(self, text: str, widget: UserMessage) -> None:
        assert self.runtime is not None
        if widget.parent is None:
            self._mount(widget)
        widget.mark_sent()
        if self._welcome is not None:
            self._welcome = None
        self._stick = True
        self._turn_edits = {}
        self._turn_active = True
        self._activity.begin(time.monotonic())
        self._refresh_status()
        self._turn = asyncio.create_task(self._run_turn(text))

    async def _run_turn(self, text: str) -> None:
        assert self.runtime is not None
        try:
            await self.runtime.agent.run_turn(text)
        except asyncio.CancelledError:
            pass
        except Exception as error:
            self._note(f"Unexpected failure: {type(error).__name__}: {error}", "error")
        finally:
            self._turn_active = False
            self._turn = None
            self._activity.end()
            if self.settings.tui.get("bell", False):
                self.bell()
            self._refresh_status()
            self._poll_git()
            interrupted, self._interrupted = self._interrupted, False
            if self._queue and interrupted:
                self._restore_queue()  # never send queued messages right after an Esc
            elif self._queue and self.runtime is not None:
                next_text, widget = self._queue.pop(0)
                self._activity.queued = len(self._queue)
                self._begin_turn(next_text, widget)

    async def _run_user_shell(self, command: str) -> None:
        if self.runtime is None or not command:
            return
        if self._turn_active:
            self._note("Wait for the running turn to finish before using !commands.", "warning")
            return
        self._mount(UserMessage(f"!{command}"))
        try:
            await self.runtime.agent.run_shell(command)
        except Exception as error:
            self._note(f"Command failed: {type(error).__name__}: {error}", "error")
        self._poll_git()

    # -- suggestions (slash commands and @files) --------------------------------------------

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area is not self._prompt:
            return
        self._update_suggestions()

    def _update_suggestions(self) -> None:
        row, col = self._prompt.cursor_location
        line = self._prompt.document.get_line(row)[:col]
        if row == 0 and line.startswith("/") and " " not in line:
            query = line[1:].lower()
            found = match_commands(query, self._all_commands())
            current = self.runtime.agent.mode if self.runtime is not None else None
            self._suggest.show_items(
                [
                    Suggestion(
                        f"/{c.name}",
                        f"/{c.name}",
                        c.args,
                        _popup_description(c, query),
                        "current" if c.mode and c.mode == current else "",
                        None if query else c.section,
                    )
                    for c in found
                ],
                f"{len(found)} commands · ↑↓ move · Tab complete · Enter run · Esc close",
            )
            return
        token = line.rsplit(None, 1)[-1] if line.strip() else ""
        if token.startswith("@") and not line.endswith(" "):
            paths = self._complete_files(token[1:])
            self._suggest.show_items(
                [Suggestion(f"@{p}", p) for p in paths],
                "↑↓ move · Tab or Enter insert · Esc close",
            )
            if self._files is None:
                self._spawn(self._load_files())
            return
        self._suggest.hide()

    def _all_commands(self) -> list[SlashCommand]:
        """Built-ins, then the project's own commands, then skills (a built-in name wins)."""
        taken = {c.name for c in self._commands} | {a for c in self._commands for a in c.aliases}
        custom = [
            SlashCommand(c.name, c.description or "custom command", "[args]", (), False,
                         "Custom commands")
            for c in self._custom.values()
            if c.name not in taken
        ]  # fmt: skip
        taken |= {c.name for c in custom}
        skills = [
            SlashCommand(
                s.name, " ".join(s.description.split())[:90], "[task]", (), False, "Skills"
            )
            for s in self._skills.values()
            if s.name not in taken
        ]
        return [*self._commands, *custom, *skills]

    async def _load_files(self) -> None:
        files = await gitinfo.tracked_files(self.cwd)
        if not files:
            files = []
            for dirpath, dirnames, filenames in os.walk(self.cwd):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
                for name in filenames:
                    files.append(os.path.relpath(os.path.join(dirpath, name), self.cwd))
                if len(files) > 5000:
                    break
        self._files = files
        self._update_suggestions()

    def _complete_files(self, query: str) -> list[str]:
        files = self._files or []
        needle = query.lower()
        if not needle:
            return files[:8]
        scored: list[tuple[int, int, str]] = []
        for path in files:
            low = path.lower()
            base = low.rsplit("/", 1)[-1]
            if base.startswith(needle):
                score = 0
            elif needle in base:
                score = 1
            elif needle in low:
                score = 2
            else:
                continue
            scored.append((score, len(path), path))
        return [p for _, _, p in sorted(scored)[:8]]

    async def on_prompt_area_accept(self, message: PromptArea.Accept) -> None:
        value = self._suggest.selected_value()
        if value is None:
            return
        row, col = self._prompt.cursor_location
        line = self._prompt.document.get_line(row)
        if value.startswith("/"):
            alone = "\n" not in self._prompt.text and not line[col:].strip()
            if message.submit and alone:  # Enter on a command runs it; Tab only completes it
                await self.on_prompt_area_submitted(PromptArea.Submitted(value))
                return
            # replace just the command word - never the rest of a multi-line draft
            self._prompt.replace(value + " ", (0, 0), (0, col))
        else:
            start = line[:col].rfind("@")
            self._prompt.replace(value + " ", (row, start), (row, col))
        self._suggest.hide()

    # -- commands ----------------------------------------------------------------------

    async def _run_command(self, line: str) -> None:
        try:
            await self._command(line)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._note(f"Command failed: {type(error).__name__}: {error}", "error")

    async def _command(self, line: str) -> None:
        name, _, arg = line[1:].partition(" ")
        name, arg = name.lower(), arg.strip()
        command = next((c for c in self._commands if name == c.name or name in c.aliases), None)
        if command is None:
            custom = self._custom.get(name)
            if custom is not None:
                await self._send(custom.expand(arg), shown=line)
                return
            skill = self._skills.get(name)
            if skill is not None:  # /skill-name [task] asks the model to load that skill
                task = f" for this task: {arg}" if arg else "."
                await self._send(f"Use the `{skill.name}` skill{task}", shown=line)
                return
            self._note(f"Unknown command /{name}. Try /help.", "warning")
            return
        if self._turn_active and not command.during_turn:
            self._note(
                f"/{command.name} can't run while the agent is working. Press Esc first.", "warning"
            )
            return
        if command.mode is not None:
            await self._set_mode(command.mode)
            return
        handler = getattr(self, f"_cmd_{command.name.replace('-', '_')}")
        await handler(arg)

    def _help_content(self) -> Group:
        p = self.palette

        def section(title: str, rows: list[tuple[str, str]]) -> list[Text | Table]:
            grid = Table.grid(padding=(0, 3))
            grid.add_column(style=f"bold {p.accent}", no_wrap=True)
            grid.add_column(ratio=1)
            for left, right in rows:
                grid.add_row(left, right)
            return [Text(title, style="bold underline"), Text(""), grid, Text("")]

        keys = [
            ("Enter", "send (queues while the agent works)"),
            ("Ctrl+J / Shift+Enter", "new line"),
            ("Esc", "interrupt the agent, close a popup, or clear the prompt"),
            ("Ctrl+C", "copy the selection, else interrupt, else clear; twice to quit"),
            ("Shift+Tab", "cycle mode: Ask → Auto → Plan"),
            ("Ctrl+T", "cycle reasoning effort (only the levels this model accepts)"),
            ("Ctrl+O", "show or hide tool details and reasoning"),
            ("↑ / ↓", "prompt history"),
            ("PgUp / PgDn / Ctrl+End", "scroll, and jump back to the newest output"),
            ("Tab", "complete a /command or @file"),
            ("!cmd", "run a shell command yourself (same sandbox)"),
            ("Ctrl+D", "quit"),
        ]
        blocks: list[Text | Table] = list(section("Keys", keys))
        for title, group in grouped(self._all_commands()):
            rows = [(f"/{c.name}{(' ' + c.args) if c.args else ''}", c.description) for c in group]
            blocks.extend(section(title, rows))
        return Group(*blocks)

    async def _cmd_help(self, _arg: str) -> None:
        await self._modal(HelpScreen(self._help_content()))

    async def _cmd_quit(self, _arg: str) -> None:
        await self.action_quit_app()

    async def _cmd_new(self, _arg: str) -> None:
        if self.runtime is not None:
            old, self.runtime = self.runtime, None
            await old.aclose()
        self._queue.clear()
        self._ctx_used = 0
        await self._start_runtime(self.model_arg, None, False)

    async def _cmd_model(self, arg: str) -> None:
        specs = all_specs(self.settings.providers)
        if not arg:
            items: list[tuple[str, str, str]] = []
            for spec in specs.values():
                if not has_credential(spec):
                    continue
                for model_id, info in catalog_for(spec).items():
                    ctx = f"{info.context // 1000}k ctx"
                    price = f"${info.price.input:g}/${info.price.output:g}" if info.price else ""
                    items.append(
                        (
                            f"{spec.id}/{model_id}",
                            f"{spec.id}/{model_id}",
                            f"{ctx}  {price}  {info.note}".strip(),
                        )
                    )
            if not items:
                self._note(
                    "No provider has a key yet. Use /provider first, or type /model provider/name.",
                    "warning",
                )
                return
            picked = await self._modal(
                PickerScreen(
                    "Choose a model",
                    items,
                    "Only providers with a key are listed. Type to filter; "
                    "/model provider/name picks any other id.",
                )
            )
            if not picked:
                return
            arg = picked
        await self._switch_model(arg)

    async def _switch_model(self, value: str) -> None:
        try:
            ref = parse_model(value, all_specs(self.settings.providers))
        except ProviderError as error:
            self._note(str(error), "error")
            return
        problem = model_problem(ref.provider, ref.model)
        if problem and problem[0] == "error":
            self._note(problem[1], "error")
            return
        old = self.runtime
        try:
            new = await create_runtime(
                cwd=self.cwd,
                settings=self.settings,
                model=str(ref),
                session=old.session if old else None,
                approver=self._approve,
                emit=self._on_event,
            )
        except (ProviderError, ConfigError, CredentialError) as error:
            self._note(f"Cannot switch to {ref}: {error}", "error")
            return
        if old is not None:
            mode = old.agent.mode
            await old.aclose(keep_session=True)
            new.agent.set_mode(mode, announce=False)
        new.agent.plan_decider = self._plan_decision
        self.runtime = new
        self.model_arg = str(ref)
        set_selected_model(ref.provider, ref.model)
        self._ctx_window = new.agent.window
        self.toast(f"Model: {ref}")
        self._refresh_status()

    async def _cmd_effort(self, arg: str) -> None:
        if self.runtime is None:
            return
        agent = self.runtime.agent
        options = effort_mod.choices(agent.info)
        if not arg:
            if not options:
                self._note(f"{self.runtime.ref} has no reasoning control.")
                return
            current = agent.effort_setting or effort_mod.DEFAULT
            items = [
                (c.value, c.label, c.detail + ("  ● current" if c.value == current else ""))
                for c in options
            ]
            picked = await self._modal(
                PickerScreen(
                    f"Reasoning effort for {self.runtime.ref}",
                    items,
                    "Only what this model accepts is listed. Ctrl+T cycles through them.",
                )
            )
            if not picked:
                return
            arg = picked
        self._apply_effort(arg)

    def _apply_effort(self, requested: str | None) -> None:
        assert self.runtime is not None
        agent = self.runtime.agent
        setting, message = agent.set_effort(requested)
        if setting is None and message:
            self._note(f"{message}.", "warning")
            return
        shown = agent.effort_display or "model default"
        self.toast(f"Reasoning effort: {shown}" + (f" ({message})" if message else ""))
        self._refresh_status()

    def action_cycle_effort(self) -> None:
        if self.runtime is None:
            return
        agent = self.runtime.agent
        if not effort_mod.has_control(agent.info):
            self.toast(f"{self.runtime.ref} has no reasoning control", "warning")
            return
        self._apply_effort(effort_mod.cycle(agent.info, agent.effort_setting))

    async def _cmd_provider(self, arg: str) -> None:
        specs = all_specs(self.settings.providers)
        provider = arg.strip().lower()
        if not provider:
            items = [(s.id, s.id, _provider_status(s)) for s in specs.values()]
            provider = (
                await self._modal(PickerScreen("Providers - choose one to set its key", items))
                or ""
            )
        if not provider:
            return
        spec = specs.get(provider)
        if spec is None:
            self._note(f"Unknown provider {provider!r}. Known: {', '.join(sorted(specs))}", "error")
            return
        if not spec.requires_key:
            self._note(f"{spec.name} needs no API key ({spec.base_url}).")
            return
        key = await self._modal(KeyScreen(spec.name))
        if not key:
            return
        try:
            save_credential(spec.id, key)
        except (CredentialError, OSError) as error:
            self._note(f"Could not save the key: {error}", "error")
            return
        self._note(f"Saved the key for {spec.name}.")
        self.toast(f"Saved the key for {spec.name}")  # starting the runtime clears the transcript
        if self.runtime is None and not await self._start_runtime(self.model_arg, None, False):
            await self._cmd_model("")

    async def _cmd_resume(self, arg: str) -> None:
        if not arg:
            infos = list_sessions(project_root(self.cwd))
            if not infos:
                self._note("No sessions in this project yet.")
                return
            from datetime import datetime

            items = [
                (
                    i.id,
                    f"{datetime.fromtimestamp(i.updated_at):%m-%d %H:%M}  {i.title or i.id}",
                    i.model,
                )
                for i in infos[:60]
            ]
            picked = await self._modal(PickerScreen("Resume a session", items))
            if not picked:
                return
            arg = picked
        old = self.runtime
        if old is not None and arg == old.session.id:
            return
        try:
            session = SessionLog.open(find_session(project_root(self.cwd), arg))
        except SessionError as error:
            self._note(str(error), "error")
            return
        if old is not None:
            self.runtime = None
            await old.aclose()
        if not await self._start_runtime(self.model_arg, None, False, session=session):
            session.close()

    async def _cmd_compact(self, _arg: str) -> None:
        if self.runtime is None:
            return
        self._turn_active = True
        self._activity.begin(time.monotonic())
        self._activity.verb = "Compacting"
        try:
            await self.runtime.agent.compact_now()
        except ctxmod.NothingToCompact as error:
            self._note(str(error).capitalize() + ".")
        except Exception as error:
            self._note(f"Compaction failed: {error}", "error")
        finally:
            self._turn_active = False
            self._activity.end()
            self._refresh_status()

    async def _cmd_cost(self, _arg: str) -> None:
        if self.runtime is None:
            return
        usage = self.runtime.session.usage_total()
        cost = self.runtime.session.cost_total()
        price = f"${cost:.4f}" if cost is not None else "unknown (no price for this model)"
        self._note(
            f"Tokens: {usage.input_tokens:,} in ({usage.cache_read_tokens:,} cached) · "
            f"{usage.output_tokens:,} out · cost {price}"
        )

    async def _cmd_status(self, _arg: str) -> None:
        if self.runtime is None:
            self._note("No model configured.")
            return
        from kalash.sandbox import detect
        from kalash.skills import discover_skills

        rt, sandbox = self.runtime, detect()
        mode = MODES[rt.agent.mode]
        net = "on" if self.settings.sandbox.network else "off"
        mcp = len(rt.mcp.clients) if rt.mcp else 0
        skills = len(discover_skills(project_root(self.cwd)))
        self._note(
            f"model {rt.ref} · {len(rt.agent.registry)} tools · MCP {mcp} · skills {skills}\n"
            f"mode {mode.label} (sandbox {mode.sandbox}, approval {mode.approval}) · "
            f"{sandbox.backend}: {'ok' if sandbox.ok else sandbox.detail} · network {net}\n"
            f"session {rt.session.path}"
        )

    async def _cmd_context(self, _arg: str) -> None:
        if self.runtime is None:
            return
        agent = self.runtime.agent
        specs = agent.tool_specs()
        fixed = ctxmod.fixed_tokens(agent.system_prompt, specs)
        total = ctxmod.context_tokens(agent.session, agent.system_prompt, specs)
        window = agent.window
        self._note(
            f"context ≈ {total:,} / {window:,} tokens ({100 * total // max(window, 1)}%)\n"
            f"  system prompt + tool definitions ≈ {fixed:,}\n"
            f"  conversation ≈ {max(0, total - fixed):,}   "
            f"(compacts automatically at {int(self.settings.compaction.threshold * 100)}%)"
        )

    async def _cmd_diff(self, _arg: str) -> None:
        summary = await gitinfo.diff_summary(self.cwd)
        agent_files = (
            self.runtime.agent.checkpoints.changed_files(self.runtime.session)
            if self.runtime
            else []
        )
        parts = []
        if summary is not None:
            parts.append(f"git working tree:\n{summary}")
        if agent_files:
            parts.append(
                "changed by the agent this session:\n"
                + "\n".join(f"  {gitinfo.shorten_path(Path(p))}" for p in agent_files)
            )
        self._note("\n\n".join(parts) if parts else "No changes to show.")

    async def _cmd_copy(self, _arg: str) -> None:
        text = ""
        for widget in reversed(list(self.query(AssistantMessage))):
            if widget.text.strip():
                text = widget.text
                break
        if not text:
            self.toast("Nothing to copy yet", "warning")
            return
        self.copy_to_clipboard(text)
        self.toast(f"Copied {len(text):,} characters")

    async def _cmd_export(self, arg: str) -> None:
        if self.runtime is None:
            return
        lines = [f"# Kalash session {self.runtime.session.id}\n", f"Model: {self.runtime.ref}\n"]
        for message in self.runtime.session.messages():
            if message.role == "user":
                lines.append(f"\n## You\n\n{message.visible_text.strip()}\n")
            elif message.role == "assistant":
                if message.text.strip():
                    lines.append(f"\n## Kalash\n\n{message.text}\n")
                for call in message.tool_calls:
                    lines.append(f"\n> tool `{call.name}`: `{call.arguments[:300]}`\n")
        path = Path(arg).expanduser() if arg else self.cwd / f"kalash-{self.runtime.session.id}.md"
        try:
            path.write_text("".join(lines), "utf-8")
        except OSError as error:
            self._note(f"Could not write {path}: {error}", "error")
            return
        self._note(f"Exported conversation to {path}")

    async def _cmd_init(self, _arg: str) -> None:
        await self._send(INIT_PROMPT, shown="/init")

    async def _cmd_memory(self, _arg: str) -> None:
        root = project_root(self.cwd)
        path = root / "AGENTS.md"
        if not path.exists():
            path.write_text("# Project instructions\n\n", "utf-8")
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
        with self.suspend(), contextlib.suppress(OSError):
            subprocess.run([*shlex.split(editor), str(path)], check=False)
        if self.runtime is not None and self.runtime.agent.context is not None:
            self.runtime.agent.context.refresh_instructions()  # re-sent with your next message
        self._note(
            f"Edited {path}. The model gets the updated instructions with your next message."
        )

    async def _cmd_permissions(self, arg: str) -> None:
        if self.runtime is None:
            return
        if not arg:
            items = [(m.id, m.label, m.description) for m in MODES.values()]
            picked = await self._modal(PickerScreen("Permission mode", items))
            if not picked:
                return
            arg = picked
        mode = resolve_mode(arg)
        if mode is None:
            self._note(f"Unknown mode {arg!r}. Modes: {', '.join(MODES)}", "warning")
            return
        await self._set_mode(mode.id)

    async def _set_mode(self, mode_id: str) -> None:
        """Switch mode. Full access (no sandbox, no prompts) needs an explicit confirmation."""
        if self.runtime is None:
            self._note("No model is configured. Use /provider and /model first.", "warning")
            return
        mode = MODES[mode_id]
        if mode_id == "full" and self.runtime.agent.mode != "full":
            sure = await self._modal(
                ConfirmScreen(
                    "Enable full access?",
                    "Commands and file changes will run WITHOUT the sandbox and WITHOUT asking "
                    "you first. The agent can delete or overwrite anything your user can. Use it "
                    "only in a throwaway or fully backed-up environment.",
                    confirm="Enable full access",
                )
            )
            if not sure:
                self.toast("Full access not enabled")
                return
        self.runtime.agent.set_mode(mode.id)
        self.toast(f"{mode.label}: {mode.description}")

    async def _cmd_todos(self, _arg: str) -> None:
        todos = self.runtime.session.todos() if self.runtime else []
        if not todos:
            self._note("No task list yet.")
            return
        marks = {"completed": "☑", "in_progress": "◐", "pending": "☐"}
        self._note("\n".join(f"{marks.get(t['status'], '☐')} {t['content']}" for t in todos))

    async def _cmd_undo(self, _arg: str) -> None:
        if self.runtime is None:
            return
        self._note(self.runtime.agent.undo_last_turn())
        self._poll_git()

    async def _cmd_theme(self, arg: str) -> None:
        if not arg:
            items = [
                (name, name, "current" if name == self.theme else "")
                for name in themes.SELECTABLE
                if name in self.available_themes
            ]
            picked = await self._modal(PickerScreen("Theme", items))
            if not picked:
                return
            arg = picked
        arg = themes.ALIASES.get(arg, arg)
        if arg not in self.available_themes:
            self._note(f"Unknown theme {arg!r}.", "warning")
            return
        self.theme = arg
        with contextlib.suppress(ConfigError, OSError):
            update_setting(settings_path("user", self.cwd), "tui.theme", arg)
        self.toast(f"Theme: {arg}")

    async def _cmd_rename(self, arg: str) -> None:
        if self.runtime is None:
            return
        title = arg or await self._modal(TextScreen("Session name"))
        if not title:
            return
        self.runtime.session.append("title", title=title)
        self.toast(f"Renamed to {title}")

    async def _cmd_mcp(self, arg: str) -> None:
        rt = self.runtime
        word, _, target = arg.partition(" ")
        if word in ("trust", "untrust") and target.strip():
            if rt is None or rt.mcp is None:
                self._note("No MCP servers are connected.", "warning")
                return
            changed = rt.mcp.set_trust(target.strip(), word == "trust")
            if not changed:
                self._note(f"No connected MCP server named {target.strip()!r}.", "warning")
                return
            state = "run without asking" if word == "trust" else "ask first again"
            self._note(
                f"{target.strip()}: its {changed} tools now {state} for this session. "
                f"To keep it: kalash mcp trust {target.strip()}"
                + ("" if word == "trust" else " --off")
            )
            return
        configured = self.settings.mcp_servers
        if rt is None or (not configured and not (rt.mcp and rt.mcp.clients)):
            self._note(
                "No MCP servers configured. Add one:  kalash mcp add deepwiki "
                "https://mcp.deepwiki.com/mcp   (then restart or /new)"
            )
            return
        lines = []
        connected = {c.name: c for c in (rt.mcp.clients if rt.mcp else [])}
        for name, server in configured.items():
            client = connected.get(name)
            if client is None:
                lines.append(f"✗ {name}: not connected ({server.target})")
                continue
            names = [t.remote_name for t in (rt.mcp.tools if rt.mcp else []) if t.client is client]
            era = getattr(client, "era", None)
            how = f"remote, {era} protocol {getattr(client, 'version', '')}" if era else "local"
            trust = "trusted" if server.trust else "asks first"
            shown = ", ".join(names[:6]) + (f" +{len(names) - 6}" if len(names) > 6 else "")
            lines.append(f"✓ {name} ({how}, {trust}): {len(names)} tools - {shown}")
        for warning in rt.mcp.warnings if rt.mcp else []:
            lines.append(f"! {warning}")
        self._note("\n".join(lines))

    async def _cmd_skills(self, _arg: str) -> None:
        from kalash.skills import discover_skills

        skills = discover_skills(project_root(self.cwd))
        if not skills:
            self._note("No skills found (.kalash/skills/<name>/SKILL.md).")
            return
        self._note("\n".join(f"{s.name} - {s.description}" for s in skills.values()))

    async def _cmd_verbose(self, _arg: str) -> None:
        self.action_toggle_verbose()

    async def _cmd_doctor(self, _arg: str) -> None:
        from kalash.sandbox import detect

        status = detect(refresh=True)
        self._note(
            f"sandbox: {status.backend} - {status.detail}"
            + ("" if status.ok else "\n(commands are refused in confined modes)")
        )

    # -- actions -----------------------------------------------------------------------

    def action_interrupt(self) -> None:
        if self._cancel_turn():
            pass
        elif self.focused is self._prompt and self._prompt.text:
            self._prompt.reset()
        elif self._queue:
            self._clear_queue()

    def _cancel_turn(self) -> bool:
        if self._turn is None or self._turn.done():
            return False
        self._interrupted = True
        self._turn.cancel()
        return True

    def _restore_queue(self) -> None:
        """Hand queued messages back to the prompt so an interrupt never sends them."""
        queued = "\n".join(text for text, _ in self._queue)
        for _, widget in self._queue:
            widget.remove()
        self._queue.clear()
        self._activity.queued = 0
        current = self._prompt.text
        self._prompt.set_text(f"{queued}\n{current}" if current else queued)
        self.toast("Queued messages are back in the prompt")

    def _clear_queue(self) -> None:
        for _, widget in self._queue:
            widget.remove()
        self._queue.clear()
        self._activity.queued = 0
        self.toast("Queued messages cleared")

    def action_ctrl_c(self) -> None:
        selected = self.screen.get_selected_text()
        if selected:
            self.copy_to_clipboard(selected)
            self.screen.clear_selection()
            self.toast(f"Copied {len(selected):,} characters")
        elif self._cancel_turn():
            pass
        elif self._prompt.text:
            self._prompt.reset()
        else:
            now = time.monotonic()
            if now - self._last_ctrl_c < 2.0:
                self._spawn(self.action_quit_app())
            else:
                self._last_ctrl_c = now
                self.toast("Press Ctrl+C again to quit")

    def action_cycle_mode(self) -> None:
        if self.runtime is None:
            return
        mode = MODES[next_mode(self.runtime.agent.mode)]
        self.runtime.agent.set_mode(mode.id)
        self.toast(f"{mode.label}: {mode.description}")

    def action_toggle_verbose(self) -> None:
        self._verbose = not self._verbose
        for block in self.query(ToolBlock):
            block.set_verbose(self._verbose)
        for row in self.query(ReasoningRow):
            row.set_verbose(self._verbose)
        for group in self.query(ExploreGroup):
            group.set_open(self._verbose)
        self._refresh_status()
        self.toast("Details shown" if self._verbose else "Details hidden")

    def action_page_up(self) -> None:
        self._stick = False
        self._transcript.scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        self._transcript.scroll_page_down(animate=False)
        self._follow_after_scroll()

    def action_follow(self) -> None:
        self._stick = True
        self._transcript.call_after_refresh(self._follow_output)

    def _at_bottom(self) -> bool:
        return self._transcript.scroll_y >= self._transcript.max_scroll_y - 1

    def _follow_after_scroll(self) -> None:
        self._stick = False
        revision = self._follow_revision

        def resume() -> None:
            # Trackpads can reverse direction many times in one input batch.
            # An older downward gesture must never re-enable follow after Up.
            if revision == self._follow_revision:
                self._stick = self._at_bottom()

        self.call_after_refresh(resume)

    def _transcript_scrolled(self, up: bool) -> None:
        if up:
            self._stick = False
        else:
            self._follow_after_scroll()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self.screen.is_modal or event.ctrl or event.shift:
            return
        # A non-scrolling prompt/footer bubbles its wheel events to the app. The
        # transcript consumes its own wheel events while scrolling.
        self._stick = False
        self._transcript.scroll_relative(
            y=-self.scroll_sensitivity_y, animate=False, immediate=True
        )

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if self.screen.is_modal or event.ctrl or event.shift:
            return
        self._transcript.scroll_relative(y=self.scroll_sensitivity_y, animate=False, immediate=True)
        self._follow_after_scroll()

    def action_help(self) -> None:
        self._command_task = asyncio.create_task(self._run_command("/help"))

    async def _release(self) -> None:
        for task in (self._turn, self._command_task):
            if task is not None and not task.done() and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        others = [t for t in self._bg if t is not asyncio.current_task()]
        if others:  # never wait on ourselves: Ctrl+C twice quits from inside a background task
            await asyncio.gather(*others, return_exceptions=True)
        if self.runtime is not None:
            runtime, self.runtime = self.runtime, None
            await runtime.aclose()

    async def action_quit_app(self) -> None:
        await self._release()
        self.exit(0)

    async def on_unmount(self) -> None:
        await self._release()


def run_tui(
    *,
    cwd: Path,
    settings: Settings,
    model: str | None,
    session_ref: str | None,
    continue_latest: bool,
) -> int:
    app = KalashApp(
        cwd=cwd,
        settings=settings,
        model=model,
        session_ref=session_ref,
        continue_latest=continue_latest,
    )
    return int(app.run() or 0)


__all__ = ["KalashApp", "run_tui"]
