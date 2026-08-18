"""Terminal approval prompt.

:class:`~kalash.permissions.prompt.ApprovalPrompt` was written against a
``UIAdapter`` protocol that nothing implemented, so ``ui`` was always ``None``
and every ASK decision fell through to its fail-closed default of DENY. The
policy was not merely unenforced — it was unusable. This is the adapter.

Two decisions worth stating:

**Prompts go to stderr, never stdout.** ``kalash -p "…" > out.txt`` has to stay
pipeable, and a permission prompt interleaved into the model's answer would
corrupt the output the user is capturing.

**Reading stdin happens on a worker thread.** ``input()`` blocks, and blocking
the event loop would freeze the heartbeat and make Ctrl-C unresponsive while the
agent waits for an answer.

Every failure mode resolves to DENY: EOF, a closed stdin, an interrupt, or an
unrecognised answer. There is no path through this file that approves something
the user did not actively approve.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from typing import TextIO

from kalash.permissions.prompt import (
    ApprovalPrompt,
    ApprovalResponse,
    PromptContext,
    PromptResult,
)


@dataclass
class ConsoleUIAdapter:
    """Renders approval prompts to a terminal and reads the answer."""

    stream: TextIO = field(default_factory=lambda: sys.stderr)
    stdin: TextIO = field(default_factory=lambda: sys.stdin)

    async def show_approval_prompt(self, context: PromptContext) -> PromptResult:
        """Render the prompt and wait for a keypress-style answer."""
        self._write(ApprovalPrompt.render_text(context))
        self._write("  > ")

        raw = await self._read_line()
        if raw is None:
            self._write("\n  denied (no input available)\n")
            return PromptResult(
                response=ApprovalResponse.DENY,
                reason="stdin unavailable — cannot obtain approval",
            )

        result = ApprovalPrompt.parse_response(raw)
        self._write(f"  -> {result.response.name.lower().replace('_', ' ')}\n\n")
        return result

    async def show_info(self, message: str) -> None:
        """Print an informational line."""
        self._write(f"{message}\n")

    # -- internals -----------------------------------------------------------

    def _write(self, text: str) -> None:
        try:
            self.stream.write(text)
            self.stream.flush()
        except (OSError, ValueError):
            # A closed or detached stream must not crash the turn.
            pass

    async def _read_line(self) -> str | None:
        """Read one line off the worker thread, or None if impossible."""
        try:
            if not self.stdin.readable() or self.stdin.closed:
                return None
        except (AttributeError, ValueError):
            return None

        try:
            return await asyncio.to_thread(self.stdin.readline)
        except (EOFError, KeyboardInterrupt, OSError, ValueError, RuntimeError):
            return None


def is_interactive(stdin: TextIO | None = None, stream: TextIO | None = None) -> bool:
    """Whether a human can plausibly answer a prompt right now.

    Both ends must be a terminal: piped stdin means nobody is typing, and a
    redirected stderr means the question would never be seen.
    """
    src = stdin if stdin is not None else sys.stdin
    out = stream if stream is not None else sys.stderr
    try:
        return bool(src.isatty() and out.isatty())
    except (AttributeError, ValueError):
        return False


def build_approval_prompt(
    event_bus: object,
    *,
    force_non_interactive: bool | None = None,
) -> ApprovalPrompt:
    """Construct an :class:`ApprovalPrompt` wired to the terminal.

    When no terminal is attached the prompt is built in non-interactive mode,
    which denies rather than hanging on a question nobody will answer.
    """
    non_interactive = (
        not is_interactive() if force_non_interactive is None else force_non_interactive
    )
    return ApprovalPrompt(
        event_bus=event_bus,  # type: ignore[arg-type]
        ui=None if non_interactive else ConsoleUIAdapter(),
        non_interactive=non_interactive,
    )
