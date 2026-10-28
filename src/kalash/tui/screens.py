"""Modal screens: approval, plan review, pickers, key entry and help."""

from __future__ import annotations

from rich.console import RenderableType
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from kalash.permissions import Answer, ApprovalRequest
from kalash.tui.describe import pretty_action
from kalash.tui.theme import Palette
from kalash.tui.widgets import render_diff

ARM_DELAY_S = 0.4


def detail_text(detail: str, palette: Palette) -> Text:
    """Colour a detail block: diffs get gutters and +/- colours, anything else stays plain."""
    if "@@" in detail:
        head, _, rest = detail.partition("\n")
        text = Text(head + "\n", style="bold")
        text.append_text(render_diff(rest, palette, 40))
        return text
    return Text(detail)


class ApprovalScreen(ModalScreen[Answer]):
    """Ask whether one action may proceed. y/s only act after a short delay so keys typed into
    the prompt as the dialog opens can never approve by accident."""

    BINDINGS = [("escape", "answer('deny')", "Deny")]

    def __init__(self, request: ApprovalRequest, palette: Palette) -> None:
        super().__init__()
        self.request = request
        self.palette = palette
        self._armed = False

    def compose(self) -> ComposeResult:
        with Vertical(id="approval", classes="dialog"):
            yield Label(Text("Approval needed", style="bold"), classes="dialog-title warn")
            yield Static(
                Text(pretty_action(self.request.summary), style="bold"), classes="dialog-line"
            )
            yield Static(Text(f"Why: {self.request.reason}", style="dim"), classes="dialog-line")
            if self.request.detail:
                with VerticalScroll(id="approval-detail"):
                    yield Static(detail_text(self.request.detail, self.palette))
            with Horizontal(id="approval-actions"):
                yield Button("Allow once  y", id="once", variant="success", disabled=True)
                yield Button("Allow for session  s", id="session", disabled=True)
                yield Button("Deny  n", id="deny", variant="error")

    def on_mount(self) -> None:
        self.set_timer(ARM_DELAY_S, self._arm)
        self.query_one("#deny", Button).focus()

    def _arm(self) -> None:
        self._armed = True
        for name in ("once", "session"):
            self.query_one(f"#{name}", Button).disabled = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in ("once", "session", "deny"):
            self.action_answer(str(event.button.id))

    def on_key(self, event: events.Key) -> None:
        answer = {"y": "once", "s": "session", "n": "deny"}.get(event.key)
        if answer is not None and (self._armed or answer == "deny"):
            event.stop()
            self.action_answer(answer)

    def action_answer(self, answer: str) -> None:
        if not self._armed and answer != "deny":
            return
        self.dismiss(answer)  # type: ignore[arg-type]


