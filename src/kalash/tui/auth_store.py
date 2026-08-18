"""Credential storage for providers — saved to ~/.kalash/auth.json.

Simple JSON file storing provider_id → api_key mapping.
"""

from __future__ import annotations

import json
from pathlib import Path

from kalash.core.paths import kalash_home


def _auth_path() -> Path:
    return kalash_home() / "auth.json"


def load_auth() -> dict[str, str]:
    """Load saved credentials."""
    path = _auth_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_credential(provider_id: str, api_key: str) -> None:
    """Save a credential for a provider."""
    data = load_auth()
    data[provider_id] = api_key
    path = _auth_path()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    path.chmod(0o600)  # owner-only read


def get_credential(provider_id: str) -> str | None:
    """Get a saved credential."""
    return load_auth().get(provider_id)


def remove_credential(provider_id: str) -> None:
    """Remove a saved credential."""
    data = load_auth()
    data.pop(provider_id, None)
    path = _auth_path()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


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
    data = {"provider": provider_id, "model": model}
    config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
