"""Kalash TUI.

Layout is a scrolling transcript with a pinned footer. The footer holds the
selection picker (when open), the prompt, a status line, and key hints. The
welcome banner is the first item in the transcript and is removed once the
conversation starts, so it scrolls away naturally instead of the app swapping
between two competing full-screen views.
"""

from __future__ import annotations

import os
from enum import StrEnum

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.widgets import Input, Static
from textual.worker import Worker, WorkerState

from kalash.tui.messages import (
    AssistantMessage,
    SystemMessage,
    UserMessage,
    WelcomeBanner,
)
from kalash.tui.picker import Picker, PickerItem, PickerMode


SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/connect", "Connect a provider"),
    ("/models", "Switch model"),
    ("/new", "Start a new session"),
    ("/status", "Show session status"),
    ("/help", "List commands"),
    ("/clear", "Clear the transcript"),
    ("/exit", "Exit Kalash"),
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

    CSS = """
    Screen {
        background: $background;
    }

    #transcript {
        height: 1fr;
        width: 100%;
        padding: 0 2;
        scrollbar-size-vertical: 1;
    }

    #footer {
        dock: bottom;
        height: auto;
        width: 100%;
        padding: 0 2 0 2;
    }

    #input-wrap {
        height: 3;
        width: 100%;
        border: round $primary;
    }
    #input-wrap.busy {
        border: round $warning;
    }
    #input-wrap:focus-within {
        border: round $accent;
    }

    #prompt {
        width: 1fr;
        background: transparent;
        border: none;
        padding: 0 1;
    }
    #prompt:focus {
        border: none;
    }

    #statusline {
        height: 1;
        width: 100%;
        padding: 0 1;
        color: $text-muted;
    }

    #hints {
        height: 1;
        width: 100%;
        padding: 0 1;
        color: $text-muted;
    }
    """

    # `tab` and the arrow keys need priority: without it Textual's default
    # focus navigation claims tab, which silently moves focus off the prompt
    # and makes every subsequent keystroke go nowhere.
    BINDINGS = [
        Binding("ctrl+c", "interrupt", "Interrupt", show=False),
        Binding("ctrl+d", "quit", "Quit", show=False),
        Binding("tab", "toggle_mode", "Toggle mode", show=False, priority=True),
        Binding("shift+tab", "toggle_mode", "Toggle mode", show=False, priority=True),
        Binding("escape", "dismiss", "Dismiss", show=False, priority=True),
        Binding("up", "history_or_picker(-1)", "Up", show=False, priority=True),
        Binding("down", "history_or_picker(1)", "Down", show=False, priority=True),
    ]

    mode: reactive[str] = reactive(Mode.BUILD.value)
    provider_id: reactive[str | None] = reactive(None)
    model_id: reactive[str | None] = reactive(None)
    busy: reactive[bool] = reactive(False)

    def __init__(self) -> None:
        super().__init__()
        self._intent = InputIntent.PROMPT
        self._pending_provider: str | None = None
        self._pending_base_url: str | None = None
        self._history: list[str] = []
        self._history_pos: int | None = None
        self._banner_removed = False
        self._stream_worker: Worker[None] | None = None
        self._resolution_error: str | None = None
        self._restore_session()

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
        self._refresh_statusline()
        self._refresh_hints()
        self.query_one("#prompt", Input).focus()

    # -- status line -------------------------------------------------------

    def _refresh_statusline(self) -> None:
        from rich.text import Text

        from kalash.tui.providers import get_provider

        text = Text()
        text.append(self.mode, style="bold green" if self.mode == Mode.BUILD else "bold cyan")

        if self.provider_id and self.model_id:
            provider = get_provider(self.provider_id)
            name = provider.name if provider else self.provider_id
            text.append("  ·  ", style="dim")
            text.append(self.model_id, style="")
            text.append(f"  {name}", style="dim")
        else:
            text.append("  ·  ", style="dim")
            text.append("no provider — run /connect", style="yellow")

        try:
            self.query_one("#statusline", Static).update(text)
        except NoMatches:
            pass

    def _refresh_hints(self) -> None:
        from rich.text import Text

        text = Text()
        pairs = [("tab", "mode"), ("/", "commands"), ("ctrl+c", "stop"), ("ctrl+d", "quit")]
        for index, (key, label) in enumerate(pairs):
            if index:
                text.append("   ")
            text.append(key, style="bold dim")
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

    def action_dismiss(self) -> None:
        picker = self.query_one(Picker)
        if picker.is_open:
            picker.close()
            self._reset_intent()
            return
        if self.busy:
            self.action_interrupt()

    def action_interrupt(self) -> None:
        if self._stream_worker is not None and self._stream_worker.state is WorkerState.RUNNING:
            self._stream_worker.cancel()
            self._say("interrupted")
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
        if picker.mode in (PickerMode.PROVIDER, PickerMode.MODEL):
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
            await self._post(
                SystemMessage(
                    [
                        f"mode      {self.mode}",
                        f"provider  {self.provider_id or '—'}",
                        f"model     {self.model_id or '—'}",
                        f"cwd       {os.getcwd()}",
                    ]
                )
            )
        elif command == "/connect":
            await self._start_connect()
        elif command == "/models":
            await self._start_models()
        else:
            await self._post(SystemMessage(f"unknown command: {command}", error=True))

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
        await self._post(message)
        self.busy = True
        self._stream_worker = self._stream(text, message)

    @work(exclusive=True, group="stream")
    async def _stream(self, text: str, sink: AssistantMessage) -> None:
        from kalash.models.normalize import BlockDelta, MessageStop, StreamError

        try:
            provider = await self._build_provider()
            if provider is None:
                sink.set_error(self._resolution_error or "provider could not be initialised")
                return

            from kalash.models.gateway import ModelGateway
            from kalash.models.normalize import Message, Role, TextBlock

            gateway = ModelGateway(primary=provider)
            history = [Message(role=Role.USER, content=[TextBlock(text=text)])]

            system = (
                "You are Kalash, a terminal coding assistant. Be direct and concise."
            )
            if self.mode == Mode.PLAN.value:
                system += " You are in Plan mode: describe the approach, do not make changes."

            async for event in gateway.stream(history, system=system, max_tokens=4096):
                if isinstance(event, BlockDelta):
                    sink.append(event.delta)
                    self._transcript().scroll_end(animate=False)
                elif isinstance(event, StreamError):
                    sink.set_error(event.error)
                    return
                elif isinstance(event, MessageStop):
                    break

            sink.finish()
        except Exception as exc:  # surfaced in the transcript, never a crash
            sink.set_error(str(exc))
        finally:
            self.busy = False

    async def _build_provider(self):
        """Construct the model provider via the shared resolver.

        Headless mode uses the same function, so a provider connected here
        also works for `kalash -p`.
        """
        from kalash.models.resolve import build_provider

        resolution = build_provider(self.provider_id, self.model_id)
        if not resolution.ok:
            self._resolution_error = resolution.reason
            return None
        return resolution.provider
