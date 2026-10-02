"""Addressable observation store — the agent's durable scratchpad.

The problem this solves: a coding agent's context window fills with large tool
observations (file reads, search hits, web results, command output) that are
mostly irrelevant a few turns later, yet keep being billed on every subsequent
request for the rest of the session.

The usual fix is summarization, which is lossy and irreversible. This module
takes a different approach: an observation is written to a content-addressed
store and only a one-line **headline plus a short reference** enters the
context. When the agent needs the body it calls ``expand(ref)`` and gets it
back verbatim. Nothing is destroyed, so nothing has to be guessed at later.

Three properties make it pay off:

* **Refs are tiny.** ``#f7`` is one or two tokens. A ref-plus-headline entry
  costs ~12 tokens where the observation it stands for might cost 6,000.

* **Dedupe is free and automatic.** Bodies are addressed by SHA-256, so reading
  an unchanged file a second time resolves to the *existing* ref and adds zero
  new context. This is the common case in a long session and it normally costs
  a full re-read.

* **It survives.** The index is persisted per session, so observations outlive
  compaction, process restarts, and session resume. Notes the agent writes to
  itself are the one thing compaction may never drop.

Bodies at or under :data:`INLINE_MAX_BYTES` are kept in the index; larger ones
go to the shared blob store, which verifies integrity on read (I-025).

Not thread-safe. Callers are expected to be a single asyncio task per session,
which is how the agent loop drives it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalash.core.budget import estimate_tokens
from kalash.core.encode import fold_paths
from kalash.core.paths import kalash_home
from kalash.storage.blobs import compute_digest, read_blob, store_blob

# Bodies at or below this size live in the index rather than the blob store.
# Small observations are the majority, and a blob file per grep hit would spray
# thousands of tiny files across the store for no benefit.
INLINE_MAX_BYTES = 2048

# Default ceiling on a single expand() result. An expand that returns 200 KiB
# defeats the purpose of having stored it out of context in the first place.
DEFAULT_EXPAND_MAX_BYTES = 16 * 1024

# Lines of surrounding context returned around each grep match.
GREP_CONTEXT_LINES = 2

# One letter per observation kind, so refs stay at one or two tokens. The letter
# also tells the model what it is looking at without a lookup.
KIND_LETTERS: dict[str, str] = {
    "file": "f",
    "search": "s",
    "web": "w",
    "fetch": "u",
    "shell": "x",
    "note": "n",
    "other": "o",
}

_REF_PATTERN = re.compile(r"^#([a-z])(\d+)$")

# Dots are excluded deliberately. Replacing only the separators in a session id
# like "../../etc/passwd" would leave "..\_..\_etc_passwd", which is a harmless
# filename but an alarming one to find in a directory listing. Generated session
# ids are alphanumeric with underscores, so excluding dots costs nothing real.
_SESSION_SAFE = re.compile(r"[^A-Za-z0-9_-]")


@dataclass(frozen=True, slots=True)
class Observation:
    """A single stored observation.

    ``headline`` is the only part that routinely enters the context window; it
    must be one line and self-describing.
    """

    ref: str
    kind: str
    headline: str
    digest: str
    size_bytes: int
    lines: int
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)
    inline: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Serialize for the on-disk index."""
        return {
            "ref": self.ref,
            "kind": self.kind,
            "headline": self.headline,
            "digest": self.digest,
            "size_bytes": self.size_bytes,
            "lines": self.lines,
            "created_at": self.created_at,
            "metadata": self.metadata,
            "inline": self.inline,
        }

    @staticmethod
    def from_json(raw: dict[str, Any]) -> Observation:
        """Rehydrate from the on-disk index."""
        return Observation(
            ref=str(raw["ref"]),
            kind=str(raw.get("kind", "other")),
            headline=str(raw.get("headline", "")),
            digest=str(raw.get("digest", "")),
            size_bytes=int(raw.get("size_bytes", 0)),
            lines=int(raw.get("lines", 0)),
            created_at=str(raw.get("created_at", "")),
            metadata=dict(raw.get("metadata") or {}),
            inline=raw.get("inline"),
        )


