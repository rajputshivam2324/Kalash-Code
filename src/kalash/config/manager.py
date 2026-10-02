"""
This is being done by this file .  Simply in CLI app if i changes something , like i switch model to xyz , it changes it into the main config file.
User action
    ↓
"set model.primary to gpt-5.6"
    ↓
ConfigManager
    ↓
opens project settings
    ↓
changes model.primary
    ↓
saves settings
    ↓
Kalash later reads the new configuration
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kalash.core.config import load_config
from kalash.core.paths import settings_path


class ConfigManager:
    """Read and write Kalash configuration files."""

    def get_config_path(self, *, scope: str = "project") -> Path:
        """Return the settings file path for a scope."""
        if scope in ("global", "user"):
            return settings_path("user")
        return settings_path("project")

    def _load_scope_file(self, scope: str) -> dict[str, Any]:
        path = self.get_config_path(scope=scope)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_scope_file(self, scope: str, data: dict[str, Any]) -> None:
        path = self.get_config_path(scope=scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _get_nested(data: dict[str, Any], key: str) -> Any:
        current: Any = data
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    @staticmethod
    def _set_nested(data: dict[str, Any], key: str, value: str) -> None:
        parsed: Any
        lowered = value.strip().lower()
        if lowered in ("true", "false"):
            parsed = lowered == "true"
        else:
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = value

        current: dict[str, Any] = data
        parts = key.split(".")
        for part in parts[:-1]:
            nested = current.get(part)
            if not isinstance(nested, dict):
                nested = {}
                current[part] = nested
            current = nested
        current[parts[-1]] = parsed

    def get(self, key: str) -> Any:
        """Resolve a dot-separated key from the merged configuration."""
        config = load_config()
        value = self._get_nested(config.raw, key)
        if value is not None:
            return value
        # Fall back to dataclass fields for common keys.
        flat = {
            "model.primary": config.model.primary,
            "model.fallback": config.model.fallback,
            "permissions.sandbox": config.permissions.sandbox,
            "permissions.approval": config.permissions.approval,
            "permissions.network": config.permissions.network,
            "memory.enabled": config.memory.enabled,
            "memory.primary": config.memory.primary,
            "budget.max_tokens": config.budget.max_tokens,
        }
        return flat.get(key)

    def set(self, key: str, value: str, *, scope: str = "project") -> None:
        """Set a configuration value in a scope-specific settings file."""
        normalized = "user" if scope in ("global", "user") else "project"
        data = self._load_scope_file(normalized)
        self._set_nested(data, key, value)
        self._save_scope_file(normalized, data)

    def render(self, *, scope: str | None = None, format: str = "toml") -> str:
        """Render the resolved configuration."""
        if scope in ("global", "user"):
            data = self._load_scope_file("user")
        elif scope == "project":
            data = self._load_scope_file("project")
        else:
            data = load_config().raw

        if format == "json":
            return json.dumps(data, indent=2) + "\n"

        # Without a TOML dependency, emit JSON for yaml/toml too — still readable.
        return json.dumps(data, indent=2) + "\n"
