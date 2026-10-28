"""File checkpoints: restore what the agent's write/edit tools changed.

Before a write or edit the file's previous content is stored (content-addressed, per session);
after it succeeds a ``file_change`` event is logged. ``undo_last_turn`` restores the files
changed by the latest turn that still has un-reverted changes - but only files that still match
what the agent left behind, so your own later edits are never overwritten.

Limits, stated plainly: changes made by ``bash`` commands, subagents' own sessions and anything
outside this session are NOT tracked, and files over 5 MiB are skipped. This is not a
substitute for git.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from kalash.paths import kalash_home
from kalash.secure_io import atomic_write
from kalash.session.log import SessionLog

MAX_BYTES = 5 * 1024 * 1024


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class Snapshot:
    path: Path
    existed: bool
    before_sha: str | None = None
    too_large: bool = False


@dataclass
class UndoReport:
    restored: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)
    call_ids: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.restored or self.removed)


class CheckpointStore:
    def __init__(self, session_id: str, base: Path | None = None) -> None:
        self.directory = (base or kalash_home() / "checkpoints") / session_id

    def _blob(self, sha: str) -> Path:
        return self.directory / f"{sha}.blob"

    def capture(self, path: Path) -> Snapshot:
        """Store the file's current content (call before the tool runs)."""
        try:
            if not path.is_file():
                return Snapshot(path, existed=False)
            data = path.read_bytes()
        except OSError:
            return Snapshot(path, existed=False)
        if len(data) > MAX_BYTES:
            return Snapshot(path, existed=True, too_large=True)
        sha = _sha(data)
        blob = self._blob(sha)
        if not blob.exists():
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write(blob, data, mode=0o600)
        return Snapshot(path, existed=True, before_sha=sha)

    def record(self, log: SessionLog, call_id: str, tool: str, snapshots: list[Snapshot]) -> None:
        """Log one ``file_change`` per snapshot, with the content left behind (call after)."""
        for snap in snapshots:
            try:
                after = _sha(snap.path.read_bytes()) if snap.path.is_file() else None
            except OSError:
                after = None
            log.append(
                "file_change",
                call_id=call_id,
                tool=tool,
                path=str(snap.path),
                existed=snap.existed,
                before=snap.before_sha,
                too_large=snap.too_large,
                after=after,
            )

    def undo_last_turn(self, log: SessionLog) -> UndoReport:
        """Revert the most recent turn that still has un-reverted tracked changes."""
        undone = {c for e in log.events if e.type == "undo" for c in e.data.get("call_ids", [])}
        turn_start_seq = -1
        pending: list[tuple[int, dict[str, object]]] = []
        for event in reversed(log.events):
            if event.type == "file_change" and event.data["call_id"] not in undone:
                if not pending:
                    turn_start_seq = self._turn_start(log, event.seq)
                if event.seq > turn_start_seq:
                    pending.append((event.seq, event.data))
            elif event.type == "turn_start" and pending and event.seq <= turn_start_seq:
                break
        report = UndoReport()
        if not pending:
            return report
        # newest first; a path changed twice in a turn is restored to its earliest content
        blocked: set[Path] = set()
        for _seq, data in pending:
            path = Path(str(data["path"]))
            call_id = str(data["call_id"])
            report.call_ids.append(call_id)
            if path in blocked:
                continue
            if data.get("too_large"):
                report.skipped.append((str(path), "too large to have been checkpointed"))
                blocked.add(path)
                continue
            try:
                current = _sha(path.read_bytes()) if path.is_file() else None
                if current != data.get("after"):
                    report.skipped.append((str(path), "changed since the agent edited it"))
                    # An earlier snapshot can coincidentally match the user's content.
                    # Once a file conflicts, do not roll back any older change to it.
                    blocked.add(path)
                    continue
                if data.get("existed"):
                    blob = self._blob(str(data["before"]))
                    atomic_write(path, blob.read_bytes())
                    report.restored.append(str(path))
                else:
                    path.unlink(missing_ok=True)
                    report.removed.append(str(path))
            except OSError as error:
                report.skipped.append((str(path), f"could not restore: {error}"))
                blocked.add(path)
        log.append(
            "undo",
            call_ids=sorted(set(report.call_ids)),
            restored=report.restored,
            removed=report.removed,
            skipped=[p for p, _ in report.skipped],
        )
        return report

    @staticmethod
    def _turn_start(log: SessionLog, seq: int) -> int:
        start = -1
        for event in log.events:
            if event.seq > seq:
                break
            if event.type == "turn_start":
                start = event.seq
        return start

    def changed_files(self, log: SessionLog) -> list[str]:
        """Files the agent changed this session (and has not undone), oldest first."""
        undone = {c for e in log.events if e.type == "undo" for c in e.data.get("call_ids", [])}
        seen: dict[str, None] = {}
        for event in log.events:
            if event.type == "file_change" and event.data["call_id"] not in undone:
                seen.setdefault(str(event.data["path"]), None)
        return list(seen)
