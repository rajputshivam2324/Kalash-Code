"""Layered configuration resolution.

Precedence (later wins):
1. Built-in defaults
2. ~/.kalash/settings.json (user)
3. .kalash/settings.json (project, committed)
4. .kalash/settings.local.json (project, gitignored)
5. Environment KALASH_*
6. CLI flags

Security-relevant keys are read ONLY from user scope.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalash.core.paths import kalash_home

# Security-relevant keys that only come from user scope
SECURITY_KEYS = frozenset(
    {
        "permissions.sandbox",
        "permissions.approval",
        "permissions.network",
        "network",
        "network.enabled",
        "network.allowed_hosts",
        "network.allow_private",
        "memory.egress",
        "memory.egress.mode",
        "memory.egress.artifacts",
        "capabilities",
        "sandbox.protected_paths",
    }
)


@dataclass
class ModelConfig:
    """Model configuration."""

    primary: str = "anthropic/claude-sonnet-4-5"
    fallback: list[str] = field(default_factory=lambda: ["openai/gpt-4o"])
    temperature: float | None = None
    max_output_tokens: int | None = None


@dataclass
class MemoryConfig:
    """Memory layer configuration."""

    enabled: bool = True
    primary: str = "local"
    providers: list[str] = field(default_factory=lambda: ["local"])
    read_policy: str = "primary_only"
    write_policy: str = "primary"
    recall_budget: float = 0.08
    provider_configs: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class PermissionsConfig:
    """Permissions configuration."""

    sandbox: str = "workspace-write"
    approval: str = "on-request"
    network: bool = False


@dataclass
class BudgetConfig:
    """Budget ceilings."""

    max_tokens: int = 500_000
    max_cost_usd: Decimal = field(default_factory=lambda: Decimal("2.00"))
    max_wallclock_s: int = 3600
    max_turns: int = 100
    max_tool_calls: int = 500
    max_spawn_depth: int = 3


@dataclass
class KalashConfig:
    """Complete Kalash configuration."""

    model: ModelConfig = field(default_factory=ModelConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    permissions: PermissionsConfig = field(default_factory=PermissionsConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    project_dir: Path = field(default_factory=Path.cwd)
    raw: dict[str, Any] = field(default_factory=dict)


def load_config(
    project_dir: Path | None = None, overrides: dict[str, Any] | None = None
) -> KalashConfig:
    """Load and merge configuration from all sources."""
    merged: dict[str, Any] = {}

    # Layer 1: built-in defaults (already in dataclass defaults)

    # Layer 2: user settings
    user_settings = kalash_home() / "settings.json"
    if user_settings.exists():
        _merge(merged, _load_json(user_settings))

    # Layer 3 & 4: project settings
    proj = project_dir or Path.cwd()
    project_settings = proj / ".kalash" / "settings.json"
    if project_settings.exists():
        project_data = _load_json(project_settings)
        # Filter out security keys from project scope
        _filter_security_keys(project_data, str(project_settings))
        _merge(merged, project_data)

    project_local = proj / ".kalash" / "settings.local.json"
    if project_local.exists():
        local_data = _load_json(project_local)
        _filter_security_keys(local_data, str(project_local))
        _merge(merged, local_data)

    # Layer 5: environment variables
    _apply_env(merged)

    # Layer 6: CLI overrides
    if overrides:
        _merge(merged, overrides)

    # Resolve env references ${env:VAR}
    _resolve_env_refs(merged)

    return _build_config(merged, proj)


def _load_json(path: Path) -> dict[str, Any]:
    """Load a JSON file, returning empty dict on error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    """Deep merge overlay into base."""
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _merge(base[key], value)
        else:
            base[key] = value


def _filter_security_keys(data: dict[str, Any], source: str) -> None:
    """Remove security-relevant keys from project-scope config."""
    import logging as _log

    _logger = _log.getLogger(__name__)
    for key in list(_flat_keys(data)):
        if key in SECURITY_KEYS:
            _logger.warning(
                "config_scope_ignored: key=%s source=%s reason=Security keys only from user scope",
                key,
                source,
            )
            _remove_nested(data, key.split("."))