@dataclass(frozen=True, slots=True)
class PutResult:
    """Outcome of storing an observation."""

    ref: str
    observation: Observation
    deduped: bool
    """True when identical content was already stored and the existing ref was
    returned. The caller added zero new context."""

    tokens_deferred: int
    """Estimated tokens the body would have cost had it gone into context."""


@dataclass(frozen=True, slots=True)
class ExpandResult:
    """Outcome of pulling an observation back into context."""

    ok: bool
    ref: str
    content: str = ""
    total_lines: int = 0
    shown_lines: int = 0
    truncated: bool = False
    error: str = ""


class Scratchpad:
    """Session-scoped addressable store for tool observations and agent notes."""

    INDEX_VERSION = 1

    def __init__(self, session_id: str, *, root: Path | None = None) -> None:
        self.session_id = session_id
        self._root = root if root is not None else kalash_home() / "scratchpad"
        self._observations: dict[str, Observation] = {}
        self._by_digest: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        self._loaded = False

    # -- paths ---------------------------------------------------------------

    @property
    def index_path(self) -> Path:
        """Location of this session's index file."""
        safe = _SESSION_SAFE.sub("_", self.session_id)[:120] or "default"
        return self._root / f"{safe}.json"

    # -- writing -------------------------------------------------------------

    def put(
        self,
        kind: str,
        headline: str,
        body: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> PutResult:
        """Store an observation and return its reference.

        Identical content resolves to the existing ref rather than allocating a
        new one, so a repeated observation costs nothing.
        """
        self._ensure_loaded()

        payload = body.encode("utf-8")
        digest = compute_digest(payload)
        deferred = estimate_tokens(body)

        existing_ref = self._by_digest.get(digest)
        if existing_ref is not None:
            existing = self._observations[existing_ref]
            return PutResult(
                ref=existing_ref,
                observation=existing,
                deduped=True,
                tokens_deferred=deferred,
            )

        normalized_kind = kind if kind in KIND_LETTERS else "other"
        ref = self._next_ref(normalized_kind)

        inline: str | None = None
        if len(payload) <= INLINE_MAX_BYTES:
            inline = body
        else:
            store_blob(payload)

        observation = Observation(
            ref=ref,
            kind=normalized_kind,
            headline=_one_line(headline),
            digest=digest,
            size_bytes=len(payload),
            lines=body.count("\n") + 1 if body else 0,
            created_at=datetime.now(UTC).isoformat(),
            metadata=dict(metadata or {}),
            inline=inline,
        )

        self._observations[ref] = observation
        self._by_digest[digest] = ref
        self.save()

        return PutResult(
            ref=ref,
            observation=observation,
            deduped=False,
            tokens_deferred=deferred,
        )

    def note(self, text: str, *, metadata: dict[str, Any] | None = None) -> PutResult:
        """Record a durable note the agent wrote to itself.

        Notes are the one observation kind rendered in full into context, and
        the one kind compaction must never drop.
        """
        return self.put(
            "note",
            _one_line(text) if "\n" not in text else _one_line(text.splitlines()[0]),
            text,
            metadata=metadata,
        )

    # -- reading -------------------------------------------------------------

    def get(self, ref: str) -> Observation | None:
        """Look up an observation by ref, or None if unknown."""
        self._ensure_loaded()
        return self._observations.get(_normalize_ref(ref))

    def body(self, ref: str) -> str | None:
        """Return an observation's full body, or None if unavailable.

        Never raises: a missing or corrupt blob yields None so the caller can
        report it as a recoverable tool error.
        """
        observation = self.get(ref)
        if observation is None:
            return None
        if observation.inline is not None:
            return observation.inline
        try:
            return read_blob(observation.digest).decode("utf-8", errors="replace")
        except (FileNotFoundError, ValueError, OSError):
            return None

    def expand(
        self,
        ref: str,
        *,
        offset: int = 0,
        limit: int | None = None,
        grep: str | None = None,
        max_bytes: int = DEFAULT_EXPAND_MAX_BYTES,
    ) -> ExpandResult:
        """Pull a stored observation back into context.

        ``grep`` returns only matching lines with surrounding context, which is
        usually what the agent wants and far cheaper than the whole body.
        """
        normalized = _normalize_ref(ref)
        observation = self.get(normalized)
        if observation is None:
            known = ", ".join(sorted(self._observations)) or "none"
            return ExpandResult(
                ok=False,
                ref=normalized,
                error=f"Unknown ref {normalized!r}. Stored refs: {known}",
            )

        text = self.body(normalized)
        if text is None:
            return ExpandResult(
                ok=False,
                ref=normalized,
                error=(
                    f"Body for {normalized} is no longer retrievable "
                    f"(digest {observation.digest[:12]})."
                ),
            )

        lines = text.splitlines()
        total = len(lines)

        if grep:
            selected = _grep_lines(lines, grep)
            if not selected:
                return ExpandResult(
                    ok=True,
                    ref=normalized,
                    content=f"(no lines in {normalized} match {grep!r})",
                    total_lines=total,
                    shown_lines=0,
                )
        else:
            start = max(0, offset)
            end = total if limit is None else min(total, start + max(0, limit))
            selected = [(i + 1, lines[i]) for i in range(start, end)]

        rendered, truncated = _render_numbered(selected, max_bytes)
        return ExpandResult(
            ok=True,
            ref=normalized,
            content=rendered,
            total_lines=total,
            shown_lines=len(rendered.splitlines()),
            truncated=truncated,
        )

    # -- rendering -----------------------------------------------------------

    def render_index(
        self,
        *,
        kinds: tuple[str, ...] | None = None,
        limit: int | None = None,
    ) -> str:
        """Render the compact index that goes into the context window.

        File paths share a folded prefix so a large index does not pay for the
        same directory string on every line.
        """
        self._ensure_loaded()
        observations = [o for o in self._observations.values() if o.kind != "note"]
        if kinds is not None:
            observations = [o for o in observations if o.kind in kinds]
        observations.sort(key=lambda o: o.created_at)
        if limit is not None:
            observations = observations[-limit:]

        if not observations:
            return ""

        paths = [str(o.metadata.get("path", "")) for o in observations]
        foldable = [p for p in paths if p]
        folded_lookup: dict[str, str] = {}
        preamble = ""
        if len(foldable) > 1:
            folded = fold_paths(foldable)
            if folded.folded:
                # fold_paths preserves length and order; strict=True makes that
                # contract explicit rather than silently truncating if it changes.
                folded_lookup = dict(zip(foldable, folded.items, strict=True))
                preamble = f"@ = {folded.prefix}"

        lines: list[str] = []
        for observation in observations:
            path = str(observation.metadata.get("path", ""))
            label = folded_lookup.get(path, observation.headline) if path else observation.headline
            lines.append(f"{observation.ref} {label} {_size_hint(observation)}".rstrip())

        body = "\n".join([preamble, *lines]) if preamble else "\n".join(lines)
        return (
            f"<scratchpad refs={len(observations)}>\n{body}\n"
            f"expand(ref) to retrieve any of these in full\n</scratchpad>"
        )

    def render_notes(self) -> str:
        """Render agent notes in full. Never dropped by compaction."""
        self._ensure_loaded()
        notes = [o for o in self._observations.values() if o.kind == "note"]
        notes.sort(key=lambda o: o.created_at)
        if not notes:
            return ""
        rendered = []
        for note in notes:
            text = self.body(note.ref) or note.headline
            rendered.append(f"{note.ref} {text}")
        return "<notes>\n" + "\n".join(rendered) + "\n</notes>"

    def stats(self) -> dict[str, Any]:
        """Counts and deferred-byte totals, for `/status` and diagnostics."""
        self._ensure_loaded()
        by_kind: dict[str, int] = {}
        for observation in self._observations.values():
            by_kind[observation.kind] = by_kind.get(observation.kind, 0) + 1
        stored = sum(o.size_bytes for o in self._observations.values())
        return {
            "session_id": self.session_id,
            "refs": len(self._observations),
            "by_kind": by_kind,
            "stored_bytes": stored,
            "index_path": str(self.index_path),
        }

    # -- persistence ---------------------------------------------------------

    def save(self) -> None:
        """Write the index atomically."""
        path = self.index_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "version": self.INDEX_VERSION,
            "session_id": self.session_id,
            "counters": self._counters,
            "observations": [o.to_json() for o in self._observations.values()],
        }
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            # Atomic rename, so a crash mid-write leaves the previous index intact
            # rather than a half-written one.
            tmp.replace(path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    def load(self) -> None:
        """Read the index from disk, tolerating absence and corruption."""
        self._loaded = True
        self._observations = {}
        self._by_digest = {}
        self._counters = {}

        path = self.index_path
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt index must not take the session down; the bodies are
            # still in the blob store and a fresh index is a safe fallback.
            return

        self._counters = {str(k): int(v) for k, v in (payload.get("counters") or {}).items()}
        for raw in payload.get("observations") or []:
            try:
                observation = Observation.from_json(raw)
            except (KeyError, TypeError, ValueError):
                continue
            self._observations[observation.ref] = observation
            self._by_digest.setdefault(observation.digest, observation.ref)

    def clear(self) -> None:
        """Drop every observation for this session and remove the index."""
        self._observations = {}
        self._by_digest = {}
        self._counters = {}
        self._loaded = True
        self.index_path.unlink(missing_ok=True)

    # -- internals -----------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _next_ref(self, kind: str) -> str:
        letter = KIND_LETTERS[kind]
        nxt = self._counters.get(letter, 0) + 1
        self._counters[letter] = nxt
        return f"#{letter}{nxt}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _one_line(text: str) -> str:
    """Collapse to a single line — headlines are rendered one per row."""
    return " ".join(text.split()).strip()


def _normalize_ref(ref: str) -> str:
    """Accept ``#f1``, ``f1``, and stray whitespace as the same reference."""
    candidate = ref.strip()
    if not candidate.startswith("#"):
        candidate = "#" + candidate
    return candidate.lower()


def _size_hint(observation: Observation) -> str:
    """Compact size annotation, e.g. ``902L`` or ``4.2KB``."""
    if observation.lines > 1:
        return f"{observation.lines}L"
    if observation.size_bytes >= 1024:
        return f"{observation.size_bytes / 1024:.1f}KB"
    return f"{observation.size_bytes}B"


def _grep_lines(lines: list[str], pattern: str) -> list[tuple[int, str]]:
    """Return 1-indexed matching lines with surrounding context, merged."""
    try:
        matcher = re.compile(pattern, re.IGNORECASE)
        matches = [i for i, line in enumerate(lines) if matcher.search(line)]
    except re.error:
        needle = pattern.lower()
        matches = [i for i, line in enumerate(lines) if needle in line.lower()]

    if not matches:
        return []

    keep: set[int] = set()
    for index in matches:
        low = max(0, index - GREP_CONTEXT_LINES)
        high = min(len(lines), index + GREP_CONTEXT_LINES + 1)
        keep.update(range(low, high))

    return [(i + 1, lines[i]) for i in sorted(keep)]


def _render_numbered(selected: list[tuple[int, str]], max_bytes: int) -> tuple[str, bool]:
    """Render numbered lines under a byte ceiling.

    Line numbers are load-bearing rather than decorative: they are what lets a
    follow-up edit address a location without re-sending its surrounding text.
    """
    out: list[str] = []
    used = 0
    truncated = False
    for number, text in selected:
        row = f"{number}|{text}"
        encoded = row.encode("utf-8")
        # One minified line must not hide all subsequent grep matches.
        if len(encoded) > max_bytes:
            allowance = max_bytes // 2
            marker = " [line truncated]"
            room = max(0, allowance - len(marker.encode("utf-8")))
            row = encoded[:room].decode("utf-8", errors="ignore") + marker
            truncated = True
        cost = len(row.encode("utf-8")) + 1
        if used + cost > max_bytes:
            truncated = True
            break
        out.append(row)
        used += cost
    return "\n".join(out), truncated


# ---------------------------------------------------------------------------
# Session-scoped access
# ---------------------------------------------------------------------------

_CACHE: dict[str, Scratchpad] = {}


def get_scratchpad(session_id: str, *, root: Path | None = None) -> Scratchpad:
    """Return the scratchpad for a session, loading it on first access.

    The cache is a read-through over durable state, not the state itself: a
    fresh process reloads the same observations from the index on disk.
    """
    key = f"{root}:{session_id}" if root is not None else session_id
    pad = _CACHE.get(key)
    if pad is None:
        pad = Scratchpad(session_id, root=root)
        pad.load()
        _CACHE[key] = pad
    return pad


def reset_cache() -> None:
    """Drop the in-process cache. Intended for tests."""
    _CACHE.clear()
