"""Compact unified diffs for display.

File edits reported only a byte count ("Wrote 412 bytes to app.py"), which tells
the user nothing about *what* changed. Every comparable CLI agent shows the change
inline, because reviewing a diff is how a developer decides whether to keep going
or interrupt.

Diffs here are for humans, not for patch application: they are truncated, they
collapse long runs of unchanged lines, and they do not carry the context needed by
``patch``. The authoritative content is always the file on disk.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

# Lines of unchanged context kept either side of a change.
CONTEXT_LINES = 2

# Ceiling on rendered diff lines. A 4,000-line rewrite is not reviewable inline,
# and forcing it into the transcript costs tokens and screen without helping.
MAX_DIFF_LINES = 60

# Individual lines longer than this are elided in the middle.
MAX_LINE_WIDTH = 200


@dataclass(frozen=True, slots=True)
class DiffStat:
    """Summary of a change."""

    added: int
    removed: int
    truncated: bool = False

    @property
    def is_empty(self) -> bool:
        return self.added == 0 and self.removed == 0

    def render(self) -> str:
        """Compact ``+12/-4`` style summary."""
        return f"+{self.added}/-{self.removed}"


@dataclass(frozen=True, slots=True)
class FileDiff:
    """A rendered diff plus its statistics."""

    path: str
    text: str
    stat: DiffStat

    @property
    def is_empty(self) -> bool:
        return self.stat.is_empty


def _clip(line: str) -> str:
    if len(line) <= MAX_LINE_WIDTH:
        return line
    half = MAX_LINE_WIDTH // 2
    return f"{line[:half]} … {line[-half:]}"


def make_diff(path: str, before: str, after: str) -> FileDiff:
    """Build a display diff between two versions of a file.

    A new file (empty ``before``) renders as all additions, which is the correct
    representation — the user still wants to see what was written.
    """
    old_lines = before.splitlines()
    new_lines = after.splitlines()

    rendered: list[str] = []
    added = 0
    removed = 0
    truncated = False

    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            span = i2 - i1
            if span <= CONTEXT_LINES * 2:
                for line in old_lines[i1:i2]:
                    rendered.append(f"  {_clip(line)}")
            else:
                for line in old_lines[i1 : i1 + CONTEXT_LINES]:
                    rendered.append(f"  {_clip(line)}")
                rendered.append(f"  … {span - CONTEXT_LINES * 2} unchanged lines")
                for line in old_lines[i2 - CONTEXT_LINES : i2]:
                    rendered.append(f"  {_clip(line)}")
            continue

        if tag in ("replace", "delete"):
            for line in old_lines[i1:i2]:
                rendered.append(f"- {_clip(line)}")
                removed += 1
        if tag in ("replace", "insert"):
            for line in new_lines[j1:j2]:
                rendered.append(f"+ {_clip(line)}")
                added += 1

    if len(rendered) > MAX_DIFF_LINES:
        keep = MAX_DIFF_LINES
        hidden = len(rendered) - keep
        rendered = rendered[:keep]
        rendered.append(f"  … {hidden} more diff lines")
        truncated = True

    return FileDiff(
        path=path,
        text="\n".join(rendered),
        stat=DiffStat(added=added, removed=removed, truncated=truncated),
    )


def summarize(path: str, before: str, after: str) -> str:
    """One-line change summary, e.g. ``app.py +12/-4``."""
    diff = make_diff(path, before, after)
    return f"{path} {diff.stat.render()}"
