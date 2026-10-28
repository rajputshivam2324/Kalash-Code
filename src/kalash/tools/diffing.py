"""Unified diffs of file changes for approval previews and tool blocks (UI only)."""

from __future__ import annotations

import difflib
from dataclasses import asdict, dataclass

MAX_DIFF_LINES = 400
MAX_DIFF_BYTES = 1_000_000


@dataclass
class FileDiff:
    path: str
    added: int
    removed: int
    text: str  # unified diff body (hunk headers + +/-/space lines), possibly truncated
    new_file: bool = False
    truncated: bool = False
    whitespace_only: bool = False  # every changed line differs from its old self only in spacing

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _lines(data: bytes | None) -> list[str]:
    if not data:
        return []
    return data.decode("utf-8", "replace").splitlines()


def diff_files(path: str, before: bytes | None, after: bytes | None) -> FileDiff | None:
    """Diff of ``before`` -> ``after`` (None means the file did not exist / does not exist).

    Returns None when nothing changed or either side is too large or binary.
    """
    if (before and len(before) > MAX_DIFF_BYTES) or (after and len(after) > MAX_DIFF_BYTES):
        return None
    if (before and b"\x00" in before[:4096]) or (after and b"\x00" in after[:4096]):
        return None
    if before == after:
        return None
    old, new = _lines(before), _lines(after)
    body = list(difflib.unified_diff(old, new, lineterm="", n=3))
    added = sum(1 for ln in body if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in body if ln.startswith("-") and not ln.startswith("---"))
    kept = [ln for ln in body if not ln.startswith(("---", "+++"))]
    truncated = len(kept) > MAX_DIFF_LINES
    if truncated:
        kept = kept[:MAX_DIFF_LINES]
    changed, ws = whitespace_changes(old, new) if before is not None else (0, 0)
    return FileDiff(
        path,
        added,
        removed,
        "\n".join(kept),
        new_file=before is None,
        truncated=truncated,
        whitespace_only=changed > 0 and ws == changed,
    )


def _squash(line: str) -> str:
    return "".join(line.split())


def whitespace_changes(before: list[str], after: list[str]) -> tuple[int, int]:
    """(changed lines, of which whitespace-only) between two versions of a file.

    A "replace" block counts as whitespace-only line by line when the old and new lines have the
    same non-space characters - the signature of a model that re-indents or realigns a file while
    believing it fixed something (seen live: a whole-file rewrite whose real diff was `id: 0` ->
    `id:   0`).
    """
    changed = ws = 0
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed += max(i2 - i1, j2 - j1)
        if tag == "replace" and i2 - i1 == j2 - j1:
            ws += sum(
                1
                for a, b in zip(before[i1:i2], after[j1:j2], strict=True)
                if _squash(a) == _squash(b)
            )
    return changed, ws


def edit_preview(old: str, new: str, path: str = "") -> str:
    """A diff-like preview of an edit that has not happened yet (for approval prompts)."""
    body = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=2))
    kept = [ln for ln in body if not ln.startswith(("---", "+++"))]
    head = f"{path}\n" if path else ""
    return head + "\n".join(kept[:60]) + ("\n..." if len(kept) > 60 else "")
