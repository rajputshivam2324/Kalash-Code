"""ULID generation for all primary keys.

ULIDs are sortable, collision-resistant identifiers.
They sort lexicographically by creation time.
"""

from __future__ import annotations

import os
import struct
import time


def generate_id(prefix: str = "") -> str:
    """Generate a ULID with optional prefix.

    Args:
        prefix: Optional prefix like 'mem_', 'ses_', 'led_', 'tmb_'.

    Returns:
        A ULID string, optionally prefixed.
    """
    # Timestamp: milliseconds since Unix epoch, 6 bytes
    timestamp_ms = int(time.time() * 1000)
    ts_bytes = struct.pack(">Q", timestamp_ms)[2:]  # 6 bytes

    # Randomness: 10 bytes
    random_bytes = os.urandom(10)

    ulid_bytes = ts_bytes + random_bytes
    encoded = _encode_base32(ulid_bytes)
    return f"{prefix}{encoded}" if prefix else encoded


# Crockford's Base32 encoding
_ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_base32(data: bytes) -> str:
    """Encode 16 bytes to 26-char Crockford Base32."""
    assert len(data) == 16
    # Convert to a 128-bit integer
    val = int.from_bytes(data, "big")
    chars = []
    for _ in range(26):
        chars.append(_ENCODING[val & 0x1F])
        val >>= 5
    return "".join(reversed(chars))


def timestamp_from_id(ulid: str) -> float:
    """Extract the Unix timestamp from a ULID (ignoring prefix)."""
    # Strip any prefix (find the 26-char ULID portion)
    raw = ulid
    if "_" in ulid:
        raw = ulid.split("_", 1)[1]

    # Decode first 10 chars (timestamp portion)
    _DECODING = {c: i for i, c in enumerate(_ENCODING)}
    val = 0
    for ch in raw[:10]:
        val = (val << 5) | _DECODING[ch.upper()]

    return val / 1000.0
