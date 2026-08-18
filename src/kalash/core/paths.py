"""XDG-aware path resolution.

Kalash stores data at ~/.kalash by default, overridable via KALASH_HOME.
"""

from __future__ import annotations

import os
from pathlib import Path


def kalash_home() -> Path:
    """Return the Kalash home directory (creates if needed)."""
    home = Path(os.environ.get("KALASH_HOME", Path.home() / ".kalash"))
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    return home


def kalash_db_path() -> Path:
    """Path to the main SQLite database."""
    return kalash_home() / "kalash.db"


def blobs_dir() -> Path:
    """Path to the content-addressed blob store."""
    d = kalash_home() / "blobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir() -> Path:
    """Path to the log directory."""
    d = kalash_home() / "logs"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def cache_dir(project_dir: Path | None = None) -> Path:
    """Path to cache directory (project-local or global)."""
    if project_dir:
        d = project_dir / ".kalash" / "cache"
    else:
        d = kalash_home() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def settings_path(scope: str = "user") -> Path:
    """Path to settings file.

    Args:
        scope: 'user' for ~/.kalash/settings.json,
               'project' for .kalash/settings.json in cwd.
    """
    if scope == "user":
        return kalash_home() / "settings.json"
    return Path.cwd() / ".kalash" / "settings.json"


def credentials_path() -> Path:
    """Path to credentials (keyring is preferred over this)."""
    return kalash_home() / "credentials.json"


def skills_dirs() -> list[Path]:
    """Discovery paths for skills: user-global then workspace."""
    paths = [kalash_home() / "skills"]
    workspace_skills = Path.cwd() / ".kalash" / "skills"
    if workspace_skills.exists():
        paths.append(workspace_skills)
    return paths


def agents_dirs() -> list[Path]:
    """Discovery paths for agent definitions."""
    paths = [kalash_home() / "agents"]
    workspace_agents = Path.cwd() / ".kalash" / "agents"
    if workspace_agents.exists():
        paths.append(workspace_agents)
    return paths


def temp_dir_for_session(session_id: str) -> Path:
    """Per-session temp directory (writable root)."""
    import tempfile

    d = Path(tempfile.gettempdir()) / f"kalash-{session_id}"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d
