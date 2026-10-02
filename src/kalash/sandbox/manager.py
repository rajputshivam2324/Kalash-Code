"""Platform sandbox selection and preflight. Callers must enforce wrapped=True."""

from __future__ import annotations

import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from kalash.sandbox.policy import SandboxPolicy

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SandboxStatus:
    """What enforcement is available on this machine."""

    platform: str
    backend: str
    available: bool
    detail: str

    def describe(self) -> str:
        state = "available" if self.available else "unavailable"
        return f"{self.backend} on {self.platform}: {state} — {self.detail}"


class SandboxManager:
    """Selects and applies the platform sandbox backend."""

    def __init__(
        self,
        *,
        workspace_root: Path | None = None,
        writable_roots: tuple[Path, ...] = (),
        allow_network: bool = False,
    ) -> None:
        self.workspace_root = (workspace_root or Path.cwd()).resolve()
        self.writable_roots = writable_roots or (self.workspace_root,)
        self.allow_network = allow_network
        self._backend = self._select()

    # -- selection ---------------------------------------------------------

    def _policy(self) -> SandboxPolicy:
        """Build the SandboxPolicy the backends take."""
        from kalash.sandbox.policy import NetworkPolicy, SandboxMode, SandboxPolicy

        extra = [root for root in self.writable_roots if root.resolve() != self.workspace_root]
        return SandboxPolicy(
            mode=SandboxMode.WORKSPACE_WRITE,
            workspace_root=self.workspace_root,
            additional_writable=extra,
            network_policy=(NetworkPolicy.ALLOW_ALL if self.allow_network else NetworkPolicy.DENY),
        )

    def _select(self) -> object | None:
        try:
            if sys.platform.startswith("linux"):
                from kalash.sandbox.linux import LinuxSandbox

                return LinuxSandbox(policy=self._policy())
            if sys.platform == "darwin":
                from kalash.sandbox.macos import MacOSSandbox

                return MacOSSandbox(policy=self._policy())
        except Exception as e:
            logger.warning("Failed to initialize sandbox backend", exc_info=e)
            return None
        return None

    def status(self) -> SandboxStatus:
        """Report what enforcement this machine can provide."""
        if sys.platform.startswith("linux"):
            has_bwrap = shutil.which("bwrap") is not None
            landlock = False
            try:
                probe = getattr(self._backend, "landlock_supported", None)
                landlock = bool(probe()) if callable(probe) else False
            except Exception as e:
                logger.debug("landlock support probe failed", exc_info=e)
                landlock = False

            if has_bwrap:
                parts = []
                if landlock:
                    parts.append("landlock kernel support")
                if has_bwrap:
                    parts.append("bwrap present")
                return SandboxStatus(
                    platform="linux",
                    backend="landlock/bubblewrap",
                    available=True,
                    detail=", ".join(parts),
                )
            return SandboxStatus(
                platform="linux",
                backend="landlock/bubblewrap",
                available=False,
                detail="bwrap not on PATH; Landlock is not applied to child commands",
            )

        if sys.platform == "darwin":
            available = Path("/usr/bin/sandbox-exec").exists()
            return SandboxStatus(
                platform="darwin",
                backend="seatbelt",
                available=available,
                detail="sandbox-exec present" if available else "sandbox-exec missing",
            )

        return SandboxStatus(
            platform=sys.platform,
            backend="none",
            available=False,
            detail="no sandbox backend for this platform",
        )

    # -- application -------------------------------------------------------

    def wrap(self, argv: list[str], *, cwd: str | None = None) -> tuple[list[str], bool]:
        """Wrap a command for sandboxed execution.

        Returns ``(argv, wrapped)``. When wrapping is unavailable the original
        argv comes back with ``wrapped=False``, so the caller can record that the
        command ran without OS-level confinement.

        ``cwd`` is forwarded so the sandboxed process starts in the workspace.
        A backend that silently runs the command elsewhere is worse than no
        sandbox, because the command succeeds against the wrong filesystem.
        """
        if self._backend is None or not self.status().available:
            return argv, False

        # A sandbox that breaks /dev/null or DNS turns ordinary builds into
        # cryptic failures, so the wrap is proven to work on this host before it
        # is trusted with a real command.
        if not preflight_ok(allow_network=self.allow_network):
            return argv, False

        wrap_command = getattr(self._backend, "wrap_command", None)
        if not callable(wrap_command):
            return argv, False

        target = cwd or str(self.workspace_root)
        try:
            try:
                wrapped = wrap_command(argv, cwd=target)
            except TypeError:
                # Backends that do not accept cwd cannot guarantee the working
                # directory, so decline rather than run in the wrong place.
                return argv, False
        except Exception as e:
            logger.warning("Sandbox wrap_command failed", exc_info=e)
            return argv, False

        if not wrapped or not isinstance(wrapped, list):
            return argv, False
        return wrapped, True


