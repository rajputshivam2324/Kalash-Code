"""Linux sandbox enforcement — Landlock LSM + Bubblewrap fallback.

Uses Landlock (kernel >= 5.13) for mandatory access control when available.
Falls back to Bubblewrap (bwrap) for namespace-based isolation.
Falls closed if neither can enforce (I-011).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kalash.core.paths import temp_dir_for_session

from .policy import SandboxMode, SandboxPolicy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Landlock constants
# ---------------------------------------------------------------------------

# Landlock ABI versions correspond to kernel features
LANDLOCK_CREATE_RULESET = 444  # syscall numbers (x86_64)
LANDLOCK_ADD_RULE = 445
LANDLOCK_RESTRICT_SELF = 446

# Access rights
LANDLOCK_ACCESS_FS_READ_FILE = 1 << 0
LANDLOCK_ACCESS_FS_READ_DIR = 1 << 1
LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 2
LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 4
LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 5
LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 6
LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7

LANDLOCK_WRITE_ACCESS = (
    LANDLOCK_ACCESS_FS_WRITE_FILE
    | LANDLOCK_ACCESS_FS_REMOVE_FILE
    | LANDLOCK_ACCESS_FS_REMOVE_DIR
    | LANDLOCK_ACCESS_FS_MAKE_REG
    | LANDLOCK_ACCESS_FS_MAKE_DIR
)

LANDLOCK_READ_ACCESS = (
    LANDLOCK_ACCESS_FS_READ_FILE
    | LANDLOCK_ACCESS_FS_READ_DIR
)


# ---------------------------------------------------------------------------
# Linux sandbox
# ---------------------------------------------------------------------------


@dataclass
class LinuxSandbox:
    """Linux sandbox using Landlock LSM or Bubblewrap.

    Enforcement priority:
    1. Landlock (kernel >= 5.13, no root required)
    2. Bubblewrap (user namespace, bwrap binary required)
    3. Falls closed (I-011) — refuses to execute if cannot enforce.
    """

    policy: SandboxPolicy

    # Internal state
    _landlock_available: bool | None = field(init=False, default=None)
    _bwrap_available: bool | None = field(init=False, default=None)
    _enforced: bool = field(init=False, default=False)

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    @property
    def landlock_supported(self) -> bool:
        """Check if Landlock LSM is available (kernel >= 5.13)."""
        if self._landlock_available is not None:
            return self._landlock_available

        self._landlock_available = False

        # Check kernel version
        try:
            release = platform.release()
            parts = release.split(".")
            major, minor = int(parts[0]), int(parts[1])
            if major < 5 or (major == 5 and minor < 13):
                return False
        except (ValueError, IndexError):
            return False

        # Check if Landlock is actually enabled
        try:
            # /sys/kernel/security/landlock/status exists when enabled
            status_path = Path("/sys/kernel/security/landlock/status")
            if status_path.exists():
                self._landlock_available = True
        except OSError:
            pass

        # Alternative: try the syscall
        if not self._landlock_available:
            try:
                libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
                # Try creating an empty ruleset — if syscall exists, Landlock is available
                ret = libc.syscall(LANDLOCK_CREATE_RULESET, None, 0, 0)
                if ret >= 0:
                    os.close(ret)
                    self._landlock_available = True
                else:
                    # ENOSYS means not available, other errors mean it exists
                    errno = ctypes.get_errno()
                    self._landlock_available = errno != 38  # ENOSYS
            except (OSError, AttributeError):
                pass

        return self._landlock_available

    @property
    def bwrap_available(self) -> bool:
        """Check if Bubblewrap (bwrap) is available."""
        if self._bwrap_available is not None:
            return self._bwrap_available

        self._bwrap_available = shutil.which("bwrap") is not None
        return self._bwrap_available

    @property
    def can_enforce(self) -> bool:
        """Whether any enforcement mechanism is available."""
        if self.policy.mode == SandboxMode.DANGER_FULL_ACCESS:
            return True  # No enforcement needed
        return self.landlock_supported or self.bwrap_available

    @property
    def is_enforced(self) -> bool:
        return self._enforced

    # ------------------------------------------------------------------
    # Enforcement
    # ------------------------------------------------------------------

    async def enforce(self) -> None:
        """Apply sandbox restrictions.

        Falls closed (I-011): if no enforcement mechanism is available
        and mode is not DANGER_FULL_ACCESS, raises RuntimeError.
        """
        if self.policy.mode == SandboxMode.DANGER_FULL_ACCESS:
            logger.warning("Sandbox in DANGER_FULL_ACCESS mode — no enforcement")
            self._enforced = True
            return

        if not self.can_enforce:
            msg = (
                "Cannot enforce sandbox (I-011): neither Landlock nor Bubblewrap available. "
                "Refusing to proceed in restricted mode."
            )
            raise RuntimeError(msg)

        if self.landlock_supported:
            await self._enforce_landlock()
        elif self.bwrap_available:
            logger.info("Landlock not available, using Bubblewrap fallback")
            # Bubblewrap is applied per-process via wrap_command()
            self._enforced = True
        else:
            msg = "No sandbox enforcement available (I-011)"
            raise RuntimeError(msg)

    async def _enforce_landlock(self) -> None:
        """Apply Landlock restrictions to the current process.

        This restricts the CURRENT process — once applied, cannot be undone.
        """
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

            # Create ruleset with all FS access rights handled
            all_access = LANDLOCK_READ_ACCESS | LANDLOCK_WRITE_ACCESS

            # struct landlock_ruleset_attr
            class RulesetAttr(ctypes.Structure):
                _fields_ = [("handled_access_fs", ctypes.c_uint64)]

            attr = RulesetAttr(handled_access_fs=all_access)
            ruleset_fd = libc.syscall(
                LANDLOCK_CREATE_RULESET,
                ctypes.byref(attr),
                ctypes.sizeof(attr),
                0,
            )

            if ruleset_fd < 0:
                raise OSError(f"landlock_create_ruleset failed: errno={ctypes.get_errno()}")

            try:
                # Add rules for writable roots (read + write)
                for root in self.policy.writable_roots:
                    self._add_landlock_rule(
                        libc, ruleset_fd, root, LANDLOCK_READ_ACCESS | LANDLOCK_WRITE_ACCESS
                    )

                # Add read-only rules for common system paths
                read_only_paths = [
                    Path("/usr"),
                    Path("/lib"),
                    Path("/lib64"),
                    Path("/etc"),
                    Path("/proc"),
                    Path("/sys"),
                    Path("/dev"),
                ]
                for path in read_only_paths:
                    if path.exists():
                        self._add_landlock_rule(
                            libc, ruleset_fd, path, LANDLOCK_READ_ACCESS
                        )

                # Restrict self
                ret = libc.syscall(LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
                if ret < 0:
                    raise OSError(f"landlock_restrict_self failed: errno={ctypes.get_errno()}")

            finally:
                os.close(ruleset_fd)

            self._enforced = True
            logger.info("Landlock sandbox enforced with %d writable roots",
                       len(self.policy.writable_roots))

        except (OSError, AttributeError) as exc:
            logger.error("Landlock enforcement failed: %s", exc)
            raise RuntimeError(f"Landlock enforcement failed (I-011): {exc}") from exc

    def _add_landlock_rule(
        self,
        libc: Any,
        ruleset_fd: int,
        path: Path,
        access: int,
    ) -> None:
        """Add a Landlock path-beneath rule."""
        if not path.exists():
            return

        fd = os.open(str(path), os.O_PATH | os.O_CLOEXEC)
        try:
            # struct landlock_path_beneath_attr
            class PathBeneathAttr(ctypes.Structure):
                _fields_ = [
                    ("allowed_access", ctypes.c_uint64),
                    ("parent_fd", ctypes.c_int32),
                ]

            attr = PathBeneathAttr(allowed_access=access, parent_fd=fd)
            libc.syscall(
                LANDLOCK_ADD_RULE,
                ruleset_fd,
                1,  # LANDLOCK_RULE_PATH_BENEATH
                ctypes.byref(attr),
                0,
            )
        finally:
            os.close(fd)

    # ------------------------------------------------------------------
    # Bubblewrap command wrapping
    # ------------------------------------------------------------------

    def wrap_command(self, cmd: list[str], *, network: bool = False) -> list[str]:
        """Wrap a command with Bubblewrap isolation.

        Args:
            cmd: The command to wrap.
            network: Whether to allow network access.

        Returns:
            The wrapped command (prefixed with bwrap + options).
        """
        if self.policy.mode == SandboxMode.DANGER_FULL_ACCESS:
            return cmd  # No wrapping in full access

        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise RuntimeError("bwrap not found — cannot wrap command (I-011)")

        args: list[str] = [bwrap]

        # Read-only bind mounts for system
        for sys_path in ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/proc", "/dev"):
            if Path(sys_path).exists():
                args.extend(["--ro-bind", sys_path, sys_path])

        # Writable bind mounts
        for root in self.policy.writable_roots:
            root_str = str(root)
            args.extend(["--bind", root_str, root_str])

        # Temp directory
        args.extend(["--tmpfs", "/tmp"])

        # Network isolation
        if not network and not self.policy.network_allowed():
            args.append("--unshare-net")

        # User namespace
        args.append("--unshare-user")
        args.append("--die-with-parent")

        # The actual command
        args.extend(cmd)

        return args

    # ------------------------------------------------------------------
    # Network denial
    # ------------------------------------------------------------------

    def deny_network(self) -> bool:
        """Check if network should be denied based on policy."""
        return not self.policy.network_allowed()
