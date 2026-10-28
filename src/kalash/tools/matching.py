"""Help a model recover from an ``edit`` that did not apply, and confirm one that did.

Observed on a real model (Sarvam 105B building a React/Node app): the same near-miss ``old_string``
was retried over and over, each time followed by a full ``read`` to look for the difference. A bare
"old_string was not found" gives the model nothing to correct, so the tool says *where* the nearest
text is, shows it as the file really has it, and names the usual causes (indentation, copied
``N: `` line-number prefixes from ``read`` output). After a successful edit the changed region is
shown with line numbers so the model does not need another ``read`` to check its work.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# a line as `read` prints it: "12: text"
LINE_PREFIX = re.compile(r"^\s*\d+: ?")
MAX_SHOWN_LINES = 12
CONTEXT_LINES = 2
MAX_SCANNED_LINES = 20_000
MIN_SIMILARITY = 0.6


def numbered(lines: list[str], first: int, limit: int = MAX_SHOWN_LINES) -> str:
    """``read``-style numbered lines (``first`` is the 1-based number of ``lines[0]``)."""
    shown = [f"{first + i}: {line}" for i, line in enumerate(lines[:limit])]
    if len(lines) > limit:
        shown.append(f"... ({len(lines) - limit} more lines)")
    return "\n".join(shown)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _window_matches(body_lines: list[str], old_lines: list[str]) -> list[int]:
    """Start indexes where the file has the same lines as ``old_lines`` ignoring indentation."""
    size = len(old_lines)
    wanted = [line.strip() for line in old_lines]
    if not any(wanted):
        return []
    return [
        start
        for start in range(len(body_lines) - size + 1)
        if [line.strip() for line in body_lines[start : start + size]] == wanted
    ]


def _closest_window(body_lines: list[str], old_lines: list[str]) -> tuple[int, float] | None:
    size = len(old_lines)
    if not body_lines or size == 0 or len(body_lines) > MAX_SCANNED_LINES:
        return None
    first = old_lines[0].strip()
    wanted = _squash("\n".join(old_lines))
    best: tuple[int, float] | None = None
    # only windows whose first line is at least vaguely like ours are worth a full comparison
    for start in range(max(1, len(body_lines) - size + 1)):
        head = body_lines[start].strip()
        if first and SequenceMatcher(None, first, head).quick_ratio() < 0.5:
            continue
        window = _squash("\n".join(body_lines[start : start + size]))
        ratio = SequenceMatcher(None, wanted, window).ratio()
        if best is None or ratio > best[1]:
            best = (start, ratio)
    return best if best is not None and best[1] >= MIN_SIMILARITY else None


def explain_no_match(body: str, old: str, path: str) -> str:
    """Why ``old`` is not in ``body``, as specifically as can be told cheaply."""
    head = f"old_string was not found in {path}."
    old_lines = old.split("\n")
    body_lines = body.split("\n")

    non_empty = [line for line in old_lines if line.strip()]
    if non_empty and all(LINE_PREFIX.match(line) for line in non_empty):
        stripped = "\n".join(LINE_PREFIX.sub("", line, count=1) for line in old_lines)
        hint = " The stripped text does occur in the file." if stripped in body else ""
        return (
            f"{head} Your old_string starts every line with a line number like `12: `. Those "
            "numbers are display only (from `read`) and are not in the file; remove them and "
            f"keep the original indentation.{hint}"
        )

    same_ignoring_indent = _window_matches(body_lines, old_lines)
    if same_ignoring_indent:
        start = same_ignoring_indent[0]
        actual = body_lines[start : start + len(old_lines)]
        return (
            f"{head} The text exists at lines {start + 1}-{start + len(old_lines)} but its "
            "indentation or trailing whitespace differs from yours. The file has:\n"
            f"{numbered(actual, start + 1)}\n"
            "Copy old_string from this, whitespace included."
        )

    close = _closest_window(body_lines, old_lines)
    if close is not None:
        start, ratio = close
        actual = body_lines[start : start + len(old_lines)]
        return (
            f"{head} The closest text is at lines {start + 1}-{start + len(old_lines)} "
            f"({round(ratio * 100)}% similar). The file has:\n{numbered(actual, start + 1)}\n"
            "Fix old_string to match it exactly, or `read` that range if it has changed."
        )

    return (
        f"{head} Nothing in the file resembles it (the file has {len(body_lines)} lines). "
        "`read` the area you mean to change, or use `grep` to find it, then copy old_string from "
        "the output without the `N: ` prefixes."
    )


def occurrence_lines(body: str, old: str, limit: int = 8) -> list[int]:
    """1-based line numbers where ``old`` starts, for the 'appears N times' message."""
    lines: list[int] = []
    index = body.find(old)
    while index != -1 and len(lines) < limit:
        line = body.count("\n", 0, index) + 1
        if not lines or lines[-1] != line:  # two hits on one line are one place to look
            lines.append(line)
        index = body.find(old, index + max(1, len(old)))
    return lines


def edit_excerpt(updated: str, start_index: int, new: str) -> str:
    """The edited region of ``updated`` with a little context, numbered like ``read``."""
    first = updated.count("\n", 0, start_index) + 1
    last = first + new.count("\n")
    lines = updated.split("\n")
    lo = max(1, first - CONTEXT_LINES)
    hi = min(len(lines), last + CONTEXT_LINES)
    truncated = hi - lo + 1 > MAX_SHOWN_LINES
    if truncated:  # a long replacement: show its beginning only
        hi = lo + MAX_SHOWN_LINES - 1
    text = numbered(lines[lo - 1 : hi], lo)
    if truncated:
        text += f"\n... (the edit continues to line {last})"
    return f"Now lines {lo}-{hi}:\n{text}"
