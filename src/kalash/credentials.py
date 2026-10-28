"""Provider API keys in ``~/.kalash/auth.json`` and the last selected model.

Values are AES-GCM encrypted with a random key kept in a separate 0600 ``auth.key``.
This protects a copied ``auth.json`` alone; any process running as the same user can
read both files. Environment variables always take precedence over stored keys.
Legacy ``enc1:`` values (machine-id keystream) and plaintext values are still read and
are re-encrypted on the next save.
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

from kalash.paths import kalash_home
from kalash.secure_io import atomic_private_write, private_lock

_ENC_PREFIX = "enc2:"
_LEGACY_PREFIX = "enc1:"


class CredentialError(Exception):
    """Credential storage cannot be read or written safely."""


def _auth_path() -> Path:
    return kalash_home() / "auth.json"


def _key_path() -> Path:
    return kalash_home() / "auth.key"


def _file_key(*, create: bool) -> bytes:
    path = _key_path()
    if not path.exists():
        if not create:
            raise CredentialError("auth.key is missing; restore it or log in again")
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
            raise CredentialError(f"{path} must have permissions 0600")
        value = os.read(fd, 33)
    finally:
        os.close(fd)
    if len(value) != 32:
        raise CredentialError("auth.key is invalid; restore it or log in again")
    return value


def _encrypt(plaintext: str, provider_id: str) -> str:
    nonce = os.urandom(12)
    cipher = AESGCM(_file_key(create=True)).encrypt(nonce, plaintext.encode(), provider_id.encode())
    return _ENC_PREFIX + urlsafe_b64encode(nonce + cipher).decode("ascii")


def _legacy_decrypt(value: str) -> str:
    machine_id = Path("/etc/machine-id")
    seed = machine_id.read_text("utf-8").strip() if machine_id.exists() else str(Path.home())
    key = hashlib.sha256(f"kalash-auth-v1:{seed}".encode()).digest()
    raw = urlsafe_b64decode(value[len(_LEGACY_PREFIX) :].encode("ascii"))
    if len(raw) < 17:
        raise CredentialError("legacy credential is truncated")
    nonce, cipher = raw[:16], raw[16:]
    stream = bytearray()
    counter = 0
    while len(stream) < len(cipher):
        stream.extend(hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(a ^ b for a, b in zip(cipher, stream, strict=False)).decode("utf-8")


def _decrypt(value: str, provider_id: str) -> str:
    try:
        if value.startswith(_ENC_PREFIX):
            raw = urlsafe_b64decode(value[len(_ENC_PREFIX) :])
            return (
                AESGCM(_file_key(create=False))
                .decrypt(raw[:12], raw[12:], provider_id.encode())
                .decode()
            )
        if value.startswith(_LEGACY_PREFIX):
            return _legacy_decrypt(value)
    except (InvalidTag, ValueError, UnicodeDecodeError) as error:
        raise CredentialError(
            f"stored key for {provider_id!r} failed its integrity check"
        ) from error
    return value


def _read_raw() -> dict[str, str]:
    path = _auth_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CredentialError(f"cannot read {path}: {error}") from error
    if not isinstance(raw, dict) or not all(isinstance(v, str) for v in raw.values()):
        raise CredentialError(f"{path} must be a JSON object of strings")
    return {str(k): v for k, v in raw.items()}


def stored_providers() -> list[str]:
    """Provider ids with a stored key (without decrypting anything)."""
    try:
        return sorted(k for k, v in _read_raw().items() if v and ":" not in k)
    except CredentialError:
        return []


def get_credential(provider_id: str) -> str | None:
    """Decrypt one stored value. A corrupt entry for another provider does not matter."""
    value = _read_raw().get(provider_id)
    return _decrypt(value, provider_id) if value else None


def save_credential(provider_id: str, secret: str) -> None:
    """Store (or with an empty ``secret`` remove) one value, re-encrypting legacy entries."""
    with private_lock(kalash_home() / "auth.lock"):
        data: dict[str, str] = {}
        for key, value in _read_raw().items():
            try:
                plain = _decrypt(value, key)
            except CredentialError:
                data[key] = value  # keep undecryptable entries untouched rather than lose them
                continue
            data[key] = _encrypt(plain, key) if plain else ""
        if secret:
            data[provider_id] = _encrypt(secret, provider_id)
        else:
            data.pop(provider_id, None)
        atomic_private_write(_auth_path(), json.dumps(data, indent=2).encode())


def get_selected_model() -> str | None:
    """The ``provider/model`` the user last selected (``kalash model`` or the TUI picker)."""
    path = kalash_home() / "provider.json"
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    provider, model = data.get("provider"), data.get("model")
    if isinstance(provider, str) and isinstance(model, str) and provider and model:
        return f"{provider}/{model}"
    return None


def set_selected_model(provider: str, model: str) -> None:
    payload = json.dumps({"provider": provider, "model": model}, indent=2).encode()
    atomic_private_write(kalash_home() / "provider.json", payload)
