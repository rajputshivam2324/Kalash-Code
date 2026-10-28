"""The start-up check that stops a stale Textual from crashing deep inside a widget."""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kalash import deps
from kalash.cli.main import app


def _pin(package: str, versions: dict[str, str | None]) -> None:
    deps.installed_version = lambda name: versions.get(name)  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def restore_version_lookup():  # type: ignore[no-untyped-def]
    original = deps.installed_version
    yield
    deps.installed_version = original  # type: ignore[assignment]


def test_current_environment_satisfies_the_requirements():
    assert deps.ui_dependency_problem() is None


@pytest.mark.parametrize(
    ("textual", "rich"),
    [("0.89.1", "15.0.0"), ("8.1.9", "15.0.0"), ("9.0.0", "15.0.0"), ("8.2.8", "13.9.4")],
)
def test_out_of_range_versions_are_reported_with_the_fix(textual: str, rich: str):
    _pin("", {"textual": textual, "rich": rich})
    problem = deps.ui_dependency_problem()
    assert problem is not None
    offender = "textual" if textual != "8.2.8" else "rich"
    assert offender in problem and (textual if offender == "textual" else rich) in problem
    assert f"{sys.executable} -m pip install --user -U" in problem


def test_a_missing_library_is_reported():
    _pin("", {"textual": None, "rich": "15.0.0"})
    assert "textual>=8.2,<9 is not installed" in (deps.ui_dependency_problem() or "")


def test_an_editable_checkout_gets_a_path_based_reinstall_command():
    _pin("", {"textual": "0.89.1", "rich": "15.0.0"})
    problem = deps.ui_dependency_problem() or ""
    checkout = Path(__file__).resolve().parents[2]
    assert f'-e "{checkout}"' in problem


def test_bounds_match_pyproject():
    """If someone bumps the pin in pyproject.toml the guard must move with it."""
    root = Path(__file__).resolve().parents[2]
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
    for package, (low, high) in deps.UI_REQUIREMENTS.items():
        spec = next(d for d in declared if re.match(rf"{package}\b", d))
        assert f">={low[0]}.{low[1]}" in spec and f"<{high[0]}" in spec, spec


def test_the_cli_refuses_to_start_the_ui_with_a_clear_message_and_usage_exit_code(
    tmp_path: Path,
):
    _pin("", {"textual": "0.89.1", "rich": "15.0.0"})
    result = CliRunner().invoke(app, ["-c", "--cwd", str(tmp_path)])  # -c: not a stdin prompt
    assert result.exit_code == 2
    assert "Traceback" not in result.output
    assert "textual>=8.2,<9 is required, found 0.89.1" in result.output
    assert "headless" in result.output


def test_doctor_reports_the_ui_libraries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    ok = CliRunner().invoke(app, ["doctor"])
    assert re.search(r"\[ok  \] terminal UI: textual \d", ok.output)
    _pin("", {"textual": "0.89.1", "rich": "15.0.0"})
    bad = CliRunner().invoke(app, ["doctor"])
    assert "[FAIL] terminal UI" in bad.output and bad.exit_code == 1
