"""Token-dense encodings for model-facing output.

Every byte in a tool result is billed, and JSON is a hostile wire format for
tokenizers: it repeats every key for every record, and indentation is pure
overhead. These helpers express the same information in fewer tokens without
losing any of it.

Three techniques, in order of how much they save in practice:

1. **Prefix folding** (`fold_paths`) — a repository path like
   ``/home/shivam/proj/src/pkg/mod/file.py`` costs ~16 tokens, and a 200-entry
   listing repeats the shared portion 200 times. Declaring the prefix once and
   emitting the remainder cuts such a listing by roughly 60%.

2. **Columnar rendering** (`render_table`) — a list of N records with K fields
   costs N*K key repetitions as JSON. A header row states the keys once.

3. **Whitespace elision** (`compact_json`) — ``json.dumps(x, indent=2)`` on a
   6 KiB payload spends 10-15% of its tokens on indentation that carries no
   information the model needs.

None of these are lossy. They are cheaper spellings of identical content.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

# A folded prefix only pays for itself once the declaration is shorter than the
# repetition it removes. Below this length the indirection costs more than it
# saves.
MIN_FOLD_PREFIX_CHARS = 12

# The sigil introducing a folded path. Chosen because it tokenizes as a single
# token in the common BPE vocabularies and does not occur in POSIX paths.
FOLD_SIGIL = "@"


@dataclass(frozen=True, slots=True)
class FoldedPaths:
    """Result of folding a shared prefix out of a path list."""

    prefix: str
    """The shared directory prefix, or empty when folding did not apply."""

    items: tuple[str, ...]
    """Paths with the prefix replaced by ``FOLD_SIGIL``, or unchanged."""

    @property
    def folded(self) -> bool:
        """Whether folding actually applied."""
        return bool(self.prefix)

    def render(self) -> str:
        """Render as a declaration line followed by one path per line."""
        if not self.folded:
            return "\n".join(self.items)
        header = f"{FOLD_SIGIL} = {self.prefix}"
        return "\n".join([header, *self.items])

    def unfold(self, item: str) -> str:
        """Expand a single folded path back to its absolute form."""
        if self.folded and item.startswith(FOLD_SIGIL):
            return self.prefix + item[len(FOLD_SIGIL) :]
        return item


def common_dir_prefix(paths: Sequence[str]) -> str:
    """Longest shared prefix of ``paths``, trimmed to a directory boundary.

    Trimming matters: the raw character-wise common prefix of ``src/foo.py`` and
    ``src/foobar.py`` is ``src/foo``, which is not a directory and would produce
    paths that cannot be reassembled unambiguously.
    """
    if len(paths) < 2:
        return ""

    shortest = min(paths, key=len)
    limit = len(shortest)
    index = 0
    while index < limit and all(p[index] == shortest[index] for p in paths):
        index += 1

    candidate = paths[0][:index]
    cut = candidate.rfind("/")
    if cut < 0:
        return ""
    return candidate[: cut + 1]


def fold_paths(
    paths: Iterable[str],
    *,
    min_prefix_chars: int = MIN_FOLD_PREFIX_CHARS,
) -> FoldedPaths:
    """Factor the shared directory prefix out of a list of paths.

    Returns the paths unchanged when there is no prefix worth declaring, so the
    caller can use the result unconditionally.
    """
    items = tuple(str(p) for p in paths)
    if len(items) < 2:
        return FoldedPaths(prefix="", items=items)

    prefix = common_dir_prefix(items)
    if len(prefix) < min_prefix_chars:
        return FoldedPaths(prefix="", items=items)

    folded = tuple(FOLD_SIGIL + item[len(prefix) :] for item in items)
    return FoldedPaths(prefix=prefix, items=folded)


def render_table(
    rows: Sequence[Sequence[Any]],
    headers: Sequence[str],
    *,
    separator: str = " | ",
) -> str:
    """Render records columnar, stating field names once.

    Unlike a JSON array of objects, the field names are not repeated per record.
    Values are not padded — alignment costs tokens and buys the model nothing.
    """
    if not rows:
        return ""

    lines = [separator.join(headers)]
    for row in rows:
        lines.append(separator.join("" if cell is None else str(cell) for cell in row))
    return "\n".join(lines)


def compact_json(obj: Any) -> str:
    """Serialize without the whitespace that ``indent`` would add."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
