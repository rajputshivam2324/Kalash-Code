"""The rendered system prompt per tool profile, compared with files in ``tests/golden``.

The prompt is the part of a request every provider caches, so a change to it should be a
deliberate, reviewed diff - not a side effect. Regenerate on purpose with
``KALASH_UPDATE_GOLDEN=1 uv run pytest tests/unit/test_system_prompt_golden.py`` and read the diff.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kalash.agent.prompt import build_system_prompt

GOLDEN = Path(__file__).resolve().parents[1] / "golden"
ALL_TOOLS = {
    "read", "write", "edit", "glob", "grep", "bash", "job_output", "job_kill", "todo_write",
    "web_fetch", "skill", "task", "exit_plan_mode", "memory", "session_search",
}  # fmt: skip
PROFILES: dict[str, dict[str, object]] = {
    "full": {"tool_names": ALL_TOOLS},
    "full_with_settings_instructions": {
        "tool_names": ALL_TOOLS,
        "extra_instructions": "Prefer pytest over unittest.",
    },
    "explore_subagent": {"tool_names": {"read", "glob", "grep", "web_fetch"}, "subagent": True},
    "bash_only": {"tool_names": {"bash"}},
    "no_tools": {"tool_names": set()},
}


@pytest.mark.parametrize("name", list(PROFILES))
def test_system_prompt_matches_its_golden_file(name: str):
    rendered = build_system_prompt(**PROFILES[name]) + "\n"  # type: ignore[arg-type]
    path = GOLDEN / f"system_prompt_{name}.md"
    if os.environ.get("KALASH_UPDATE_GOLDEN"):
        path.parent.mkdir(exist_ok=True)
        path.write_text(rendered)
    assert path.exists(), f"missing {path}; run with KALASH_UPDATE_GOLDEN=1 to create it"
    assert rendered == path.read_text(), (
        f"{path.name} differs. If the change is intended, regenerate with KALASH_UPDATE_GOLDEN=1 "
        "and review the diff: it changes every provider's prompt cache key."
    )


def test_prompts_do_not_depend_on_the_machine_or_the_clock():
    first = build_system_prompt(tool_names=ALL_TOOLS)
    assert first == build_system_prompt(tool_names=ALL_TOOLS)
    for volatile in ("/home/", "/tmp/", "2026", "Linux", "Darwin"):
        assert volatile not in first
