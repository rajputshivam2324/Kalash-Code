"""Content-addressed blob store.

Files > 64 KiB are stored at ~/.kalash/blobs/<sha256[:2]>/<sha256>.
Verified on read (I-025).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from kalash.core.paths import blobs_dir

# Threshold for externalizing to blob store
BLOB_THRESHOLD = 64 * 1024  # 64 KiB


def compute_digest(data: bytes) -> str:
    """Compute SHA-256 hex digest of data."""
    return hashlib.sha256(data).hexdigest()


def blob_path(digest: str) -> Path:
    """Get the filesystem path for a blob by its digest."""
    base = blobs_dir()
    return base / digest[:2] / digest


def store_blob(data: bytes) -> str:
    """Store data as a content-addressed blob. Returns the digest."""
    digest = compute_digest(data)
    path = blob_path(digest)

    if path.exists():
        # Already stored (content-addressed dedup)
        return digest

    path.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write: temp file + rename (I-008)
    tmp_path = path.with_suffix(".tmp")
    try:
        tmp_path.write_bytes(data)
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return digest


def read_blob(digest: str) -> bytes:
    """Read a blob by digest, verifying integrity (I-025)."""
    path = blob_path(digest)

    if not path.exists():
        msg = f"Blob not found: {digest}"
        raise FileNotFoundError(msg)

    data = path.read_bytes()

    # Verify integrity
    actual_digest = compute_digest(data)
    if actual_digest != digest:
        msg = f"Blob integrity check failed: expected {digest}, got {actual_digest}"
        raise ValueError(msg)

    return data


def blob_exists(digest: str) -> bool:
    """Check if a blob exists."""
    return blob_path(digest).exists()


def delete_blob(digest: str) -> bool:
    """Delete a blob. Returns True if it existed."""
    path = blob_path(digest)
    if path.exists():
        path.unlink()
        return True
    return False


def should_externalize(data: bytes | str) -> bool:
    """Check if content should be stored as a blob."""
    if isinstance(data, str):
        return len(data.encode("utf-8")) > BLOB_THRESHOLD
    return len(data) > BLOB_THRESHOLD
