"""The built wheel, installed into a clean venv away from the source tree.

CI sets ``KALASH_WHEEL`` to the built file; locally:  uv build && KALASH_WHEEL=dist/*.whl pytest
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kalash.paths import kalash_home
from tests.harness.mock_server import MockLLMServer, text_response, tool_call_response

WHEEL = os.environ.get("KALASH_WHEEL")
pytestmark = pytest.mark.skipif(not WHEEL, reason="set KALASH_WHEEL to a built wheel to run")


@pytest.fixture(scope="module")
def venv_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("wheel-venv")
    subprocess.run(
        ["uv", "venv", str(root), "--python", sys.executable], check=True, capture_output=True
    )
    python = root / "bin" / "python"
    assert WHEEL is not None
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), WHEEL], check=True, capture_output=True
    )
    return python


def clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    env["NO_COLOR"] = "1"
    return env


def test_installed_package_runs_outside_the_source_tree(venv_python: Path, tmp_path: Path):
    version = subprocess.run(
        [str(venv_python), "-m", "kalash", "--version"],
        cwd=tmp_path,
        env=clean_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert version.returncode == 0 and version.stdout.startswith("kalash ")
    src = Path(__file__).resolve().parents[2] / "src"
    where = subprocess.run(
        [str(venv_python), "-c", "import kalash; print(kalash.__file__)"],
        cwd=tmp_path,
        env=clean_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    assert str(src) not in where.stdout and "site-packages" in where.stdout


def test_installed_cli_completes_a_tool_task_against_a_loopback_model(
    venv_python: Path, tmp_path: Path
):
    server = MockLLMServer().start()
    try:
        kalash_home().mkdir(parents=True, exist_ok=True)
        (kalash_home() / "settings.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "mock": {
                            "kind": "openai",
                            "base_url": server.base_url,
                            "models": ["m"],
                            "requires_key": False,
                        }
                    }
                }
            )
        )
        work = tmp_path / "proj"
        work.mkdir()
        server.reply(
            200,
            tool_call_response("c1", "write", {"file_path": "out.txt", "content": "wheel ok\n"}),
        )
        server.reply(200, text_response("Done."))
        env = {**clean_env(), "KALASH_HOME": str(kalash_home()), "HOME": str(tmp_path)}
        proc = subprocess.run(
            [str(venv_python), "-m", "kalash", "-p", "write out.txt", "-m", "mock/m"],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert (work / "out.txt").read_text() == "wheel ok\n"
        assert server.unexpected == 0
    finally:
        server.stop()


def test_doctor_runs_from_the_installed_package(venv_python: Path, tmp_path: Path):
    proc = subprocess.run(
        [str(venv_python), "-m", "kalash", "doctor"],
        cwd=tmp_path,
        env={**clean_env(), "KALASH_HOME": str(tmp_path / "h")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert "sandbox" in proc.stdout and "Traceback" not in proc.stderr


def test_every_source_module_is_inside_the_wheel():
    """Regression: a broad `credentials` .gitignore pattern once dropped kalash/credentials.py
    from the wheel, which only broke the installed package, never the source checkout."""
    import zipfile

    assert WHEEL is not None
    in_wheel = {n for n in zipfile.ZipFile(WHEEL).namelist() if n.endswith(".py")}
    src = Path(__file__).resolve().parents[2] / "src"
    on_disk = {str(p.relative_to(src)) for p in src.rglob("*.py")}
    assert on_disk - in_wheel == set()


def test_stylesheet_and_every_data_file_are_inside_the_wheel():
    """The TUI loads ``app.tcss`` by path at start-up; a wheel without it installs fine and then
    crashes on first launch."""
    import zipfile

    assert WHEEL is not None
    names = set(zipfile.ZipFile(WHEEL).namelist())
    src = Path(__file__).resolve().parents[2] / "src"
    data_files = {
        str(p.relative_to(src))
        for p in (src / "kalash").rglob("*")
        if p.is_file() and p.suffix not in (".py", ".pyc") and "__pycache__" not in p.parts
    }
    assert "kalash/tui/app.tcss" in data_files
    assert data_files - names == set()


def test_installed_tui_boots_with_its_stylesheet_and_theme(venv_python: Path, tmp_path: Path):
    script = (
        "import asyncio\n"
        "from pathlib import Path\n"
        "from kalash.config import load_settings\n"
        "from kalash.tui.app import KalashApp\n"
        "async def main():\n"
        "    app = KalashApp(cwd=Path.cwd(), settings=load_settings(Path.cwd())[0])\n"
        "    async with app.run_test(size=(100, 30)) as pilot:\n"
        "        await pilot.pause(0.3)\n"
        "        print('theme', app.theme, 'prompt', app.query_one('#prompt').region.width)\n"
        "asyncio.run(main())\n"
    )
    proc = subprocess.run(
        [str(venv_python), "-c", script],
        cwd=tmp_path,
        env={**clean_env(), "KALASH_HOME": str(tmp_path / "home"), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "theme kalash-dark" in proc.stdout and "prompt 9" in proc.stdout, proc.stdout
