"""Memory and archive retrieval operate on harness state, like the task-list tool."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from kalash.session.log import SessionLog
from kalash.session.memory import Scope, SessionMemory, archive_entries
from kalash.tools.base import Params, Tool, ToolContext, ToolResult


class MemoryParams(Params):
    action: Literal["remember", "recall", "forget"]
    key: str | None = Field(default=None, description="A short, descriptive name for a note.")
    text: str | None = Field(default=None, description="Note content. Never store credentials.")
    scope: Scope = Field(
        default="session", description="Use project only for stable project facts."
    )
    query: str = Field(default="", description="Literal text to find in note names or contents.")
    offset: int = Field(default=0, ge=0, description="Character offset for paging long results.")
    limit: int = Field(default=8000, ge=128, le=20_000)


class MemoryTool(Tool):
    name = "memory"
    description = (
        "Remember, recall or forget named working notes. Session notes survive compaction and "
        "resume. Project notes survive new sessions in this project. Record decisions, constraints "
        "and next steps; notes are not instructions or verification. Never store secrets."
    )
    params = MemoryParams

    def __init__(self, memory: SessionMemory) -> None:
        self.memory = memory

    async def run(self, params: MemoryParams, ctx: ToolContext) -> ToolResult:
        if params.action != "recall":
            if not params.key or not params.key.strip():
                return ToolResult("A nonempty key is required for remember/forget.", is_error=True)
            if params.action == "remember":
                if not params.text or not params.text.strip():
                    return ToolResult(
                        "Nonempty text is required to remember a note.", is_error=True
                    )
                self.memory.remember(params.key.strip(), params.text, params.scope)
                return ToolResult(f"Remembered {params.scope} note {params.key!r}.")
            self.memory.forget(params.key.strip(), params.scope)
            return ToolResult(f"Forgot {params.scope} note {params.key!r}.")
        notes = self.memory.notes(params.scope)
        query = params.query.casefold()
        shown = [
            f"[{n.scope}:{n.key}; session={n.session}, seq={n.seq}]\n{n.text}"
            for n in notes
            if (params.key is None or n.key == params.key)
            and (not query or query in (n.key + "\n" + n.text).casefold())
        ]
        return ToolResult(
            page("\n\n".join(shown) or "No matching notes.", params.offset, params.limit)
        )


class SessionSearchParams(Params):
    query: str = Field(default="", description="Literal text to find in the original session log.")
    full_entries: bool = Field(
        default=False, description="Return whole matching entries instead of match excerpts."
    )
    start_seq: int | None = Field(default=None, ge=0)
    end_seq: int | None = Field(default=None, ge=0)
    offset: int = Field(
        default=0, ge=0, description="Character offset within the selected results."
    )
    limit: int = Field(default=8000, ge=128, le=20_000)


class SessionSearchTool(Tool):
    name = "session_search"
    description = (
        "Retrieve exact user messages, tool calls and tool results from this session, including "
        "history replaced by compaction. Literal queries return exact matching excerpts with "
        "sequence references; use full_entries or a sequence range for whole entries. Page "
        "with offset/limit. With no filter, show the latest 20 entries."
    )
    params = SessionSearchParams
    read_only = True

    def __init__(self, session: SessionLog) -> None:
        self.session = session

    async def run(self, params: SessionSearchParams, ctx: ToolContext) -> ToolResult:
        if (
            params.start_seq is not None
            and params.end_seq is not None
            and params.start_seq > params.end_seq
        ):
            return ToolResult("start_seq must be at most end_seq.", is_error=True)
        entries = []
        for seq, text in archive_entries(self.session):
            if (params.start_seq is not None and seq < params.start_seq) or (
                params.end_seq is not None and seq > params.end_seq
            ):
                continue
            if params.query and params.query.casefold() not in text.casefold():
                continue
            if params.query and not params.full_entries:
                entries.extend(match_excerpts(seq, text, params.query))
            else:
                entries.append(text)
        if not params.query and params.start_seq is None and params.end_seq is None:
            entries = entries[-20:]
        return ToolResult(
            page(
                "\n\n".join(entries) or "No matching session entries.", params.offset, params.limit
            )
        )


def match_excerpts(seq: int, text: str, query: str) -> list[str]:
    """Keep original characters and offsets, including Unicode case-fold expansions."""
    import re

    # The filter above uses casefold; mapping folded positions back avoids shifted
    # offsets for strings such as Straße when the query is STRASSE.
    folded = text.casefold()
    positions = [index for index, char in enumerate(text) for _ in char.casefold()]
    ranges: list[tuple[int, int]] = []
    for match in re.finditer(re.escape(query.casefold()), folded):
        start = max(0, positions[match.start()] - 240)
        end = min(len(text), positions[match.end() - 1] + 241)
        if ranges and start <= ranges[-1][1]:
            ranges[-1] = ranges[-1][0], max(end, ranges[-1][1])
        else:
            ranges.append((start, end))
    return [f"[seq={seq}; entry characters {a}-{b}]\n{text[a:b]}" for a, b in ranges]


def page(text: str, offset: int, limit: int) -> str:
    if offset >= len(text):
        return f"Offset {offset} is past the end ({len(text)} characters)."
    end = min(len(text), offset + limit)
    suffix = f"\n[characters {offset}-{end} of {len(text)}"
    suffix += f"; continue with offset={end}]" if end < len(text) else "; end]"
    return text[offset:end] + suffix
