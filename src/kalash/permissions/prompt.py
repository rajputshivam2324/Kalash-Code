"""Approval UX contract — rendering and response handling.

Renders permission prompts with full context (action, command, paths,
risk class, reversibility) and handles user responses. Never auto-approves.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from kalash.core.events import Event, EventBus, EventType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response options
# ---------------------------------------------------------------------------


class ApprovalResponse(StrEnum):
    """User's response to an approval prompt."""

    ALLOW_ONCE = "a"  # Allow this one time
    ALLOW_SESSION = "s"  # Allow for this session
    ALLOW_ALWAYS = "A"  # Allow always (persistent)
    DENY = "d"  # Deny
    DENY_WITH_REASON = "D"  # Deny with reason
    MODIFY = "m"  # Modify the action


# ---------------------------------------------------------------------------
# Prompt data
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PromptContext:
    """All context needed to render an approval prompt."""

    action: str  # Human-readable action description
    tool_name: str
    command: str  # Exact command or operation
    paths: list[str]  # Affected paths
    capabilities: list[str]  # Capabilities required
    risk_class: str  # RiskClass value
    reversibility: str  # "reversible", "partially_reversible", "irreversible"
    confirmation_classes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromptResult:
    """Result of showing an approval prompt."""

    response: ApprovalResponse
    reason: str = ""  # For DENY_WITH_REASON
    modified_action: dict[str, Any] | None = None  # For MODIFY


# ---------------------------------------------------------------------------
# UI adapter protocol
# ---------------------------------------------------------------------------


class UIAdapter(Protocol):
    """Protocol for rendering approval prompts to the user.

    Implementations exist for TUI, GUI, and headless modes.
    """

    async def show_approval_prompt(self, context: PromptContext) -> PromptResult:
        """Show the approval prompt and wait for a response."""
        ...

    async def show_info(self, message: str) -> None:
        """Show an informational message."""
        ...


# ---------------------------------------------------------------------------
# Approval prompt
# ---------------------------------------------------------------------------


@dataclass
class ApprovalPrompt:
    """Handles the approval UX flow.

    Renders: action, exact command, affected paths, capabilities,
    risk class, and reversibility information.

    Response options: a(once), s(session), A(always), d(deny),
    D(deny+reason), m(modify).

    Key invariants:
    - Never auto-approves (no timeout → approve)
    - Non-interactive mode stops and reports
    """

    event_bus: EventBus
    ui: UIAdapter | None = None

    # Configuration.
    #
    # 300s was long enough that an approval which could not be *rendered* looked
    # exactly like a frozen application: no output, no tool line, five minutes of
    # nothing. 60s is still ample for a human who can see the prompt, and short
    # enough that a broken prompt surfaces as a denial with a reason instead of a
    # hang. Timing out always denies — never approves.
    timeout_seconds: float = 60.0
    non_interactive: bool = False  # If True, deny all (never auto-approve)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def request_approval(self, context: PromptContext) -> PromptResult:
        """Request user approval for an action.

        Args:
            context: Full context about the action needing approval.

        Returns:
            PromptResult with the user's decision.

        In non-interactive mode, immediately returns DENY.
        On timeout, returns DENY (never auto-approves).
        """
        # Emit permission requested event
        await self.event_bus.emit(Event(
            type=EventType.PERMISSION_REQUESTED,
            data={
                "tool_name": context.tool_name,
                "action": context.action,
                "risk_class": context.risk_class,
                "paths": context.paths,
            },
        ))

        # Non-interactive: stop and report, never auto-approve
        if self.non_interactive:
            logger.info(
                "Non-interactive mode: denying %s (%s)",
                context.tool_name,
                context.action,
            )
            return PromptResult(
                response=ApprovalResponse.DENY,
                reason="Non-interactive mode — requires manual approval",
            )

        # Show prompt with timeout
        if self.ui is None:
            logger.warning("No UI adapter configured — denying by default")
            return PromptResult(
                response=ApprovalResponse.DENY,
                reason="No UI adapter available",
            )

        try:
            result = await asyncio.wait_for(
                self.ui.show_approval_prompt(context),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            # Timeout: DENY (never auto-approve). The reason names the likely
            # cause, because the usual explanation is that the prompt never
            # became visible rather than that the user ignored it.
            logger.warning("Approval timeout for %s — denying", context.tool_name)
            return PromptResult(
                response=ApprovalResponse.DENY,
                reason=(
                    f"no answer within {self.timeout_seconds:.0f}s — if no prompt "
                    f"appeared, the approval UI could not be shown"
                ),
            )
        except Exception as exc:
            # A UI that raises must not take the turn down, and must not be read
            # as consent.
            logger.warning("Approval UI failed for %s: %s", context.tool_name, exc)
            return PromptResult(
                response=ApprovalResponse.DENY,
                reason=f"the approval prompt could not be shown ({exc})",
            )

        # Log decision
        logger.info(
            "Approval decision for %s: %s",
            context.tool_name,
            result.response,
        )

        return result

    # ------------------------------------------------------------------
    # Rendering helpers (for TUI/plain-text display)
    # ------------------------------------------------------------------

    @staticmethod
    def render_text(context: PromptContext) -> str:
        """Render the approval prompt as plain text (for TUI/logging).

        Returns a formatted string showing all relevant context.
        """
        lines: list[str] = []
        lines.append("=" * 60)
        lines.append("PERMISSION REQUIRED")
        lines.append("=" * 60)
        lines.append("")
        lines.append(f"  Action:        {context.action}")
        lines.append(f"  Tool:          {context.tool_name}")

        if context.command:
            lines.append(f"  Command:       {context.command}")

        if context.paths:
            lines.append(f"  Paths:")
            for p in context.paths[:10]:
                lines.append(f"    - {p}")
            if len(context.paths) > 10:
                lines.append(f"    ... and {len(context.paths) - 10} more")

        if context.capabilities:
            lines.append(f"  Capabilities:  {', '.join(context.capabilities)}")

        lines.append(f"  Risk:          {context.risk_class}")
        lines.append(f"  Reversibility: {context.reversibility}")

        if context.confirmation_classes:
            lines.append(f"  Requires:      {', '.join(context.confirmation_classes)}")

        lines.append("")
        lines.append("  [a] Allow once  [s] Allow session  [A] Allow always")
        lines.append("  [d] Deny        [D] Deny + reason  [m] Modify")
        lines.append("")

        return "\n".join(lines)

    @staticmethod
    def parse_response(raw: str) -> PromptResult:
        """Parse a raw user response string into a PromptResult.

        Handles single-character shortcuts and multi-part responses
        (e.g., 'D:some reason').
        """
        raw = raw.strip()

        if not raw:
            # Empty response = deny (safe default)
            return PromptResult(response=ApprovalResponse.DENY)

        first_char = raw[0]

        match first_char:
            case "a":
                return PromptResult(response=ApprovalResponse.ALLOW_ONCE)
            case "s":
                return PromptResult(response=ApprovalResponse.ALLOW_SESSION)
            case "A":
                return PromptResult(response=ApprovalResponse.ALLOW_ALWAYS)
            case "d":
                return PromptResult(response=ApprovalResponse.DENY)
            case "D":
                reason = raw[2:] if len(raw) > 2 and raw[1] == ":" else ""
                return PromptResult(
                    response=ApprovalResponse.DENY_WITH_REASON,
                    reason=reason,
                )
            case "m":
                return PromptResult(response=ApprovalResponse.MODIFY)
            case _:
                # Unrecognized: treat as deny
                return PromptResult(
                    response=ApprovalResponse.DENY,
                    reason=f"Unrecognized response: {raw}",
                )
