"""Settings: defaults < user < project < project-local < environment < command line.

* ``~/.kalash/settings.json`` (user) may set everything.
* ``<project>/.kalash/settings.json`` is committed and therefore untrusted: it may only
  set the model and behaviour keys in :data:`PROJECT_SAFE_KEYS`. Permission, sandbox,
  hook, MCP and provider endpoint keys there are ignored with a warning.
* ``<project>/.kalash/settings.local.json`` (not committed) may set everything.

Invalid values fail loudly with the file and key; unknown keys produce a warning.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from kalash.paths import kalash_home, project_root

SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]
ApprovalPolicy = Literal["ask", "never"]

PROJECT_SAFE_KEYS = frozenset(
    {
        "model",
        "provider",
        "max_output_tokens",
        "reasoning_effort",
        "temperature",
        "compaction",
        "instructions",
        "tui",
    }
)


class ConfigError(Exception):
    """Settings are invalid; the message names the source and key."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Permissions(_Strict):
    sandbox: SandboxMode = "workspace-write"
    approval: ApprovalPolicy = "ask"
    # fnmatch patterns over "<tool> <argument>", e.g. "bash git push*" or "write secrets/*"
    deny: list[str] = Field(default_factory=list)
    ask: list[str] = Field(default_factory=list)


class SandboxSettings(_Strict):
    network: bool = True
    writable_roots: list[str] = Field(default_factory=lambda: ["~/.cache", "~/.npm"])
    pass_env: list[str] = Field(default_factory=list)  # secret-looking variables to let through


class Compaction(_Strict):
    threshold: float = Field(default=0.8, gt=0.1, lt=1.0)
    retain: float = Field(default=0.16, gt=0.0, lt=0.9)


class HookSpec(_Strict):
    matcher: str | None = None
    command: str
    timeout: float = Field(default=60.0, gt=0)


class Hooks(_Strict):
    PreToolUse: list[HookSpec] = Field(default_factory=list)  # noqa: N815 - event names
    PostToolUse: list[HookSpec] = Field(default_factory=list)  # noqa: N815
    UserPromptSubmit: list[HookSpec] = Field(default_factory=list)  # noqa: N815
    Stop: list[HookSpec] = Field(default_factory=list)  # noqa: N815


class McpServer(_Strict):
    """One MCP server: a local process (``command``) or a remote endpoint (``url``)."""

    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None  # Streamable HTTP endpoint, e.g. https://mcp.deepwiki.com/mcp
    # Extra request headers for ``url`` servers. ``${VAR}`` is replaced from the environment when
    # connecting, so a key never has to be written into a settings file.
    headers: dict[str, str] = Field(default_factory=dict)
    trust: bool = False  # true: tool calls run without approval
    timeout: float = Field(default=60.0, gt=0)

    @model_validator(mode="after")
    def _one_transport(self) -> McpServer:
        if (self.command is None) == (self.url is None):
            raise ValueError("set exactly one of `command` (local server) or `url` (remote)")
        if self.url is not None:
            if self.args or self.env:
                raise ValueError("`args` and `env` only apply to a local `command` server")
            parts = urlsplit(self.url)
            loopback = parts.hostname in ("localhost", "127.0.0.1", "::1")
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError(f"`url` must be an http(s) URL, got {self.url!r}")
            if parts.scheme == "http" and not loopback:
                raise ValueError("a remote MCP `url` must use https (http is only for localhost)")
            if parts.username or parts.password:
                raise ValueError("put credentials in `headers` (with ${VAR}), not in the URL")
        elif self.headers:
            raise ValueError("`headers` only apply to a remote `url` server")
        return self

    @property
    def target(self) -> str:
        """Human-readable address of the server."""
        return self.url or " ".join([self.command or "", *self.args]).strip()


class Pricing(_Strict):
    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)
    cache_read_per_mtok: float | None = Field(default=None, ge=0)
    cache_write_per_mtok: float | None = Field(default=None, ge=0)


