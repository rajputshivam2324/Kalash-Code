"""OS-level confinement of the commands the agent runs.

Linux uses bubblewrap: the whole filesystem is visible read-only, the workspace (and a
per-session temp dir) is writable in ``workspace-write`` mode, pids are isolated and
credential files are masked. The cost of a wrap is constant: nothing walks the workspace.
macOS uses ``sandbox-exec`` (Seatbelt); that backend is generated but has not been
exercised on a Mac in this repository's CI. When no backend works, confined modes refuse
to run commands rather than run them unconfined.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from kalash.paths import kalash_home

SECRET_ENV = (
    "*_KEY",
    "*_TOKEN",
    "*_SECRET",
    "*_PASSWORD",
    "*PASSWORD",
    "*_CREDENTIALS",
    "*API_KEY*",
    "*SECRET*",
    "*_PASS",
    "AWS_*",
    "GOOGLE_APPLICATION_CREDENTIALS",
)
MASKED_DIRS = ("~/.ssh", "~/.aws", "~/.gnupg")


def clean_env(
    env: dict[str, str] | None = None,
    pass_env: list[str] | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Environment for agent-run commands: secrets removed unless explicitly passed."""
    source = dict(os.environ if env is None else env)
    allowed = set(pass_env or [])
    out = {
        k: v
        for k, v in source.items()
        if k in allowed or not any(fnmatch.fnmatch(k.upper(), p) for p in SECRET_ENV)
    }
    out.setdefault("TERM", "dumb")
    out["GIT_TERMINAL_PROMPT"] = "0"
    out["PAGER"] = "cat"
    out["GIT_PAGER"] = "cat"
    if extra:
        out.update(extra)
    return out


@dataclass
class SandboxPolicy:
    mode: str  # read-only | workspace-write | danger-full-access
    workspace: Path
    session_tmp: Path
    network: bool = True
    writable_roots: list[Path] = field(default_factory=list)


@dataclass
class SandboxStatus:
    backend: str  # bwrap | seatbelt | none
    ok: bool
    detail: str


def _existing(paths: list[str]) -> list[Path]:
    return [Path(p).expanduser() for p in paths if Path(p).expanduser().exists()]


def protected_paths(workspace: Path) -> list[Path]:
    """Inside the workspace but read-only to confined commands (persistence/escalation)."""
    candidates = [
        workspace / ".git" / "hooks",
        workspace / ".git" / "config",
        workspace / ".kalash",
        workspace / ".mcp.json",
    ]
    return [p for p in candidates if p.exists()]


def masked_files() -> list[Path]:
    home = kalash_home()
    return [p for p in (home / "auth.json", home / "auth.key", home / "auth.lock") if p.exists()]


def bwrap_argv(policy: SandboxPolicy, cwd: Path) -> list[str]:
    argv = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
    ]
    policy.session_tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
    argv += ["--bind", str(policy.session_tmp), "/tmp"]
    if policy.mode == "workspace-write":
        for root in [policy.workspace, *policy.writable_roots]:
            if root.exists():
                argv += ["--bind", str(root), str(root)]
        for path in protected_paths(policy.workspace):
            argv += ["--ro-bind", str(path), str(path)]
    elif policy.workspace.exists():
        # Re-mount read-only so a workspace under /tmp is still visible after /tmp is replaced.
        argv += ["--ro-bind", str(policy.workspace), str(policy.workspace)]
    for directory in _existing(list(MASKED_DIRS)):
        argv += ["--tmpfs", str(directory)]
    for file in masked_files():
        argv += ["--ro-bind", "/dev/null", str(file)]
    if not policy.network:
        argv += ["--unshare-net"]
    argv += ["--chdir", str(cwd), "--"]
    return argv


def seatbelt_profile(policy: SandboxPolicy) -> tuple[str, list[str]]:
    """Seatbelt profile text plus ``-D`` parameter arguments (no string interpolation of paths)."""
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        '(allow file-write* (literal "/dev/null") (subpath "/dev/fd"))',
        '(allow file-write* (subpath (param "SESSION_TMP")))',
    ]
    params = [f"-DSESSION_TMP={policy.session_tmp.resolve()}"]
    if policy.mode == "workspace-write":
        roots = [policy.workspace, *policy.writable_roots]
        for number, root in enumerate(roots):
            lines.append(f'(allow file-write* (subpath (param "ROOT{number}")))')
            params.append(f"-DROOT{number}={root.resolve()}")
        for number, path in enumerate(protected_paths(policy.workspace)):
            lines.append(f'(deny file-write* (subpath (param "PROT{number}")))')
            params.append(f"-DPROT{number}={path.resolve()}")
    if not policy.network:
        lines.append("(deny network*)")
    return "\n".join(lines), params


_status_cache: dict[str, SandboxStatus] = {}


def detect(policy: SandboxPolicy | None = None, *, refresh: bool = False) -> SandboxStatus:
    """Probe once whether confinement works here (running a real wrapped ``true``)."""
    key = sys.platform
    if not refresh and key in _status_cache:
        return _status_cache[key]
    if sys.platform.startswith("linux"):
        if shutil.which("bwrap") is None:
            status = SandboxStatus("none", False, "bubblewrap (bwrap) is not installed")
        else:
            probe = SandboxPolicy(
                "workspace-write",
                Path.cwd(),
                Path(os.environ.get("TMPDIR", "/tmp")) / "kalash-probe",
            )
            command = [*bwrap_argv(probe, Path("/")), "true"]
            try:
                result = subprocess.run(command, capture_output=True, timeout=10, check=False)
            except (OSError, subprocess.TimeoutExpired) as error:
                status = SandboxStatus("none", False, f"bwrap could not run: {error}")
            else:
                if result.returncode == 0:
                    status = SandboxStatus("bwrap", True, "bubblewrap")
                else:
                    reason = result.stderr.decode("utf-8", "replace").strip()[:200]
                    status = SandboxStatus(
                        "none",
                        False,
                        f"bwrap failed ({reason or 'unprivileged user namespaces disabled?'})",
                    )
    elif sys.platform == "darwin":
        path = shutil.which("sandbox-exec")
        status = (
            SandboxStatus("seatbelt", True, "sandbox-exec")
            if path
            else SandboxStatus("none", False, "sandbox-exec not found")
        )
    else:
        status = SandboxStatus("none", False, f"no sandbox backend for {sys.platform}")
    _status_cache[key] = status
    return status


class SandboxUnavailable(Exception):
    """Confinement was required but no working backend exists."""


def wrap(argv: list[str], policy: SandboxPolicy, cwd: Path) -> list[str]:
    """Return ``argv`` wrapped for ``policy``. ``danger-full-access`` returns it unchanged."""
    if policy.mode == "danger-full-access":
        return argv
    status = detect()
    if not status.ok:
        raise SandboxUnavailable(
            f"commands must run in the {policy.mode} sandbox but it is unavailable: "
            f"{status.detail}. Install bubblewrap (Linux), or set permissions.sandbox to "
            f"'danger-full-access' in ~/.kalash/settings.json to run commands unconfined."
        )
    if status.backend == "bwrap":
        return [*bwrap_argv(policy, cwd), *argv]
    profile, params = seatbelt_profile(policy)
    return ["sandbox-exec", *params, "-p", profile, *argv]
