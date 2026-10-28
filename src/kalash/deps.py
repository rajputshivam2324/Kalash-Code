"""Check that the installed terminal-UI libraries are the ones the code was written for.

An editable install keeps running after ``git pull`` without re-resolving dependencies, so a
machine can end up with new Kalash code on an old Textual. Without this check that surfaces as a
Rich traceback from deep inside a widget constructor (``TextArea.__init__() got an unexpected
keyword argument 'compact'``). ``tests/unit/test_deps.py`` keeps these bounds equal to
``pyproject.toml``.
"""

from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

# package -> (minimum inclusive, maximum exclusive), major.minor only
UI_REQUIREMENTS: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {
    "textual": ((8, 2), (9, 0)),
    "rich": ((14, 0), (16, 0)),
}


def installed_version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


def _major_minor(version: str) -> tuple[int, int] | None:
    match = re.match(r"(\d+)\.(\d+)", version)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _spec(package: str) -> str:
    low, high = UI_REQUIREMENTS[package]
    return f"{package}>={low[0]}.{low[1]},<{high[0]}"


def _checkout() -> Path | None:
    """The source checkout this code runs from (editable installs), if there is one."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").is_file() else None


def ui_dependency_problem() -> str | None:
    """A ready-to-print explanation when a UI dependency is missing or out of range."""
    wrong: list[str] = []
    for package, (low, high) in UI_REQUIREMENTS.items():
        version = installed_version(package)
        parsed = _major_minor(version) if version else None
        if version is None:
            wrong.append(f"{_spec(package)} is not installed")
        elif parsed is None or not low <= parsed < high:
            wrong.append(f"{_spec(package)} is required, found {version}")
    if not wrong:
        return None
    target = str(_checkout() or "kalash-code")
    return (
        "the terminal UI cannot start: "
        + "; ".join(wrong)
        + f".\n  Reinstall so dependencies are updated:\n  {sys.executable} -m pip install --user"
        f' -U {"-e " if _checkout() else ""}"{target}"\n'
        "  (`kalash -p ...` headless mode does not need the UI libraries.)"
    )