def _flat_keys(data: dict[str, Any], prefix: str = "") -> list[str]:
    """Flatten nested dict to dot-separated keys."""
    keys = []
    for k, v in data.items():
        full = f"{prefix}.{k}" if prefix else k
        keys.append(full)
        if isinstance(v, dict):
            keys.extend(_flat_keys(v, full))
    return keys


def _remove_nested(data: dict[str, Any], parts: list[str]) -> None:
    """Remove a nested key by path parts."""
    if len(parts) == 1:
        data.pop(parts[0], None)
    elif parts[0] in data and isinstance(data[parts[0]], dict):
        _remove_nested(data[parts[0]], parts[1:])


def _apply_env(data: dict[str, Any]) -> None:
    """Apply KALASH_* environment variables."""
    prefix = "KALASH_"
    for key, value in os.environ.items():
        if key.startswith(prefix) and key != "KALASH_HOME":
            # KALASH_MODEL_PRIMARY -> model.primary
            config_key = key[len(prefix) :].lower().replace("__", ".")
            parts = config_key.split(".")
            target = data
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value


def _resolve_env_refs(data: dict[str, Any]) -> None:
    """Resolve ${env:VAR} references in string values."""
    pattern = re.compile(r"\$\{env:(\w+)\}")
    for key, value in list(data.items()):
        if isinstance(value, str):
            match = pattern.search(value)
            if match:
                env_var = match.group(1)
                env_val = os.environ.get(env_var, "")
                data[key] = pattern.sub(env_val, value)
        elif isinstance(value, dict):
            _resolve_env_refs(value)


def _build_config(data: dict[str, Any], project_dir: Path) -> KalashConfig:
    """Build a KalashConfig from merged dict."""
    model_data = data.get("model", {})
    if isinstance(model_data, str):
        model_data = {"primary": model_data}
    elif not isinstance(model_data, dict):
        model_data = {}

    memory_data = data.get("memory", {})
    if not isinstance(memory_data, dict):
        memory_data = {}

    permissions_data = data.get("permissions", {})
    if not isinstance(permissions_data, dict):
        permissions_data = {}

    budget_data = data.get("budget", {})
    if not isinstance(budget_data, dict):
        budget_data = {}

    model = ModelConfig(
        primary=model_data.get("primary", "anthropic/claude-sonnet-4-5"),
        fallback=model_data.get("fallback", ["openai/gpt-4o"]),
        temperature=model_data.get("temperature"),
        max_output_tokens=model_data.get("max_output_tokens"),
    )

    memory = MemoryConfig(
        enabled=memory_data.get("enabled", True),
        primary=memory_data.get("primary", "local"),
        providers=memory_data.get("providers", ["local"]),
        read_policy=memory_data.get("read_policy", "primary_only"),
        write_policy=memory_data.get("write_policy", "primary"),
        recall_budget=memory_data.get("recall_budget", 0.08),
        provider_configs=memory_data.get("provider_configs", {}),
    )

    permissions = PermissionsConfig(
        sandbox=permissions_data.get("sandbox", "workspace-write"),
        approval=permissions_data.get("approval", "on-request"),
        network=permissions_data.get("network", False),
    )

    budget = BudgetConfig(
        max_tokens=budget_data.get("max_tokens", 500_000),
        max_cost_usd=Decimal(str(budget_data.get("max_cost_usd", "2.00"))),
        max_wallclock_s=budget_data.get("max_wallclock_s", 3600),
        max_turns=budget_data.get("max_turns", 100),
        max_tool_calls=budget_data.get("max_tool_calls", 500),
        max_spawn_depth=budget_data.get("max_spawn_depth", 3),
    )

    return KalashConfig(
        model=model,
        memory=memory,
        permissions=permissions,
        budget=budget,
        project_dir=project_dir,
        raw=data,
    )
