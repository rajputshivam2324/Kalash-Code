"""Append-only JSONL session log.

Every model-visible input (system prompt, user messages, assistant messages, tool
results, compaction checkpoints) is an event in the log, and model history is derived
from the log alone. Log-only events (turn boundaries, tool starts, failed attempts,
usage of side calls, todo state) support replay, accounting and crash repair.

One process owns a session at a time (an exclusive ``flock`` on the file).
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalash.llm.types import Block, Message, TextBlock, ToolCallBlock, Usage, tool_result
from kalash.paths import sessions_dir

FORMAT_VERSION = 1
# Events that carry provider usage/cost: model responses, side calls and compaction summaries.
BILLED_EVENTS = ("assistant", "usage", "compaction")

CHECKPOINT_PREAMBLE = (
    "This is an automatically generated checkpoint condensing an earlier span of the "
    "conversation to free up context. Treat it as established background and continue the "
    "task directly from the messages that follow, without acknowledging this checkpoint."
)
INTERRUPTED_RUNNING = (
    "Tool outcome unknown: the session was interrupted while this tool was running. "
    "Check the current state before relying on or repeating its effect."
)
INTERRUPTED_NOT_STARTED = "Not executed: the session was interrupted before this tool started."


class SessionError(Exception):
    """The session cannot be opened or written."""


@dataclass
class Event:
    type: str
    seq: int
    ts: float
    data: dict[str, Any]

    def to_json(self) -> str:
        payload = {"type": self.type, "seq": self.seq, "ts": self.ts, **self.data}
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@dataclass
class SessionInfo:
    id: str
    path: Path
    created_at: float
    updated_at: float
    cwd: str
    model: str
    title: str


def new_session_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def checkpoint_message(summary: str, blocks: list[str] | None = None) -> Message:
    """The summary that replaces compacted history, plus the context blocks (instructions,
    runtime snapshot, ...) that were in force, so compaction never silently drops them."""
    text = f"{CHECKPOINT_PREAMBLE}\n\n<compacted-summary>\n{summary.strip()}\n</compacted-summary>"
    content: list[Block] = [TextBlock(text)]
    content += [TextBlock(b, source="context") for b in blocks or []]
    return Message("user", content)


class SessionLog:
    """An open, owned session. Use :meth:`create` or :meth:`open`; call :meth:`close`."""

    def __init__(self, path: Path, events: list[Event], fd: int) -> None:
        self.path = path
        self.events = events
        self._fd = fd
        self._closed = False

    # -- lifecycle ---------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        root: Path,
        cwd: Path,
        model: str,
        *,
        session_id: str | None = None,
        parent: str | None = None,
    ) -> SessionLog:
        directory = sessions_dir(root)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        sid = session_id or new_session_id()
        path = directory / f"{sid}.jsonl"
        fd = _open_owned(path, create=True)
        log = cls(path, [], fd)
        header: dict[str, Any] = {
            "id": sid,
            "version": FORMAT_VERSION,
            "cwd": str(cwd),
            "root": str(root),
            "model": model,
        }
        if parent:
            header["parent"] = parent
        log.append("session", **header)
        return log

    @classmethod
    def open(cls, path: Path) -> SessionLog:
        if not path.exists():
            raise SessionError(f"session file not found: {path}")
        fd = _open_owned(path, create=False)
        try:
            events = _read_events(path, fd)
        except Exception:
            os.close(fd)
            raise
        if not events or events[0].type != "session":
            os.close(fd)
            raise SessionError(f"{path} is not a Kalash session log")
        version = events[0].data.get("version")
        if not isinstance(version, int) or version > FORMAT_VERSION:
            os.close(fd)
            raise SessionError(
                f"{path} uses session format {version!r}; this Kalash reads "
                f"up to {FORMAT_VERSION}. Upgrade Kalash to open it."
            )
        return cls(path, events, fd)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self._fd)

    # -- writing -----------------------------------------------------------------------

    def append(self, type_: str, **data: Any) -> Event:
        if self._closed:
            raise SessionError("session is closed")
        seq = self.events[-1].seq + 1 if self.events else 0
        event = Event(type_, seq, round(time.time(), 3), data)
        line = (event.to_json() + "\n").encode("utf-8")
        try:
            os.write(self._fd, line)
            os.fsync(self._fd)
        except OSError as error:
            raise SessionError(f"cannot write session log {self.path}: {error}") from error
        self.events.append(event)
        return event

    def append_message(self, message: Message, **extra: Any) -> Event:
        kind = {"user": "user", "assistant": "assistant", "tool": "tool_result"}[message.role]
        return self.append(kind, message=message.to_dict(), **extra)

    # -- reading -----------------------------------------------------------------------

    @property
    def id(self) -> str:
        return str(self.events[0].data["id"])

    @property
    def header(self) -> dict[str, Any]:
        return self.events[0].data

    def message_items(self) -> list[tuple[int, Message]]:
        """Model history with the seq of the event each message came from."""
        items: list[tuple[int, Message]] = []
        for event in self.events:
            if event.type in ("user", "assistant", "tool_result"):
                items.append((event.seq, Message.from_dict(event.data["message"])))
            elif event.type == "compaction":
                keep_from = int(event.data["keep_from"])
                kept = [(s, m) for s, m in items if s >= keep_from]
                blocks = [str(b) for b in event.data.get("blocks") or []]
                items = [
                    (event.seq, checkpoint_message(str(event.data["summary"]), blocks)),
                    *kept,
                ]
        return items

    def messages(self) -> list[Message]:
        """Model history derived from the log."""
        return [message for _, message in self.message_items()]

    def system_prompt(self) -> str | None:
        for event in reversed(self.events):
            if event.type == "system":
                return str(event.data["prompt"])
        return None

    def context_digests(self) -> dict[str, str | None]:
        """Digest of the last delivered version of each context block (None: delivered as gone)."""
        digests: dict[str, str | None] = {}
        for event in self.events:
            if event.type == "context":
                digests.update(event.data.get("digests", {}))
        return digests

    def effort(self) -> tuple[bool, str | None]:
        """(was an effort ever chosen in this session, the last choice). None = model default."""
        for event in reversed(self.events):
            if event.type == "effort":
                value = event.data.get("value")
                return True, str(value) if value is not None else None
        return False, None

    def model(self) -> str:
        for event in reversed(self.events):
            if event.type == "model":
                return str(event.data["model"])
        return str(self.header.get("model", ""))

    def todos(self) -> list[dict[str, str]]:
        for event in reversed(self.events):
            if event.type == "todo":
                return list(event.data.get("todos", []))
        return []

    def turn_count(self) -> int:
        return sum(1 for e in self.events if e.type == "turn_start")

    def usage_total(self) -> Usage:
        total = Usage(input_tokens=0, output_tokens=0)
        for event in self.events:
            if event.type in BILLED_EVENTS and event.data.get("usage"):
                usage = Usage.from_dict(event.data["usage"])
                total.input_tokens = (total.input_tokens or 0) + (usage.input_tokens or 0)
                total.output_tokens = (total.output_tokens or 0) + (usage.output_tokens or 0)
                total.cache_read_tokens += usage.cache_read_tokens
                total.cache_write_tokens += usage.cache_write_tokens
                total.reasoning_tokens += usage.reasoning_tokens
        return total

    def cost_total(self) -> float | None:
        costs = [e.data.get("cost") for e in self.events if e.type in BILLED_EVENTS]
        known = [c for c in costs if isinstance(c, int | float)]
        return sum(known) if known else None

    def last_usage(self) -> Usage | None:
        """Usage of the most recent assistant response after the last compaction."""
        for event in reversed(self.events):
            if event.type == "compaction":
                return None
            if event.type == "assistant" and event.data.get("usage"):
                return Usage.from_dict(event.data["usage"])
        return None

    # -- repair ------------------------------------------------------------------------

    def close_open_calls(self) -> list[str]:
        """Give every tool call without a result its one result (``outcome unknown`` if it had
        started, ``not executed`` otherwise). Returns a description of each fix."""
        fixes: list[str] = []
        pending: dict[str, ToolCallBlock] = {}
        started: set[str] = set()
        for event in self.events:
            if event.type == "assistant":
                message = Message.from_dict(event.data["message"])
                pending = {c.id: c for c in message.tool_calls}
                started = set()
            elif event.type == "tool_start":
                started.add(str(event.data.get("call_id")))
            elif event.type == "tool_result":
                pending.pop(str(event.data["message"].get("tool_call_id")), None)
        for call_id, call in pending.items():
            running = call_id in started
            text = INTERRUPTED_RUNNING if running else INTERRUPTED_NOT_STARTED
            self.append_message(tool_result(call, text, is_error=True), repaired=True)
            fixes.append(f"closed tool call {call.name} ({'running' if running else 'not run'})")
        return fixes

    def repair(self) -> list[str]:
        """Close what an interrupted process left open: tool calls, then the open turn."""
        fixes = self.close_open_calls()
        open_turn: int | None = None
        for event in self.events:
            if event.type == "turn_start":
                open_turn = int(event.data.get("turn", 0))
            elif event.type == "turn_end":
                open_turn = None
        if open_turn is not None:
            self.append("turn_end", turn=open_turn, reason="interrupted")
            fixes.append(f"closed interrupted turn {open_turn}")
        return fixes


def _open_owned(path: Path, *, create: bool) -> int:
    flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise SessionError(f"session already exists: {path}") from error
    except OSError as error:
        raise SessionError(f"cannot open session {path}: {error}") from error
    if os.name == "posix":
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(fd)
            raise SessionError("session is in use by another Kalash process") from error
    return fd


def _read_events(path: Path, fd: int) -> list[Event]:
    raw = path.read_bytes()
    events: list[Event] = []
    good_length = 0
    offset = 0
    for line in raw.splitlines(keepends=True):
        offset += len(line)
        if not line.endswith(b"\n"):
            break  # torn final write: dropped below
        try:
            data = json.loads(line)
        except ValueError as error:
            raise SessionError(f"{path} is corrupt at byte {offset - len(line)}") from error
        events.append(Event(data.pop("type"), int(data.pop("seq")), float(data.pop("ts")), data))
        good_length = offset
    if good_length < len(raw):
        os.ftruncate(fd, good_length)  # drop a partially written last line
    return events


def read_info(path: Path) -> SessionInfo | None:
    """Cheap summary for listings (reads the file without taking ownership)."""
    try:
        with path.open("rb") as stream:
            first = json.loads(stream.readline())
            title = ""
            updated = float(first.get("ts", 0))
            model = str(first.get("model", ""))
            for line in stream:
                try:
                    data = json.loads(line)
                except ValueError:
                    break
                updated = float(data.get("ts", updated))
                if data.get("type") == "model":
                    model = str(data.get("model", model))
                if not title and data.get("type") == "user":
                    message = Message.from_dict(data["message"])
                    title = " ".join(message.visible_text.split())[:80]
    except (OSError, ValueError, KeyError):
        return None
    if first.get("type") != "session":
        return None
    return SessionInfo(
        str(first["id"]), path, float(first["ts"]), updated, str(first.get("cwd", "")), model, title
    )


def list_sessions(root: Path) -> list[SessionInfo]:
    directory = sessions_dir(root)
    if not directory.is_dir():
        return []
    infos = [info for p in directory.glob("*.jsonl") if (info := read_info(p)) is not None]
    return sorted(infos, key=lambda i: i.updated_at, reverse=True)


def find_session(root: Path, ident: str) -> Path:
    """Resolve a session id (or unique prefix) within this project, or a path."""
    candidate = Path(ident).expanduser()
    if candidate.suffix == ".jsonl" and candidate.exists():
        return candidate
    matches = [i.path for i in list_sessions(root) if i.id.startswith(ident)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SessionError(f"no session {ident!r} in this project (see `kalash sessions`)")
    raise SessionError(f"session id {ident!r} is ambiguous; give more characters")
