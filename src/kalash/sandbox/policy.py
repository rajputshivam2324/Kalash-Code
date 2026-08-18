"""Sandbox policy — writable roots, protected paths, network control.

Defines SandboxMode and resolves which filesystem paths are accessible.
Protected path checks implement I-010 (security-sensitive file protection).
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from kalash.core.paths import temp_dir_for_session


# ---------------------------------------------------------------------------
# Sandbox modes
# ---------------------------------------------------------------------------


class SandboxMode(StrEnum):
    """Sandbox enforcement level."""

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    DANGER_FULL_ACCESS = "danger_full_access"


# ---------------------------------------------------------------------------
# Network policy
# ---------------------------------------------------------------------------


class NetworkPolicy(StrEnum):
    """Network access policy."""

    DENY = "deny"  # No network access
    ALLOW_LISTED = "allow_listed"  # Only allowed hosts
    ALLOW_ALL = "allow_all"  # Unrestricted


# ---------------------------------------------------------------------------
# Protected paths (I-010)
# ---------------------------------------------------------------------------

# Paths that are always protected regardless of sandbox mode
DEFAULT_PROTECTED_PATHS: tuple[str, ...] = (
    ".git/config",
    ".git/hooks/*",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "~/.ssh/*",
    "~/.aws/*",
    "~/.gnupg/*",
    "~/.config/gcloud/*",
    "~/.docker/config.json",
    "~/.npmrc",
    "~/.pypirc",
    "**/credentials.json",
    "**/secrets.yaml",
    "**/secrets.yml",
    "**/*.pem",
    "**/*.key",
    "**/id_rsa",
    "**/id_ed25519",
)


# ---------------------------------------------------------------------------
# Sandbox policy
# ---------------------------------------------------------------------------


@dataclass
class SandboxPolicy:
    """Policy governing filesystem and network access.

    Resolves writable roots, checks path admission, and integrates
    network policy.
    """

    mode: SandboxMode = SandboxMode.WORKSPACE_WRITE
    workspace_root: Path = field(default_factory=Path.cwd)
    session_id: str = ""

    # Additional writable roots beyond workspace + session temp
    additional_writable: list[Path] = field(default_factory=list)

    # Network configuration
    network_policy: NetworkPolicy = NetworkPolicy.DENY
    allowed_hosts: list[str] = field(default_factory=list)

    # Custom protected path patterns (added to defaults)
    extra_protected: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Writable roots resolution
    # ------------------------------------------------------------------

    @property
    def writable_roots(self) -> list[Path]:
        """Resolve all writable root paths.

        Writable roots are:
        1. The workspace root (in WORKSPACE_WRITE or DANGER_FULL_ACCESS)
        2. Per-session temp directory
        3. Configured additional roots
        """
        if self.mode == SandboxMode.READ_ONLY:
            return []

        roots: list[Path] = []

        # Workspace root
        roots.append(self.workspace_root.resolve())

        # Per-session temp
        if self.session_id:
            roots.append(temp_dir_for_session(self.session_id))

        # Additional configured roots
        for p in self.additional_writable:
            resolved = p.expanduser().resolve()
            if resolved.exists():
                roots.append(resolved)

        return roots

    # ------------------------------------------------------------------
    # Path admission
    # ------------------------------------------------------------------

    def path_admitted(self, path: str | Path) -> bool:
        """Check if a path is admitted for writing.

        A path is admitted if:
        1. It is not in the protected paths list (I-010)
        2. It falls under a writable root

        In DANGER_FULL_ACCESS mode, all paths are writable
        (but protected paths still raise warnings).
        """
        target = Path(path).expanduser().resolve()

        # Protected path check always applies
        if self.is_protected(path):
            return False

        # Full access mode: everything non-protected is admitted
        if self.mode == SandboxMode.DANGER_FULL_ACCESS:
            return True

        # Read-only: nothing is writable
        if self.mode == SandboxMode.READ_ONLY:
            return False

        # Workspace write: must be under a writable root
        for root in self.writable_roots:
            try:
                target.relative_to(root)
                return True
            except ValueError:
                continue

        return False

    def is_protected(self, path: str | Path) -> bool:
        """Check if a path matches protected patterns (I-010).

        Protected paths include security-sensitive files that should
        never be modified by the agent.
        """
        path_str = str(Path(path).expanduser())
        home = str(Path.home())

        # Normalize ~ references for matching
        all_patterns = list(DEFAULT_PROTECTED_PATHS) + self.extra_protected

        for pattern in all_patterns:
            # Expand ~ in pattern
            expanded_pattern = pattern.replace("~", home)

            # Try both the path as-is and as relative to workspace
            if fnmatch.fnmatch(path_str, expanded_pattern):
                return True

            # Also check the basename/relative portion
            try:
                rel = str(Path(path).relative_to(self.workspace_root))
                if fnmatch.fnmatch(rel, pattern):
                    return True
            except (ValueError, TypeError):
                pass

        return False

    # ------------------------------------------------------------------
    # Network policy
    # ------------------------------------------------------------------

    def network_allowed(self, host: str | None = None) -> bool:
        """Check if network access is allowed, optionally to a specific host."""
        match self.network_policy:
            case NetworkPolicy.DENY:
                return False
            case NetworkPolicy.ALLOW_ALL:
                return True
            case NetworkPolicy.ALLOW_LISTED:
                if host is None:
                    return bool(self.allowed_hosts)
                return any(
                    fnmatch.fnmatch(host, pattern)
                    for pattern in self.allowed_hosts
                )
        return False

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_write(self, path: str | Path) -> tuple[bool, str]:
        """Validate a write operation. Returns (allowed, reason)."""
        if self.mode == SandboxMode.READ_ONLY:
            return False, "Sandbox is in read-only mode"

        if self.is_protected(path):
            return False, f"Path is protected (I-010): {path}"

        if not self.path_admitted(path):
            return False, f"Path is outside writable roots: {path}"

        return True, "Write permitted"

    def validate_read(self, path: str | Path) -> tuple[bool, str]:
        """Validate a read operation. Reads are always allowed but logged."""
        # Reads are permitted in all modes
        # Protected paths can be read but not written
        return True, "Read permitted"
