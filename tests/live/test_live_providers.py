"""Real provider calls. Skipped unless explicitly enabled; they cost money.

    KALASH_LIVE=1 ANTHROPIC_API_KEY=... uv run pytest tests/live -m live -v
    KALASH_LIVE=1 KALASH_LIVE_MODEL_SARVAM=sarvam-105b SARVAM_API_KEY=... uv run pytest tests/live

Only keys present in the *environment* are used (never stored credentials), so a run can
never spend a key you did not name. Each test is a few hundred tokens. Results are verified
independently (file contents on disk), never from the model's own report.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kalash.agent.build import create_runtime
from kalash.config import load_settings
from kalash.llm.providers import PROVIDERS

pytestmark = pytest.mark.live

# Captured at import time, before the autouse isolation fixture strips credentials.
_ENV = dict(os.environ)
_ENABLED = _ENV.get("KALASH_LIVE") == "1"


def _candidates() -> list[str]:
    found = []
    for spec in PROVIDERS.values():
        if any(_ENV.get(name) for name in spec.env_keys) and spec.requires_key:
            found.append(spec.id)
    return found


def _model_for(provider: str) -> str:
    override = _ENV.get(f"KALASH_LIVE_MODEL_{provider.upper()}")
    if override:
        return f"{provider}/{override}"
    spec = PROVIDERS[provider]
    if not spec.models:
        pytest.skip(f"set KALASH_LIVE_MODEL_{provider.upper()} to a model name for {provider}")
    return f"{provider}/{spec.models[0]}"


@pytest.fixture(params=_candidates() or ["none"])
def live_model(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    if not _ENABLED:
        pytest.skip("live tests are off: set KALASH_LIVE=1")
    provider = request.param
    if provider == "none":
        pytest.skip("no provider API key found in the environment")
    for name in PROVIDERS[provider].env_keys:
        if _ENV.get(name):
            monkeypatch.setenv(name, _ENV[name])
    return _model_for(provider)


async def _runtime(work: Path, model: str, **overrides):
    settings = load_settings(
        work, {"model": model, "permissions": {"approval": "never"}, **overrides}
    )[0]
    return await create_runtime(cwd=work, settings=settings, model=model, with_mcp=False)


async def test_plain_answer_streams_and_reports_usage(live_model: str, work: Path):
    runtime = await _runtime(work, live_model, max_output_tokens=2000)
    try:
        result = await runtime.agent.run_turn("Reply with exactly the single word PONG.")
    finally:
        await runtime.aclose()
    assert result.reason == "completed", result.error
    assert "PONG" in result.text.upper()
    assert result.usage.input_tokens and result.usage.output_tokens


async def test_tool_round_trip_writes_a_file_we_verify_on_disk(live_model: str, work: Path):
    runtime = await _runtime(work, live_model, max_output_tokens=4000)
    try:
        result = await runtime.agent.run_turn(
            "Create a file named hello.txt containing exactly the text KALASH_LIVE_OK "
            "(no trailing text). Use your tools, then say done."
        )
    finally:
        await runtime.aclose()
    assert result.reason == "completed", result.error
    assert result.tool_calls >= 1 and not result.denials
    assert "KALASH_LIVE_OK" in (work / "hello.txt").read_text()


async def test_multi_step_read_edit_verify(live_model: str, work: Path):
    (work / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (work / "check.py").write_text(
        "from calc import add\nassert add(2, 3) == 5, 'add is wrong'\nprint('PASS')\n"
    )
    runtime = await _runtime(work, live_model, max_output_tokens=6000)
    try:
        result = await runtime.agent.run_turn(
            "check.py fails. Fix the bug in calc.py (do not edit check.py), then run "
            "`python3 check.py` to confirm it prints PASS."
        )
    finally:
        await runtime.aclose()
    assert result.reason == "completed", result.error
    import subprocess

    check = subprocess.run(
        ["python3", "check.py"], cwd=work, capture_output=True, text=True, check=False
    )
    assert check.stdout.strip() == "PASS", check.stderr
    assert "a - b" not in (work / "calc.py").read_text()
