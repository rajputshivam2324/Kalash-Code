"""Shared fixtures for Kalash test suite."""

import sys
from pathlib import Path

import pytest

# Ensure the source is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture(autouse=True)
def isolated_user_configuration(tmp_path, monkeypatch):
    """Tests never consume a developer's real credentials or active provider."""
    monkeypatch.setenv("KALASH_HOME", str(tmp_path / "kalash-home"))
    for variable in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
        "SARVAM_API_KEY",
        "KALASH_PROVIDER",
        "KALASH_MODEL",
    ):
        monkeypatch.delenv(variable, raising=False)
