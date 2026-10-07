"""Credential storage for providers — saved to ~/.kalash/auth.json.

AES-GCM authenticates credentials with a random secret key in a separate 0600
file. Copying auth.json alone does not disclose keys. A process able to read both
files (including the same OS user) can decrypt them. Legacy values migrate on save.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from base64 import urlsafe_b64decode, urlsafe_b64encode
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from kalash.core.paths import kalash_home
from kalash.core.secure_io import atomic_private_write, private_lock

_ENC_PREFIX = "enc2:"


def _auth_path() -> Path:
    return kalash_home() / "auth.json"


def _legacy_key() -> bytes:
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


def _derive_key(*, create: bool = False) -> bytes:
    path = kalash_home() / "auth.key"
    if not path.exists():
        if not create:
            raise ValueError("Credential key is missing; restore auth.key or reconnect")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(
            path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        try:
            os.write(fd, AESGCM.generate_key(bit_length=256))
            os.fsync(fd)
        finally:
            os.close(fd)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if os.name == "posix" and os.fstat(fd).st_mode & 0o077:
            raise ValueError("Credential key permissions must be 0600")
        value = os.read(fd, 33)
    finally:
        os.close(fd)
    if len(value) != 32:
        raise ValueError("Credential key is invalid; restore it or reconnect")
    return value


def _encrypt(plaintext: str, provider_id: str = "") -> str:
    if not plaintext:
        return ""
    key = _derive_key(create=True)
    nonce = os.urandom(12)
    cipher = AESGCM(key).encrypt(nonce, plaintext.encode(), provider_id.encode())
    blob = urlsafe_b64encode(nonce + cipher).decode("ascii")
    return f"{_ENC_PREFIX}{blob}"


def _decrypt(value: str, provider_id: str = "") -> str:
    if value.startswith(_ENC_PREFIX):
        try:
            raw = urlsafe_b64decode(value[len(_ENC_PREFIX) :])
            return AESGCM(_derive_key()).decrypt(raw[:12], raw[12:], provider_id.encode()).decode()
        except (InvalidTag, ValueError, OSError) as error:
            raise ValueError(
                "Credential integrity check failed; restore storage or reconnect"
            ) from error
    if not value or not value.startswith("enc1:"):
        return value
    key = _legacy_key()
    try:
        raw = urlsafe_b64decode(value[len("enc1:") :].encode("ascii"))
    except (ValueError, OSError) as error:
        raise ValueError("Legacy credential is malformed; restore it before migrating") from error
    if len(raw) < 17:
        raise ValueError("Legacy credential is truncated; restore it before migrating")
    nonce, cipher = raw[:16], raw[16:]
    stream = _keystream(key, nonce, len(cipher))
    plain = bytes(a ^ b for a, b in zip(cipher, stream, strict=True))
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(
            "Legacy credential cannot be decoded; restore it before migrating"
        ) from error


_auth_cache: dict[str, str] | None = None
_auth_cache_mtime: tuple[int, ...] = ()
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
        _auth_cache_mtime = ()
        return {}

    current_mtime: tuple[int, ...]
    try:
        auth_stat = path.stat()
        key_path = kalash_home() / "auth.key"
        key_stat = key_path.stat() if key_path.exists() else None
        current_mtime = (
            auth_stat.st_mtime_ns,
            auth_stat.st_size,
            auth_stat.st_ino,
            key_stat.st_mtime_ns if key_stat else 0,
            key_stat.st_mode if key_stat else 0,
        )
    except OSError:
        current_mtime = ()

    if _auth_cache is not None and path == _auth_cache_path and current_mtime == _auth_cache_mtime:
        return dict(_auth_cache)  # return a copy

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise ValueError("Cannot read credential storage; repair it before reconnecting") from error
    if not isinstance(raw, dict) or any(not isinstance(v, str) for v in raw.values()):
        raise ValueError("Credential storage must be an object of encrypted strings")

    result = {str(k): _decrypt(str(v), str(k)) for k, v in raw.items()}
    _auth_cache_path = path
    _auth_cache = result
    _auth_cache_mtime = current_mtime
    return dict(result)


def _invalidate_auth_cache() -> None:
    """Reset the credential cache after a write."""
    global _auth_cache, _auth_cache_mtime, _auth_cache_path
    _auth_cache_path = None
    _auth_cache = None
    _auth_cache_mtime = ()


def save_credential(provider_id: str, api_key: str) -> None:
    with private_lock(kalash_home() / "auth.lock"):
        _save_credential(provider_id, api_key)


def _save_credential(provider_id: str, api_key: str) -> None:
    """Save a credential for a provider."""
    _invalidate_auth_cache()
    path = _auth_path()
    data: dict[str, str] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict) or any(
                not isinstance(v, str) for v in existing.values()
            ):
                raise ValueError("Credential storage is malformed; refusing to replace it")
            data = {str(k): _encrypt(_decrypt(v, str(k)), str(k)) for k, v in existing.items()}
        except (json.JSONDecodeError, OSError) as error:
            raise ValueError("Credential storage is unreadable; refusing to replace it") from error

    if api_key:
        data[provider_id] = _encrypt(api_key, provider_id)
    else:
        data.pop(provider_id, None)

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_private_write(path, json.dumps(data, indent=2).encode())


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
    atomic_private_write(config_path, json.dumps(data, indent=2).encode())