class Settings(_Strict):
    model: str | None = None  # "provider/model"
    max_output_tokens: int | None = Field(default=None, ge=256)
    context_window: int | None = Field(default=None, ge=4096)
    reasoning_effort: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    permissions: Permissions = Field(default_factory=Permissions)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    compaction: Compaction = Field(default_factory=Compaction)
    hooks: Hooks = Field(default_factory=Hooks)
    mcp_servers: dict[str, McpServer] = Field(default_factory=dict)
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    pricing: dict[str, Pricing] = Field(default_factory=dict)  # keyed by provider/model
    instructions: str | None = None  # extra system-prompt text
    stream_idle_timeout: float = Field(default=300.0, gt=0)
    tui: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _retired_caps(cls, value: Any) -> Any:
        # Old user settings and SDK callers must not silently reintroduce session caps.
        if isinstance(value, dict):
            return {k: v for k, v in value.items() if k not in ("budget", "limits")}
        return value

    @field_validator("reasoning_effort")
    @classmethod
    def _effort(cls, value: str | None) -> str | None:
        allowed = {"auto", "off", "none", "minimal", "low", "medium", "high", "xhigh", "max"}
        if value is not None and value not in allowed:
            raise ValueError(f"must be one of {sorted(allowed)}")
        return value


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise ConfigError(f"{path}: cannot read settings: {error}") from error
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: settings must be a JSON object")
    return data


def _normalize(raw: dict[str, Any], source: Path, warnings: list[str]) -> dict[str, Any]:
    """Map legacy keys onto the current schema and drop unknown ones with a warning."""
    data = dict(raw)
    provider = data.pop("provider", None)
    if provider and isinstance(data.get("model"), str) and "/" not in data["model"]:
        data["model"] = f"{provider}/{data['model']}"
    for retired in ("budget", "limits"):
        if retired in data:
            data.pop(retired)
            warnings.append(
                f"{source}: ignoring retired {retired!r}; sessions have no spending or step caps"
            )
    for key in list(data):
        if key not in Settings.model_fields:
            warnings.append(f"{source}: ignoring unknown setting {key!r}")
            data.pop(key)
    return data


def _merge(base: dict[str, Any], top: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in top.items():
        if (
            isinstance(value, dict)
            and isinstance(out.get(key), dict)
            and key not in ("mcp_servers", "providers", "pricing")
        ):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_settings(cwd: Path, overrides: dict[str, Any] | None = None) -> tuple[Settings, list[str]]:
    """Load effective settings for ``cwd``. Returns the settings and any warnings."""
    warnings: list[str] = []
    root = project_root(cwd)
    merged: dict[str, Any] = {}
    user_path = kalash_home() / "settings.json"
    merged = _merge(merged, _normalize(_read_json(user_path), user_path, warnings))
    project_path = root / ".kalash" / "settings.json"
    if project_path != user_path:
        project = _normalize(_read_json(project_path), project_path, warnings)
        for key in list(project):
            if key not in PROJECT_SAFE_KEYS:
                warnings.append(
                    f"{project_path}: {key!r} is ignored in committed project settings; "
                    f"put it in .kalash/settings.local.json or ~/.kalash/settings.json"
                )
                project.pop(key)
        merged = _merge(merged, project)
    local_path = root / ".kalash" / "settings.local.json"
    merged = _merge(merged, _normalize(_read_json(local_path), local_path, warnings))
    env_model = os.environ.get("KALASH_MODEL")
    if env_model:
        merged["model"] = env_model
    if overrides:
        merged = _merge(merged, {k: v for k, v in overrides.items() if v is not None})
    try:
        return Settings.model_validate(merged), warnings
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors()
        )
        raise ConfigError(f"invalid settings: {details}") from error


def settings_path(scope: Literal["user", "project", "local"], cwd: Path) -> Path:
    root = project_root(cwd)
    if scope == "user":
        return kalash_home() / "settings.json"
    if scope == "project":
        return root / ".kalash" / "settings.json"
    return root / ".kalash" / "settings.local.json"


def remove_setting(path: Path, dotted_key: str) -> bool:
    """Delete one key from a settings file. Returns False when it was not there."""
    import json as _json

    from kalash.secure_io import atomic_write

    if not path.exists():
        return False
    try:
        data = _json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as error:
        raise ConfigError(f"{path}: cannot read settings: {error}") from error
    node = data
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return False
    if not isinstance(node, dict) or parts[-1] not in node:
        return False
    del node[parts[-1]]
    atomic_write(path, (_json.dumps(data, indent=2) + "\n").encode())
    return True


def update_setting(path: Path, dotted_key: str, value: Any) -> None:
    """Set one key in a settings file (created if needed), keeping the other keys."""
    import json as _json

    from kalash.secure_io import atomic_write

    try:
        data = _json.loads(path.read_text("utf-8")) if path.exists() else {}
    except (OSError, ValueError) as error:
        raise ConfigError(f"{path}: cannot read settings: {error}") from error
    node = data
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = node[part] = {}
        node = child
    node[parts[-1]] = value
    atomic_write(path, (_json.dumps(data, indent=2) + "\n").encode())
