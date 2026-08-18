"""Approval modal for the TUI.

The terminal adapter in :mod:`kalash.permissions.console` reads stdin directly,
which the TUI cannot do — Textual owns the input stream. So the TUI needs its own
``UIAdapter``, and this is it.

:class:`ApprovalScreen` is a modal that resolves to a
:class:`~kalash.permissions.prompt.PromptResult`. :class:`TuiApprovalAdapter`
pushes it and waits, which works because the send path runs inside a Textual
worker and ``push_screen_wait`` is awaitable there.

Dismissing the modal without choosing — escape, or the screen being torn down —
resolves to DENY. There is no path here that approves something the user did not
actively approve.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from kalash.permissions.prompt import (
    ApprovalResponse,
    PromptContext,
    PromptResult,
)

# Shown when a path list would otherwise dominate the dialog.
MAX_PATHS_SHOWN = 6

_RISK_STYLE: dict[str, str] = {
    "read": "green",
    "network": "cyan",
    "write": "yellow",
    "write_remote": "magenta",
    "destructive": "bold red",
}


class ApprovalScreen(ModalScreen[PromptResult]):
    """Modal asking the user to approve one tool call."""

    BINDINGS = [
        Binding("a", "allow_once", "allow once", show=False),
        Binding("s", "allow_session", "allow session", show=False),
        Binding("d", "deny", "deny", show=False),
        Binding("escape", "deny", "deny", show=False),
        Binding("y", "allow_once", "allow", show=False),
        Binding("n", "deny", "deny", show=False),
    ]

    DEFAULT_CSS = """
    ApprovalScreen {
        align: center middle;
        background: $background 60%;
    }
    #approval-box {
        width: 84;
        max-width: 96%;
        height: auto;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }
    #approval-title {
        text-style: bold;
        color: $warning;
    }
    #approval-keys {
        margin-top: 1;
        color: $text-muted;
    }
    """

    def __init__(self, request: PromptContext) -> None:
        super().__init__()
        # Deliberately not named `_context`: Textual's MessagePump defines a
        # `_context()` method, and an instance attribute shadows it. The screen
        # still mounted, but its message pump raised
        # `TypeError: 'PromptContext' object is not callable` on every message —
        # so the dialog could never process a keypress and the agent waited on an
        # answer that was impossible to give. That is the freeze.
        self._request = request

    def compose(self) -> ComposeResult:
        from rich.text import Text

        ctx = self._request
        body = Text()

        body.append("action  ", style="dim")
        body.append(f"{ctx.action}\n")

        if ctx.command:
            body.append("command ", style="dim")
            body.append(f"{ctx.command}\n", style="bold")

        for path in ctx.paths[:MAX_PATHS_SHOWN]:
            body.append("path    ", style="dim")
            body.append(f"{path}\n")
        if len(ctx.paths) > MAX_PATHS_SHOWN:
            body.append("        ", style="dim")
            body.append(f"and {len(ctx.paths) - MAX_PATHS_SHOWN} more\n", style="dim")

        body.append("risk    ", style="dim")
        body.append(
            f"{ctx.risk_class}",
            style=_RISK_STYLE.get(ctx.risk_class, "yellow"),
        )
        body.append(f"  ·  {ctx.reversibility}\n", style="dim")

        if ctx.confirmation_classes:
            body.append("flags   ", style="dim")
            body.append(f"{', '.join(ctx.confirmation_classes)}\n", style="magenta")

        reason = str(ctx.metadata.get("reason", "")).strip()
        if reason:
            body.append("why     ", style="dim")
            body.append(f"{reason}\n")

        with Vertical(id="approval-box"):
            yield Static("permission required", id="approval-title")
            yield Static(body)
            yield Static(
                "[a] allow once   [s] allow for session   [d] deny   esc denies",
                id="approval-keys",
            )

    def action_allow_once(self) -> None:
        self.dismiss(PromptResult(response=ApprovalResponse.ALLOW_ONCE))

    def action_allow_session(self) -> None:
        self.dismiss(PromptResult(response=ApprovalResponse.ALLOW_SESSION))

    def action_deny(self) -> None:
        self.dismiss(
            PromptResult(
                response=ApprovalResponse.DENY,
                reason="declined in the approval dialog",
            )
        )


@dataclass
class TuiApprovalAdapter:
    """``UIAdapter`` that surfaces approvals as a Textual modal."""

    app: object

    async def show_approval_prompt(self, context: PromptContext) -> PromptResult:
        """Push the modal and wait for the answer, denying on any failure."""
        try:
            result = await self.app.push_screen_wait(  # type: ignore[attr-defined]
                ApprovalScreen(context)
            )
        except Exception:
            # A torn-down screen stack or a cancelled worker must not be read as
            # consent.
            return PromptResult(
                response=ApprovalResponse.DENY,
                reason="approval dialog unavailable",
            )
        if result is None:
            return PromptResult(
                response=ApprovalResponse.DENY,
                reason="approval dialog dismissed",
            )
        return result

    async def show_info(self, message: str) -> None:
        notify = getattr(self.app, "notify", None)
        if callable(notify):
            notify(message)
