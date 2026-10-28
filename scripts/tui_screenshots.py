"""Render scripted Kalash TUI scenarios to SVG (and PNG when Chrome is installed).

A development aid for looking at the real UI without a terminal: every scenario runs the real
app over a scripted model. Usage:

    uv run python scripts/tui_screenshots.py [scenario ...] [--out DIR] [--size 120x36]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

Scenario = Callable[[Any, Any, Path], Awaitable[None]]


async def settle(pilot: Any, app: Any) -> None:
    await pilot.pause(0.1)
    for _ in range(200):
        if not app._turn_active:
            break
        await pilot.pause(0.05)
    await pilot.pause(0.4)


def make_app(work: Path, script: list[Any], **overrides: Any) -> Any:
    from tests.harness.scripted import ScriptedAdapter

    from kalash.config import load_settings
    from kalash.tui.app import KalashApp

    if os.environ.get("KALASH_SHOT_THEME"):
        overrides.setdefault("tui", {"theme": os.environ["KALASH_SHOT_THEME"]})
    settings = load_settings(work, {"model": "scripted/model", **overrides})[0]
    return KalashApp(
        cwd=work,
        settings=settings,
        model="scripted/model",
        adapter=ScriptedAdapter(script, model="claude-opus-5-5"),
    )


def work_tree(work: Path) -> None:
    (work / "src").mkdir()
    (work / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"
    )
    (work / "AGENTS.md").write_text("Use pytest.\n")
    skill = work / ".kalash" / "skills" / "deploy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: Ship the build to staging\n---\nSteps.\n"
    )
    (work / ".kalash" / "commands").mkdir(parents=True)
    (work / ".kalash" / "commands" / "review.md").write_text(
        "---\ndescription: review a file\n---\nReview $ARGUMENTS.\n"
    )
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=work, check=False)


async def welcome(app: Any, pilot: Any, work: Path) -> None:
    await pilot.pause(0.5)


async def effort(app: Any, pilot: Any, work: Path) -> None:
    from kalash.llm.catalog import CATALOG

    app.runtime.agent.info = CATALOG["sarvam"]["sarvam-105b"]
    app._prompt.set_text("/effort")
    await pilot.press("enter")
    await pilot.pause(0.5)


async def sarvam_models(app: Any, pilot: Any, work: Path) -> None:
    app._prompt.set_text("/model")
    await pilot.press("enter")
    await pilot.pause(0.4)
    await pilot.press("g", "l", "m")
    await pilot.pause(0.4)


async def mouse_scroll(app: Any, pilot: Any, work: Path) -> None:
    from textual import events

    from kalash.agent.events import Notice

    app._prompt.set_text("Show a long answer")
    await pilot.press("enter")
    await settle(pilot, app)
    for _ in range(5):
        await pilot._post_mouse_events([events.MouseScrollUp], "#transcript", offset=(10, 3))
    app._on_event(Notice("New output arrived while reading earlier paragraphs."))
    await pilot.pause(0.3)
    assert not app._stick and not app._at_bottom()
    assert app._activity.behind


async def conversation(app: Any, pilot: Any, work: Path) -> None:
    app._prompt.set_text("add() returns the wrong result, please fix it and run the checks")
    await pilot.press("enter")
    await settle(pilot, app)


async def slash(app: Any, pilot: Any, work: Path) -> None:
    await pilot.press("slash", "m")
    await pilot.pause(0.4)


async def slash_all(app: Any, pilot: Any, work: Path) -> None:
    await pilot.press("slash")
    await pilot.pause(0.5)


async def slash_scrolled(app: Any, pilot: Any, work: Path) -> None:
    await pilot.press("slash")
    await pilot.pause(0.3)
    for _ in range(22):
        await pilot.press("down")
    await pilot.pause(0.4)


async def full_access(app: Any, pilot: Any, work: Path) -> None:
    app._prompt.set_text("/full")
    await pilot.press("enter")
    for _ in range(100):
        await pilot.pause(0.05)
        if type(app.screen).__name__ == "ConfirmScreen":
            break
    await pilot.pause(0.8)


async def files(app: Any, pilot: Any, work: Path) -> None:
    await pilot.press("f", "i", "x", "space", "at", "c", "a")
    await pilot.pause(0.8)


async def approval(app: Any, pilot: Any, work: Path) -> None:
    app._prompt.set_text("update the project config")
    await pilot.press("enter")
    for _ in range(100):
        await pilot.pause(0.05)
        if type(app.screen).__name__ == "ApprovalScreen":
            break
    await pilot.pause(0.8)


async def plan(app: Any, pilot: Any, work: Path) -> None:
    app.runtime.agent.set_mode("plan")
    app._prompt.set_text("plan the refactor")
    await pilot.press("enter")
    for _ in range(100):
        await pilot.pause(0.05)
        if type(app.screen).__name__ == "PlanScreen":
            break
    await pilot.pause(0.5)


async def error(app: Any, pilot: Any, work: Path) -> None:
    app._prompt.set_text("hello")
    await pilot.press("enter")
    await settle(pilot, app)


async def help_screen(app: Any, pilot: Any, work: Path) -> None:
    await pilot.press("f1")
    await pilot.pause(0.6)


async def picker(app: Any, pilot: Any, work: Path) -> None:
    app._prompt.set_text("/permissions")
    await pilot.press("enter")
    await pilot.pause(0.6)


async def onboarding(app: Any, pilot: Any, work: Path) -> None:
    await pilot.pause(0.5)


def build(name: str, work: Path) -> Any:
    from tests.harness.scripted import Reply, call, calls, fail, say

    from kalash.llm.errors import ErrorCode

    if name == "welcome":
        return make_app(work, [])
    if name == "onboarding":
        from kalash.config import load_settings
        from kalash.tui.app import KalashApp

        return KalashApp(cwd=work, settings=load_settings(work)[0])
    if name == "conversation":
        return make_app(
            work,
            [
                Reply(
                    text="I'll look at the implementation first.",
                    calls=[("read", {"file_path": "src/calc.py"})],
                ),
                call(
                    "edit",
                    {
                        "file_path": "src/calc.py",
                        "old_string": "return a - b",
                        "new_string": "return a + b",
                    },
                ),
                call(
                    "bash",
                    {
                        "description": "Check add()",
                        "command": "python3 -c \"import sys; sys.path.insert(0,'src'); "
                        'from calc import add; print(add(2, 3))"',
                    },
                ),
                calls(
                    ("grep", {"pattern": "def ", "path": "src"}),
                    (
                        "todo_write",
                        {
                            "todos": [
                                {"content": "Fix add()", "status": "completed"},
                                {"content": "Add a regression test", "status": "in_progress"},
                                {"content": "Update the changelog", "status": "pending"},
                            ]
                        },
                    ),
                ),
                say(
                    "Fixed. `add()` subtracted its arguments; it now returns `a + b`.\n\n"
                    "**Verified:** `add(2, 3)` prints `5`.\n\n```python\ndef add(a, b):\n"
                    "    return a + b\n```\n\n- Next: a regression test\n- Then: the changelog"
                ),
            ],
        )
    if name == "sarvam_models":
        os.environ["SARVAM_API_KEY"] = "screenshot-key"
        return make_app(work, [])
    if name == "mouse_scroll":
        return make_app(
            work, [say("\n\n".join(f"Paragraph {i}: earlier output." for i in range(80)))]
        )
    if name in ("slash", "slash_all", "slash_scrolled", "files", "picker", "full_access", "effort"):
        return make_app(work, [])
    if name == "approval":
        (work / ".kalash").mkdir()
        (work / ".kalash" / "settings.local.json").write_text('{"model": "a/b"}\n')
        return make_app(
            work,
            [
                call("read", {"file_path": ".kalash/settings.local.json"}),
                call(
                    "edit",
                    {
                        "file_path": ".kalash/settings.local.json",
                        "old_string": '"model": "a/b"',
                        "new_string": '"model": "c/d"',
                    },
                ),
                say("done"),
            ],
        )
    if name == "plan":
        return make_app(
            work,
            [
                call(
                    "exit_plan_mode",
                    {
                        "plan": "## Plan\n\n1. Extract `parse()` into `parser.py`\n"
                        "2. Update the imports in `cli.py`\n"
                        "3. Run `pytest -q` and fix any fallout\n\n"
                        "**Risk:** public import path changes."
                    },
                ),
                say("ok"),
            ],
        )
    if name == "error":
        return make_app(work, [fail(ErrorCode.AUTH, "Incorrect API key provided: sk-…abcd")])
    if name == "help":
        return make_app(work, [])
    raise SystemExit(f"unknown scenario {name}")


SCENARIOS: dict[str, Scenario] = {
    "welcome": welcome,
    "effort": effort,
    "sarvam_models": sarvam_models,
    "mouse_scroll": mouse_scroll,
    "onboarding": onboarding,
    "conversation": conversation,
    "slash": slash,
    "slash_all": slash_all,
    "slash_scrolled": slash_scrolled,
    "full_access": full_access,
    "files": files,
    "approval": approval,
    "plan": plan,
    "error": error,
    "help": help_screen,
    "picker": picker,
}


async def run_one(name: str, out: Path, size: tuple[int, int]) -> Path:
    work = Path(tempfile.mkdtemp(prefix="kalash-shot-"))
    os.environ["KALASH_HOME"] = str(work / "home")
    work_tree(work)
    os.chdir(work)
    app = build(name, work)
    async with app.run_test(size=size) as pilot:
        await pilot.pause(0.3)
        await SCENARIOS[name](app, pilot, work)
        svg = app.export_screenshot(title="Kalash")
    path = out / f"{name}.svg"
    path.write_text(svg)
    return path


def to_png(svg: Path, size: tuple[int, int]) -> Path | None:
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if chrome is None:
        return None
    png = svg.with_suffix(".png")
    width, height = size[0] * 12 + 60, size[1] * 24 + 80
    subprocess.run(
        [
            chrome,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--screenshot={png}",
            f"--window-size={width},{height}",
            f"file://{svg}",
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )
    return png if png.exists() else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenarios", nargs="*", default=list(SCENARIOS))
    parser.add_argument("--out", default="/tmp/kalash-shots")
    parser.add_argument("--size", default="110x34")
    parser.add_argument("--theme", help="e.g. kalash-light, nord, tokyo-night")
    args = parser.parse_args()
    if args.theme:
        os.environ["KALASH_SHOT_THEME"] = args.theme
    cols, rows = (int(x) for x in args.size.split("x"))
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    for name in args.scenarios:
        svg = asyncio.run(run_one(name, out, (cols, rows)))
        png = to_png(svg, (cols, rows))
        print(f"{name}: {png or svg}")


if __name__ == "__main__":
    main()
