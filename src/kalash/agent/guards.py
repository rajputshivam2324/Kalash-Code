"""Mid-turn recovery nudges appended to tool results: repeated failures and stale tasks.

Observed on a real model (Sarvam 105B): it ran the same failing test six times, "fixing" a variable
name each time while the real error (an ``await`` in a non-async callback) never changed. The old
reminder only fired for *byte-identical, back-to-back* calls, so interleaved edits and rewritten
descriptions hid the loop. This guard keys on *what is being attempted* (the command, or the file)
and on *whether the failure looks the same*, so an edit-test-edit-test cycle with a changing error
is left alone and one with an unchanging error is not.

The nudges are plain text appended to the tool result, so they are model-visible, logged, and need
no special event.
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Any

SIMILARITY = 0.8
IDENTICAL = 0.97  # "the same output" for the circuit breaker; stricter than a nudge
COMPARE_HEAD = 1500
COMPARE_TAIL = 500  # together: the most that is compared
VOLATILE_ARGS = {"description", "timeout_ms", "run_in_background"}


def _label_and_key(name: str, arguments: str) -> tuple[str, str]:
    """(short label for messages, identity of the attempt)."""
    try:
        args: Any = json.loads(arguments) if arguments else {}
    except ValueError:
        args = {}
    if not isinstance(args, dict):
        return name, f"{name}:{arguments}"
    if name == "bash":
        command = " ".join(str(args.get("command", "")).split())
        key = f"bash:{args.get('workdir') or ''}:{command}"
        return f"`{command[:80]}`", key
    for field in ("file_path", "pattern", "url", "path"):
        if args.get(field):
            return f"{name} {args[field]}", f"{name}:{field}:{args[field]}"
    rest = {k: v for k, v in sorted(args.items()) if k not in VOLATILE_ARGS}
    return name, f"{name}:{json.dumps(rest, sort_keys=True, default=str)}"


ERROR_LINE = re.compile(
    r"error|fail|exception|not ok|assert|expected|traceback|cannot|unexpected|denied|"
    r"not found|no such|✗|✖",
    re.IGNORECASE,
)


def _shape(text: str) -> str:
    """What a failure says, without the boilerplate around it.

    Test runners print a screenful of fixed headers and summaries; comparing whole outputs would
    call two different failures "the same". So only the lines that mention a problem are
    compared (digits blanked, so line numbers and durations do not hide a repeat).
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    picked = [line for line in lines if ERROR_LINE.search(line)][:40] or lines[-15:]
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", " | ".join(picked)))[
        : COMPARE_HEAD + COMPARE_TAIL
    ]


def _ordinal(n: int) -> str:
    return f"{n}th" if n > 3 else {1: "1st", 2: "2nd", 3: "3rd"}[n]


class RepeatFailureGuard:
    def __init__(self, warn_after: int = 3, give_up_after: int = 5, stop_after: int = 6) -> None:
        self.warn_after = warn_after
        self.give_up_after = give_up_after
        self.stop_after = stop_after
        self._failures: dict[str, list[str]] = {}
        self.tripped: str | None = None  # set when the loop must end, not just be nudged

    def clear(self) -> None:
        self._failures.clear()
        self.tripped = None

    def observe(self, name: str, arguments: str, text: str, is_error: bool) -> str | None:
        """Record one finished call; return a note to append to its result, or None."""
        label, key = _label_and_key(name, arguments)
        if not is_error:
            self._failures.pop(key, None)  # it worked: the loop, if any, is over
            return None
        shape = _shape(text)
        history = self._failures.setdefault(key, [])
        ratios = [SequenceMatcher(None, earlier, shape).ratio() for earlier in history]
        similar = 1 + sum(1 for r in ratios if r >= SIMILARITY)
        identical = 1 + sum(1 for r in ratios if r >= IDENTICAL)
        history.append(shape)
        del history[:-8]
        if identical >= self.stop_after and self.tripped is None:
            # near-identical output this many times: a progressing suite changes more than this
            self.tripped = f"{label} failed {identical} times with the same output"
        if similar == self.give_up_after:
            return (
                f"[Kalash: {label} has now failed {similar} times with essentially the same "
                "output. Stop retrying it. Tell the user what you tried, what the error says, "
                "and what you think the cause is, so they can help.]"
            )
        if similar == self.warn_after:
            return (
                f"[Kalash: this is the {_ordinal(similar)} time {label} has failed with "
                "essentially the same output, so what you are changing is not the cause. "
                "Re-read the error for the exact file and line it names, test a different "
                "explanation, or stop and tell the user what is blocking you.]"
            )
        return None


class TodoWatch:
    """Keeps the visible task list honest: a plan that never changes while work happens is noise.

    Both live runs set a six-item list in step 1 and never touched it again, so the status bar
    read "0/6 done" over a half-built app.
    """

    def __init__(self, every: int = 8) -> None:
        self.every = every
        self._since = 0

    def reset(self) -> None:
        self._since = 0

    def observe(self, tool: str, todos: list[dict[str, str]]) -> str | None:
        if tool == "todo_write":
            self._since = 0
            return None
        self._since += 1
        open_items = [t for t in todos if t.get("status") != "completed"]
        if self._since < self.every or not open_items:
            return None
        self._since = 0
        current = next((t for t in open_items if t.get("status") == "in_progress"), open_items[0])
        return (
            f"[Kalash: your task list has not been updated for {self.every} tool calls. If "
            f"'{current.get('content', '')[:80]}' or anything else is done, mark it completed "
            "with todo_write; if the plan changed, update it.]"
        )
