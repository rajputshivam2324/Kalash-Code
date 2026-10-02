"""Credential storage for providers — saved to ~/.kalash/auth.json.

Values are encrypted at rest with a machine-local key derived from
``/etc/machine-id`` (or the home directory as fallback). Legacy plaintext
entries are still readable and are re-encrypted on the next save.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from base64 import urlsafe_b64decode, urlsafe_b64encode
from pathlib import Path

from kalash.core.paths import kalash_home

_ENC_PREFIX = "enc1:"


def _auth_path() -> Path:
    return kalash_home() / "auth.json"


def _derive_key() -> bytes:
    machine_id_path = Path("/etc/machine-id")
    if machine_id_path.exists():
        seed = machine_id_path.read_text(encoding="utf-8").strip()
    else:
        seed = str(Path.home())
    return hashlib.sha256(f"kalash-auth-v1:{seed}".encode()).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def _encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    key = _derive_key()
    nonce = os.urandom(16)
    raw = plaintext.encode("utf-8")
    stream = _keystream(key, nonce, len(raw))
    cipher = bytes(a ^ b for a, b in zip(raw, stream, strict=True))
    blob = urlsafe_b64encode(nonce + cipher).decode("ascii")
    return f"{_ENC_PREFIX}{blob}"


def _decrypt(value: str) -> str:
    if not value or not value.startswith(_ENC_PREFIX):
        return value
    key = _derive_key()
    try:
        raw = urlsafe_b64decode(value[len(_ENC_PREFIX) :].encode("ascii"))
    except (ValueError, OSError):
        return ""
    if len(raw) < 17:
        return ""
    nonce, cipher = raw[:16], raw[16:]
    stream = _keystream(key, nonce, len(cipher))
    plain = bytes(a ^ b for a, b in zip(cipher, stream, strict=True))
    return plain.decode("utf-8", errors="replace")


_auth_cache: dict[str, str] | None = None
_auth_cache_mtime: float = 0.0
_auth_cache_path: Path | None = None


def load_auth() -> dict[str, str]:
    """Load saved credentials, with in-memory caching (P-1).

    The cache is invalidated when the file's mtime changes (i.e. after a
    ``save_credential`` call or external edit).
    """
    global _auth_cache, _auth_cache_mtime, _auth_cache_path

    path = _auth_path()
    if not path.exists():
        _auth_cache = {}
        _auth_cache_mtime = 0.0
        return {}

    try:
        current_mtime = path.stat().st_mtime
    except OSError:
        current_mtime = 0.0

    if _auth_cache is not None and path == _auth_cache_path and current_mtime == _auth_cache_mtime:
        return dict(_auth_cache)  # return a copy

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}

    result = {str(k): _decrypt(str(v)) for k, v in raw.items()}
    _auth_cache_path = path
    _auth_cache = result
    _auth_cache_mtime = current_mtime
    return dict(result)


def _invalidate_auth_cache() -> None:
    """Reset the credential cache after a write."""
    global _auth_cache, _auth_cache_mtime, _auth_cache_path
    _auth_cache_path = None
    _auth_cache = None
    _auth_cache_mtime = 0.0


def save_credential(provider_id: str, api_key: str) -> None:
    """Save a credential for a provider."""
    _invalidate_auth_cache()
    path = _auth_path()
    data: dict[str, str] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                data = {str(k): str(v) for k, v in existing.items()}
        except (json.JSONDecodeError, OSError):
            data = {}

    if api_key:
        data[provider_id] = _encrypt(api_key)
    else:
        data.pop(provider_id, None)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    path.chmod(0o600)


def get_credential(provider_id: str) -> str | None:
    """Get a saved credential."""
    value = load_auth().get(provider_id)
    return value or None


def remove_credential(provider_id: str) -> None:
    """Remove a saved credential."""
    save_credential(provider_id, "")


def get_active_provider() -> tuple[str, str] | None:
    """Get the currently active provider and model from config."""
    config_path = kalash_home() / "provider.json"
    if not config_path.exists():
        return None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return data.get("provider"), data.get("model")
    except (json.JSONDecodeError, OSError):
        return None


def set_active_provider(provider_id: str, model: str) -> None:
    """Set the active provider and model."""
    config_path = kalash_home() / "provider.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data = {"provider": provider_id, "model": model}
    config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    config_path.chmod(0o600)  # SEC-3: restrict like auth.json
