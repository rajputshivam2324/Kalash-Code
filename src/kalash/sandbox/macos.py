"""macOS sandbox enforcement — Seatbelt (sandbox-exec) profiles.

Generates sandbox-exec profiles for writable roots and network control.
Falls closed (I-011) if sandbox-exec is not available.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .policy import NetworkPolicy, SandboxMode, SandboxPolicy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Seatbelt profile generation
# ---------------------------------------------------------------------------


def _generate_seatbelt_profile(
    *,
    writable_roots: list[Path],
    allow_network: bool = False,
    allowed_hosts: list[str] | None = None,
    protected_paths: list[Path] | None = None,
) -> str:
    """Generate a macOS sandbox-exec (Seatbelt) profile.

    Args:
        writable_roots: Paths where write access is allowed.
        allow_network: Whether to allow network access.
        allowed_hosts: Specific hosts to allow (if network restricted).

    Returns:
        The Seatbelt profile as a string in SBPL (Scheme) format.
    """
    lines: list[str] = []
    lines.append("(version 1)")
    lines.append("")

    # Default: deny everything
    lines.append("(deny default)")
    lines.append("")

    # Allow basic process operations
    lines.append(";; Basic process operations")
    lines.append("(allow process-exec)")
    lines.append("(allow process-fork)")
    lines.append("(allow signal)")
    lines.append("(allow sysctl-read)")
    lines.append("(allow mach-lookup)")
    lines.append("(allow ipc-posix-shm-read-data)")
    lines.append("")

    # Allow reading everywhere (sandbox restricts writes, not reads)
    lines.append(";; Global read access")
    lines.append("(allow file-read*)")
    lines.append("")

    # Writable paths
    lines.append(";; Writable roots")
    for root in writable_roots:
        root_str = str(root.resolve())
        # Allow all file write operations under this tree
        lines.append(f"(allow file-write* (subpath {json.dumps(root_str)}))")

    # Allow writing to temp
    lines.append('(allow file-write* (subpath "/private/tmp"))')
    lines.append('(allow file-write* (subpath "/tmp"))')
    lines.append("")
    for path in protected_paths or []:
        literal = json.dumps(str(path))
        matcher = "subpath" if path.is_dir() else "literal"
        lines.append(f"(deny file-write* ({matcher} {literal}))")
        if ".git" not in path.parts:
            lines.append(f"(deny file-read-data (literal {literal}))")
    from kalash.core.paths import kalash_home

    for secret_dir in (
        Path.home() / ".ssh",
        Path.home() / ".aws",
        Path.home() / ".gnupg",
        Path.home() / ".config/gcloud",
    ):
        literal = json.dumps(str(secret_dir))
        lines.append(f"(deny file-read* (subpath {literal}))")
        lines.append(f"(deny file-write* (subpath {literal}))")
    for secret_file in (kalash_home() / "auth.json", kalash_home() / "auth.key"):
        literal = json.dumps(str(secret_file))
        lines.append(f"(deny file-read* (literal {literal}))")
        lines.append(f"(deny file-write* (literal {literal}))")

    # System library access (read-only is covered above)
    lines.append(";; System access")
    lines.append('(allow file-write* (subpath "/dev"))')
    lines.append("")

    # Network policy
    lines.append(";; Network policy")
    if allow_network and not allowed_hosts:
        lines.append("(allow network*)")
    elif allowed_hosts:
        # Generic subprocess confinement has no enforcing hostname proxy.
        # Preserve the allow-list boundary by denying subprocess networking.
        lines.append("(deny network*)")
    else:
        # Deny all network
        lines.append("(deny network*)")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# macOS sandbox
# ---------------------------------------------------------------------------


@dataclass
class MacOSSandbox:
    """macOS sandbox using Seatbelt (sandbox-exec).

    Generates and applies sandbox-exec profiles for:
    - Writable root enforcement
    - Network control
    - Falls closed if sandbox-exec not available (I-011)
    """

    policy: SandboxPolicy

    # Internal state
    _sandbox_exec_path: str | None = field(init=False, default=None)
    _profile_path: Path | None = field(init=False, default=None)
    _enforced: bool = field(init=False, default=False)

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    @property
    def sandbox_exec_available(self) -> bool:
        """Check if sandbox-exec is available on this system."""
        if self._sandbox_exec_path is not None:
            return True

        path = shutil.which("sandbox-exec")
        if path:
            self._sandbox_exec_path = path
            return True

        # macOS default location
        default = "/usr/bin/sandbox-exec"
        if Path(default).exists():
            self._sandbox_exec_path = default
            return True

        return False

    @property
    def can_enforce(self) -> bool:
        """Whether sandbox enforcement is possible."""
        if self.policy.mode == SandboxMode.DANGER_FULL_ACCESS:
            return True
        return self.sandbox_exec_available

    @property
    def is_enforced(self) -> bool:
        return self._enforced

    # ------------------------------------------------------------------
    # Profile management
    # ------------------------------------------------------------------

    def generate_profile(self) -> str:
        """Generate the Seatbelt profile for current policy."""
        allow_network = self.policy.network_policy == NetworkPolicy.ALLOW_ALL

        from .policy import protected_existing_paths

        return _generate_seatbelt_profile(
            writable_roots=self.policy.writable_roots,
            allow_network=allow_network,
            allowed_hosts=self.policy.allowed_hosts if not allow_network else None,
            protected_paths=protected_existing_paths(
                [self.policy.workspace_root, *self.policy.writable_roots],
                self.policy.extra_protected,
            ),
        )

    async def prepare_profile(self) -> Path:
        """Generate and write the profile to a temp file.

        Returns the path to the profile file.
        """
        profile_content = self.generate_profile()

        # Write to a temp file that persists for the session
        profile_dir = Path(tempfile.gettempdir()) / "kalash-sandbox"
        profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        descriptor, name = tempfile.mkstemp(prefix="profile-", suffix=".sb", dir=profile_dir)
        os.close(descriptor)
        profile_path = Path(name)
        from kalash.core.secure_io import atomic_private_write

        atomic_private_write(profile_path, profile_content.encode())

        self._profile_path = profile_path
        logger.info("Seatbelt profile written to %s", profile_path)

        return profile_path

    # ------------------------------------------------------------------
    # Enforcement
    # ------------------------------------------------------------------

    async def enforce(self) -> None:
        """Prepare sandbox enforcement.

        On macOS, enforcement is applied per-command via wrap_command().
        This method prepares the profile and validates that sandbox-exec
        is available.

        Falls closed (I-011) if cannot enforce.
        """
        if self.policy.mode == SandboxMode.DANGER_FULL_ACCESS:
            logger.warning("Sandbox in DANGER_FULL_ACCESS mode — no enforcement")
            self._enforced = True
            return

        if not self.can_enforce:
            msg = (
                "Cannot enforce sandbox (I-011): sandbox-exec not available. "
                "Refusing to proceed in restricted mode."
            )
            raise RuntimeError(msg)

        # Prepare the profile
        await self.prepare_profile()
        self._enforced = True

        logger.info(
            "macOS sandbox prepared (mode=%s, writable_roots=%d)",
            self.policy.mode,
            len(self.policy.writable_roots),
        )

    def wrap_command(self, cmd: list[str], *, cwd: str | None = None) -> list[str]:  # noqa: ARG002 - Preserve provider/tool callback signature.
        """Wrap a command with sandbox-exec isolation.

        Args:
            cmd: The command to wrap.

        Returns:
            The wrapped command prefixed with sandbox-exec.

        Raises:
            RuntimeError: If profile not prepared or sandbox-exec unavailable.
        """
        if self.policy.mode == SandboxMode.DANGER_FULL_ACCESS:
            return cmd

        if not self._sandbox_exec_path:
            raise RuntimeError("sandbox-exec not available (I-011)")

        if self._profile_path is None or not self._profile_path.exists():
            from kalash.core.secure_io import atomic_private_write

            descriptor, name = tempfile.mkstemp(prefix="kalash-seatbelt-", suffix=".sb")
            os.close(descriptor)
            self._profile_path = Path(name)
            atomic_private_write(self._profile_path, self.generate_profile().encode())

        return [
            self._sandbox_exec_path,
            "-f",
            str(self._profile_path),
            *cmd,
        ]

    # ------------------------------------------------------------------
    # Execution helper
    # ------------------------------------------------------------------

    async def run_sandboxed(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        timeout: float = 120.0,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a command inside the sandbox.

        Args:
            cmd: Command to execute.
            cwd: Working directory.
            timeout: Execution timeout in seconds.
            env: Environment variables.

        Returns:
            CompletedProcess result.
        """
        wrapped = self.wrap_command(cmd)

        proc_env = dict(os.environ)
        if env:
            proc_env.update(env)

        return subprocess.run(
            wrapped,
            cwd=cwd or self.policy.workspace_root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=proc_env,
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def cleanup(self) -> None:
        """Remove temporary profile files."""
        if self._profile_path and self._profile_path.exists():
            self._profile_path.unlink(missing_ok=True)
            logger.debug("Removed sandbox profile: %s", self._profile_path)
            self._profile_path = None