class PlanScreen(ModalScreen[tuple[str, str]]):
    """The model's finished plan: approve, approve with auto mode, or keep planning."""

    BINDINGS = [("escape", "decide('keep')", "Keep planning")]

    def __init__(self, plan: str) -> None:
        super().__init__()
        self.plan = plan

    def compose(self) -> ComposeResult:
        with Vertical(id="plan", classes="dialog"):
            yield Label(Text("Plan ready for review", style="bold"), classes="dialog-title plan")
            with VerticalScroll(id="plan-body"):
                yield Markdown(self.plan)
            yield Input(
                placeholder="Feedback for the model (optional) - Enter to keep planning",
                id="plan-feedback",
            )
            with Horizontal(id="plan-actions"):
                yield Button("Approve  y", id="approve", variant="success")
                yield Button("Approve + auto mode  a", id="auto")
                yield Button("Keep planning  n", id="keep", variant="warning")

    def on_mount(self) -> None:
        self.query_one("#approve", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id:
            self.action_decide(str(event.button.id))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_decide("keep")

    def on_key(self, event: events.Key) -> None:
        if isinstance(self.focused, Input):
            return
        verdict = {"y": "approve", "a": "auto", "n": "keep"}.get(event.key)
        if verdict:
            event.stop()
            self.action_decide(verdict)

    def action_decide(self, verdict: str) -> None:
        feedback = self.query_one("#plan-feedback", Input).value.strip()
        self.dismiss((verdict, feedback))


class PickerScreen(ModalScreen[str | None]):
    """A filterable list. Items are (value, label, description)."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str, items: list[tuple[str, str, str]], hint: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.items = items
        self.hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(id="picker", classes="dialog"):
            yield Label(Text(self.title_text, style="bold"), classes="dialog-title")
            if self.hint:
                yield Static(Text(self.hint, style="dim"), classes="dialog-line")
            yield Input(placeholder="type to filter…", id="picker-filter")
            yield OptionList(id="picker-list")

    def on_mount(self) -> None:
        self._fill("")
        self.query_one("#picker-filter", Input).focus()

    def _fill(self, query: str) -> None:
        options = self.query_one("#picker-list", OptionList)
        options.clear_options()
        needle = query.lower().strip()
        shown = [
            (v, label, desc)
            for v, label, desc in self.items
            if not needle or needle in f"{label} {desc}".lower()
        ]
        width = max((len(label) for _, label, _ in shown), default=0)
        options.add_options(
            Option(Text.assemble((label.ljust(width + 2), "bold"), (desc, "dim")), id=v)
            for v, label, desc in shown[:300]
        )
        if shown:
            options.highlighted = 0

    def on_input_changed(self, event: Input.Changed) -> None:
        self._fill(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        options = self.query_one("#picker-list", OptionList)
        if options.highlighted is not None and options.option_count:
            self.dismiss(str(options.get_option_at_index(options.highlighted).id))

    def on_key(self, event: events.Key) -> None:
        options = self.query_one("#picker-list", OptionList)
        if event.key == "down":
            event.stop()
            options.action_cursor_down()
        elif event.key == "up":
            event.stop()
            options.action_cursor_up()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option_id))

    def action_cancel(self) -> None:
        self.dismiss(None)


class KeyScreen(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, provider_name: str) -> None:
        super().__init__()
        self.provider_name = provider_name

    def compose(self) -> ComposeResult:
        with Vertical(id="keybox", classes="dialog"):
            yield Label(
                Text(f"API key for {self.provider_name}", style="bold"), classes="dialog-title"
            )
            yield Static(
                Text(
                    "Stored encrypted in ~/.kalash/auth.json. Enter to save, Esc to cancel.",
                    style="dim",
                ),
                classes="dialog-line",
            )
            yield Input(password=True, placeholder="paste key")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TextScreen(ModalScreen[str | None]):
    """A one-line text entry (rename)."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str, value: str = "") -> None:
        super().__init__()
        self.title_text, self.value = title, value

    def compose(self) -> ComposeResult:
        with Vertical(id="textbox", classes="dialog"):
            yield Label(Text(self.title_text, style="bold"), classes="dialog-title")
            yield Input(value=self.value)

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        ("escape", "close", "Close"),
        ("q", "close", "Close"),
        ("question_mark", "close", ""),
    ]

    def __init__(self, content: RenderableType) -> None:
        super().__init__()
        self.content = content

    def compose(self) -> ComposeResult:
        with Vertical(id="help", classes="dialog"):
            yield Label(Text("Kalash help", style="bold"), classes="dialog-title")
            with VerticalScroll(id="help-body"):
                yield Static(self.content)
            yield Static(Text("Esc or q to close", style="dim"), id="help-footer")

    def action_close(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no question for something dangerous. Cancel is the default; y only acts after a
    short delay, like the approval dialog, so typing into the prompt cannot confirm it."""

    BINDINGS = [("escape", "answer(False)", "Cancel")]

    def __init__(self, title: str, message: str, confirm: str = "Confirm") -> None:
        super().__init__()
        self.title_text, self.message, self.confirm_label = title, message, confirm
        self._armed = False

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm", classes="dialog"):
            yield Label(Text(self.title_text, style="bold"), classes="dialog-title warn")
            yield Static(Text(self.message), classes="dialog-line")
            with Horizontal(id="confirm-actions"):
                yield Button(f"{self.confirm_label}  y", id="yes", variant="error", disabled=True)
                yield Button("Cancel  n", id="no", variant="default")

    def on_mount(self) -> None:
        self.set_timer(ARM_DELAY_S, self._arm)
        self.query_one("#no", Button).focus()

    def _arm(self) -> None:
        self._armed = True
        self.query_one("#yes", Button).disabled = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_answer(event.button.id == "yes")

    def on_key(self, event: events.Key) -> None:
        if event.key in ("y", "n"):
            event.stop()
            self.action_answer(event.key == "y")

    def action_answer(self, yes: bool) -> None:
        if yes and not self._armed:
            return
        self.dismiss(yes)