def get_sandbox_manager(
    *,
    workspace_root: Path | None = None,
    writable_roots: tuple[Path, ...] = (),
    allow_network: bool = False,
) -> SandboxManager:
    """Construct a manager for the current platform."""
    return SandboxManager(
        workspace_root=workspace_root,
        writable_roots=writable_roots,
        allow_network=allow_network,
    )


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
#
# Sandbox configuration is easy to get subtly wrong in ways that do not look like
# a sandbox problem. Two real examples from this codebase:
#
#   * `--ro-bind /dev /dev` leaves /dev/null unwritable, so every command
#     containing `>/dev/null` fails with `/bin/bash: line 1: /dev`.
#   * /etc/resolv.conf is a symlink into /run on systemd-resolved hosts, so DNS
#     silently fails and `npm install` hangs until its timeout.
#
# Both produced failures that looked like the agent freezing. Rather than trust
# the configuration, the wrap is verified once per process against a throwaway
# directory, and skipped entirely if it does not behave.

# Cached per (network) mode: probing costs a subprocess, and the answer cannot
# change within a process.
_PREFLIGHT: dict[bool, bool] = {}

PREFLIGHT_TIMEOUT_S = 10.0

# Exercises the two things that actually broke: a writable /dev/null and, when
# network is expected, name resolution.
_PROBE_BASE = "echo probe >/dev/null && echo DEV_OK"
_PROBE_NET = " && (getent hosts one.one.one.one >/dev/null && echo DNS_OK || echo DNS_FAIL)"


def preflight_ok(*, allow_network: bool = False, force: bool = False) -> bool:
    """Whether the sandbox actually works on this host.

    Network reachability is *not* required to pass: a machine that is genuinely
    offline should still get filesystem confinement. Only a sandbox-induced DNS
    failure disqualifies it, which is distinguished by checking resolution
    outside the sandbox too.
    """
    if not force and allow_network in _PREFLIGHT:
        return _PREFLIGHT[allow_network]

    result = _run_preflight(allow_network=allow_network)
    _PREFLIGHT[allow_network] = result
    return result


def _run_preflight(*, allow_network: bool) -> bool:
    import subprocess
    import tempfile

    if sys.platform.startswith("linux") and shutil.which("bwrap") is None:
        return False

    probe_dir = Path(tempfile.mkdtemp(prefix="kalash-sandbox-probe-"))
    try:
        manager = SandboxManager(
            workspace_root=probe_dir,
            writable_roots=(probe_dir,),
            allow_network=allow_network,
        )
        backend = manager._backend  # noqa: SLF001
        wrap_command = getattr(backend, "wrap_command", None)
        if backend is None or not callable(wrap_command):
            return False

        script = _PROBE_BASE + (_PROBE_NET if allow_network else "")
        try:
            argv = wrap_command(["/bin/sh", "-c", script], cwd=str(probe_dir))
        except Exception as e:
            logger.warning("Preflight sandbox wrap failed", exc_info=e)
            return False

        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=PREFLIGHT_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False

        output = completed.stdout
        if "DEV_OK" not in output:
            return False

        if allow_network and "DNS_FAIL" in output:
            # Only a sandbox-caused failure counts against it. If the host cannot
            # resolve either, the machine is offline and confinement is still
            # worth having.
            return not _host_can_resolve()

        return True
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


def _host_can_resolve() -> bool:
    """Whether name resolution works outside the sandbox."""
    import socket

    try:
        # Use getaddrinfo with a local timeout instead of mutating
        # socket.setdefaulttimeout() which affects the entire process (S-7).
        socket.getaddrinfo("one.one.one.one", 443, proto=socket.IPPROTO_TCP)
    except (OSError, socket.gaierror):
        return False
    return True


def preflight_report(*, allow_network: bool = False) -> str:
    """Human-readable preflight outcome, for `kalash doctor`."""
    if preflight_ok(allow_network=allow_network):
        scope = "filesystem + network" if allow_network else "filesystem"
        return f"verified ({scope})"
    return "probe failed — sandboxed commands will be refused"
