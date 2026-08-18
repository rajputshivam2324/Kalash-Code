"""Scratchpad tools: `expand` and `note`.

These are the agent's two doors into the addressable observation store
(:mod:`kalash.runtime.scratchpad`).

``expand`` is what makes deferring an observation safe. A large tool result
enters the context as a one-line headline plus a short ref; if the agent later
needs the body, it expands the ref and gets the content back verbatim. Without
this tool the deferral would be lossy and the agent would have to re-run the
original call — usually more expensive than never having deferred at all.

``note`` gives the agent durable memory that is cheap and immediate. Unlike the
memory layer, a note is session-local, needs no provider, and is guaranteed to
survive compaction. It is the right place for the things that must not be lost
mid-task: a constraint the user stated, an approach that already failed, a
decision and why.

Both tools operate on state inside Kalash's own home directory rather than the
user's workspace, so neither declares a workspace capability. Their handlers are
synchronous throughout, which under a single-threaded event loop means two
concurrent calls cannot interleave a read-modify-write on the index.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from kalash.runtime.scratchpad import (
    DEFAULT_EXPAND_MAX_BYTES,
    get_scratchpad,
)
from kalash.tools.base import (
    SideEffect,
    ToolContext,
    ToolEnvelope,
    TruncationInfo,
)

# A note is meant to be a durable one-liner, not a document. Capping it keeps
# the notes block — which is rendered in full on every turn — from quietly
# becoming the largest thing in the context window.
MAX_NOTE_CHARS = 2000


# ---------------------------------------------------------------------------
# ExpandTool
# ---------------------------------------------------------------------------


class ExpandParams(BaseModel):
    """Parameters for retrieving a stored observation."""

    ref: str = Field(
        description="Scratchpad reference to expand, e.g. '#f3' or '#w1'",
    )
    grep: str = Field(
        default="",
        description=(
            "Return only lines matching this regex, with 2 lines of surrounding "
            "context. Usually far cheaper than the whole body."
        ),
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="First line to return, 0-indexed. Ignored when grep is set.",
    )
    limit: int = Field(
        default=0,
        ge=0,
        description="Max lines to return; 0 means all. Ignored when grep is set.",
    )
    max_bytes: int = Field(
        default=DEFAULT_EXPAND_MAX_BYTES,
        gt=0,
        le=256 * 1024,
        description="Hard ceiling on returned bytes",
    )


class ExpandTool:
    """Retrieves a deferred observation from the scratchpad by reference."""

    @property
    def name(self) -> str:
        return "expand"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return (
            "Retrieve the full body of a scratchpad observation by its ref "
            "(e.g. '#f3'). Use 'grep' to pull only matching lines, or "
            "offset/limit for a line range. Refs appear in the <scratchpad> block."
        )

    @property
    def params(self) -> type[BaseModel]:
        return ExpandParams

    @property
    def side_effect(self) -> SideEffect:
        return SideEffect.READ

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset()

    @property
    def timeout_s(self) -> float:
        return 10.0

    @property
    def max_output_bytes(self) -> int:
        return 256 * 1024

    @property
    def idempotent(self) -> bool:
        return True

    @property
    def cancellable(self) -> bool:
        return False

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:
        return frozenset()

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        assert isinstance(args, ExpandParams)

        pad = get_scratchpad(ctx.session_id)
        result = pad.expand(
            args.ref,
            offset=args.offset,
            limit=args.limit or None,
            grep=args.grep or None,
            max_bytes=args.max_bytes,
        )

        if not result.ok:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=result.error,
                recoverable=True,
                remediation="Check the <scratchpad> block for valid refs.",
            )

        observation = pad.get(result.ref)
        header = f"{result.ref}"
        if observation is not None and observation.headline:
            header = f"{result.ref} {observation.headline}"

        # Web and fetch bodies came from outside the trust boundary; the marking
        # has to survive the round trip through storage (I-033).
        untrusted = observation is not None and observation.kind in ("web", "fetch")
        prefix = "[UNTRUSTED EXTERNAL CONTENT]\n" if untrusted else ""

        truncation = (
            TruncationInfo(
                total_lines=result.total_lines,
                shown_lines=result.shown_lines,
                total_bytes=observation.size_bytes if observation else 0,
                retrieval_hint=(
                    f"Raise max_bytes, or narrow with grep/offset/limit on {result.ref}."
                ),
            )
            if result.truncated
            else None
        )

        return ToolEnvelope.success(
            content=f"{prefix}{header}\n{result.content}",
            metadata={
                "ref": result.ref,
                "kind": observation.kind if observation else "unknown",
                "total_lines": result.total_lines,
                "shown_lines": result.shown_lines,
                "untrusted": untrusted,
            },
            truncated=result.truncated,
            truncation=truncation,
        )


# ---------------------------------------------------------------------------
# NoteTool
# ---------------------------------------------------------------------------


class NoteParams(BaseModel):
    """Parameters for the note tool."""

    action: str = Field(
        default="add",
        description="'add' to record a note, 'list' to read them all back",
    )
    text: str = Field(
        default="",
        description="Note content. Required for 'add'.",
    )


class NoteTool:
    """Durable session notes that survive compaction."""

    @property
    def name(self) -> str:
        return "note"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return (
            "Record a short durable note for this session, or list existing "
            "notes. Notes survive context compaction, so use them for user "
            "constraints, decisions and their rationale, and approaches that "
            "already failed."
        )

    @property
    def params(self) -> type[BaseModel]:
        return NoteParams

    @property
    def side_effect(self) -> SideEffect:
        return SideEffect.NONE

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset()

    @property
    def timeout_s(self) -> float:
        return 10.0

    @property
    def max_output_bytes(self) -> int:
        return 32 * 1024

    @property
    def idempotent(self) -> bool:
        return False

    @property
    def cancellable(self) -> bool:
        return False

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:
        return frozenset()

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        assert isinstance(args, NoteParams)

        pad = get_scratchpad(ctx.session_id)
        action = args.action.strip().lower()

        if action == "list":
            rendered = pad.render_notes()
            return ToolEnvelope.success(
                content=rendered or "(no notes recorded for this session)",
                metadata={"action": "list"},
            )

        if action != "add":
            return ToolEnvelope.fail(
                code="KALASH_TOOL_INVALID_ARGS",
                message=f"Unknown action {args.action!r}; expected 'add' or 'list'.",
                recoverable=True,
            )

        text = args.text.strip()
        if not text:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_INVALID_ARGS",
                message="'text' is required when action is 'add'.",
                recoverable=True,
            )

        if len(text) > MAX_NOTE_CHARS:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_INVALID_ARGS",
                message=(
                    f"Note is {len(text)} chars; the limit is {MAX_NOTE_CHARS}. "
                    "Notes are rendered in full on every turn."
                ),
                recoverable=True,
                remediation="Shorten the note, or write the detail to a file instead.",
            )

        result = pad.note(text)
        verb = "Already recorded as" if result.deduped else "Recorded as"
        return ToolEnvelope.success(
            content=f"{verb} {result.ref}",
            metadata={
                "action": "add",
                "ref": result.ref,
                "deduped": result.deduped,
            },
        )
