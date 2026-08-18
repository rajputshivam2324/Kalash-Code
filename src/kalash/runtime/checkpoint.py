"""Snapshot and rewind for conversation + working tree.

Checkpoints pair a message sequence number with a content-addressed
file manifest. Rewind restores both conversation state and (optionally)
the working tree.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kalash.core.ids import generate_id
from kalash.core.paths import kalash_home
from kalash.storage.blobs import compute_digest, read_blob, store_blob

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# File manifest entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """A single file in a checkpoint manifest."""

    path: str  # Relative to workspace root
    digest: str  # SHA-256 blob reference
    mode: int  # File permissions
    size: int  # File size in bytes


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------


@dataclass
class Checkpoint:
    """A snapshot of conversation position + workspace state."""

    id: str
    session_id: str
    message_seq: int  # Conversation position
    manifest: list[ManifestEntry]  # Content-addressed file manifest
    created_at: str  # ISO timestamp
    label: str = ""  # Optional human-readable label
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def manifest_digest(self) -> str:
        """Content-address of the entire manifest."""
        data = json.dumps(
            [{"path": e.path, "digest": e.digest, "mode": e.mode, "size": e.size}
             for e in self.manifest],
            sort_keys=True,
        ).encode()
        return compute_digest(data)


# ---------------------------------------------------------------------------
# Checkpoint manager
# ---------------------------------------------------------------------------


@dataclass
class CheckpointManager:
    """Creates and restores checkpoints.

    Checkpoints are content-addressed snapshots that pair:
    - A message sequence number (conversation position)
    - A file manifest (workspace state as blob references)

    Auto-checkpointing occurs before destructive git operations.
    """

    session_id: str
    workspace_root: Path

    # Internal storage
    _checkpoints: list[Checkpoint] = field(init=False, default_factory=list)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create(
        self,
        message_seq: int,
        *,
        label: str = "",
        paths: list[Path] | None = None,
    ) -> Checkpoint:
        """Create a checkpoint at the given message sequence.

        Args:
            message_seq: Current conversation position.
            label: Human-readable label for the checkpoint.
            paths: Specific files to snapshot. If None, snapshots
                   all tracked files in the workspace.

        Returns:
            The created Checkpoint.
        """
        from datetime import datetime, timezone

        files_to_snapshot = paths or self._get_tracked_files()
        manifest = await self._build_manifest(files_to_snapshot)

        checkpoint = Checkpoint(
            id=generate_id("chk_"),
            session_id=self.session_id,
            message_seq=message_seq,
            manifest=manifest,
            created_at=datetime.now(timezone.utc).isoformat(),
            label=label,
        )

        self._checkpoints.append(checkpoint)
        await self._persist_checkpoint(checkpoint)

        logger.info(
            "Checkpoint created: %s (seq=%d, files=%d)",
            checkpoint.id,
            message_seq,
            len(manifest),
        )

        return checkpoint

    async def rewind(
        self,
        checkpoint_id: str,
        *,
        restore_files: bool = True,
        restore_conversation: bool = True,
    ) -> Checkpoint:
        """Rewind to a checkpoint.

        Args:
            checkpoint_id: The checkpoint to restore.
            restore_files: Whether to restore workspace files.
            restore_conversation: Whether to truncate conversation.

        Returns:
            The restored Checkpoint.

        Raises:
            ValueError: If checkpoint not found.
        """
        checkpoint = self._find_checkpoint(checkpoint_id)
        if checkpoint is None:
            msg = f"Checkpoint not found: {checkpoint_id}"
            raise ValueError(msg)

        if restore_files:
            await self._restore_files(checkpoint)

        logger.info(
            "Rewound to checkpoint %s (seq=%d)",
            checkpoint.id,
            checkpoint.message_seq,
        )

        return checkpoint

    async def auto_checkpoint_before_destructive(
        self, message_seq: int, operation: str
    ) -> Checkpoint:
        """Auto-create a checkpoint before a destructive git operation.

        Args:
            message_seq: Current conversation position.
            operation: Description of the destructive operation.
        """
        return await self.create(
            message_seq,
            label=f"auto: before {operation}",
        )

    def list_checkpoints(self) -> list[Checkpoint]:
        """List all checkpoints for this session, newest first."""
        return sorted(
            self._checkpoints,
            key=lambda c: c.created_at,
            reverse=True,
        )

    # ------------------------------------------------------------------
    # Manifest building
    # ------------------------------------------------------------------

    async def _build_manifest(self, paths: list[Path]) -> list[ManifestEntry]:
        """Build a manifest by storing file contents as blobs."""
        manifest: list[ManifestEntry] = []

        for path in paths:
            abs_path = self.workspace_root / path if not path.is_absolute() else path
            if not abs_path.is_file():
                continue

            try:
                data = abs_path.read_bytes()
                digest = store_blob(data)
                stat = abs_path.stat()

                rel_path = str(abs_path.relative_to(self.workspace_root))
                manifest.append(ManifestEntry(
                    path=rel_path,
                    digest=digest,
                    mode=stat.st_mode,
                    size=len(data),
                ))
            except (OSError, PermissionError) as exc:
                logger.warning("Cannot snapshot %s: %s", path, exc)

        return manifest

    # ------------------------------------------------------------------
    # Restoration
    # ------------------------------------------------------------------

    async def _restore_files(self, checkpoint: Checkpoint) -> None:
        """Restore workspace files from a checkpoint's manifest."""
        for entry in checkpoint.manifest:
            target = self.workspace_root / entry.path
            try:
                data = read_blob(entry.digest)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                os.chmod(target, entry.mode & 0o777)
            except (FileNotFoundError, ValueError) as exc:
                logger.warning("Cannot restore %s: %s", entry.path, exc)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _persist_checkpoint(self, checkpoint: Checkpoint) -> None:
        """Persist checkpoint metadata to the checkpoint store."""
        store_dir = kalash_home() / "checkpoints" / self.session_id
        store_dir.mkdir(parents=True, exist_ok=True)

        meta = {
            "id": checkpoint.id,
            "session_id": checkpoint.session_id,
            "message_seq": checkpoint.message_seq,
            "manifest_digest": checkpoint.manifest_digest,
            "manifest_count": len(checkpoint.manifest),
            "created_at": checkpoint.created_at,
            "label": checkpoint.label,
        }

        meta_path = store_dir / f"{checkpoint.id}.json"
        meta_path.write_text(json.dumps(meta, indent=2))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        """Find a checkpoint by ID."""
        for cp in self._checkpoints:
            if cp.id == checkpoint_id:
                return cp
        return None

    def _get_tracked_files(self) -> list[Path]:
        """Get git-tracked files in the workspace."""
        try:
            result = subprocess.run(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return [
                    Path(f) for f in result.stdout.strip().split("\n")
                    if f.strip()
                ]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Fallback: walk workspace (exclude hidden dirs and common ignores)
        files: list[Path] = []
        ignore_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv"}
        for root, dirs, filenames in os.walk(self.workspace_root):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for f in filenames:
                files.append(Path(root) / f)
            if len(files) > 1000:
                break  # Safety limit
        return files
