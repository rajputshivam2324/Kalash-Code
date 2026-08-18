"""Tests for shared provider resolution.

The TUI and headless mode used to resolve providers independently, so a
provider connected with `/connect` was invisible to `kalash -p`. Both now go
through `kalash.models.resolve`.
"""

from __future__ import annotations

import pytest

from kalash.models import resolve


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Point the credential store at a temp KALASH_HOME."""
    monkeypatch.setenv("KALASH_HOME", str(tmp_path))
    # Clear provider env vars so tests are not affected by the real machine.
    from kalash.tui.providers import PROVIDERS

    for provider in PROVIDERS:
        monkeypatch.delenv(provider.env_key, raising=False)
    return tmp_path


class TestActiveSelection:
    def test_none_when_nothing_configured(self, isolated_store):
        assert resolve.active_selection() == (None, None)

    def test_prefers_saved_selection(self, isolated_store):
        from kalash.tui.auth_store import set_active_provider

        set_active_provider("groq", "openai/gpt-oss-20b")
        assert resolve.active_selection() == ("groq", "openai/gpt-oss-20b")

    def test_falls_back_to_environment(self, isolated_store, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
        provider_id, model_id = resolve.active_selection()
        assert provider_id == "groq"
        assert model_id is not None

    def test_saved_selection_wins_over_environment(self, isolated_store, monkeypatch):
        from kalash.tui.auth_store import set_active_provider

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        set_active_provider("groq", "openai/gpt-oss-20b")
        assert resolve.active_selection()[0] == "groq"


class TestCredentialLookup:
    def test_saved_credential_preferred(self, isolated_store, monkeypatch):
        from kalash.tui.auth_store import save_credential

        monkeypatch.setenv("GROQ_API_KEY", "from-env")
        save_credential("groq", "from-store")
        assert resolve.credential_for("groq") == "from-store"

    def test_environment_used_when_unsaved(self, isolated_store, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "from-env")
        assert resolve.credential_for("groq") == "from-env"

    def test_base_url_override(self, isolated_store):
        from kalash.tui.auth_store import save_credential

        save_credential("custom:base_url", "http://localhost:9999/v1")
        assert resolve.base_url_for("custom") == "http://localhost:9999/v1"


class TestBuildProvider:
    def test_reports_missing_provider_without_raising(self, isolated_store):
        result = resolve.build_provider()
        assert not result.ok
        assert "no provider" in result.reason.lower()

    def test_reports_missing_key(self, isolated_store):
        from kalash.tui.auth_store import set_active_provider

        set_active_provider("groq", "openai/gpt-oss-20b")
        result = resolve.build_provider()
        assert not result.ok
        assert "api key" in result.reason.lower()

    def test_reports_unknown_provider(self, isolated_store):
        result = resolve.build_provider("nope-not-real", "some-model")
        assert not result.ok
        assert "unknown provider" in result.reason.lower()

    def test_builds_openai_compatible_provider(self, isolated_store):
        from kalash.tui.auth_store import save_credential, set_active_provider

        set_active_provider("groq", "openai/gpt-oss-20b")
        save_credential("groq", "gsk-test")

        result = resolve.build_provider()
        assert result.ok
        assert result.provider_id == "groq"
        assert result.model_id == "openai/gpt-oss-20b"
        assert result.provider is not None

    def test_local_provider_needs_no_key(self, isolated_store):
        """Ollama has requires_key=False, so it must resolve without one."""
        result = resolve.build_provider("ollama", "llama3.1")
        assert result.ok, result.reason

    def test_defaults_model_when_only_provider_given(self, isolated_store):
        from kalash.tui.auth_store import save_credential

        save_credential("groq", "gsk-test")
        result = resolve.build_provider("groq", None)
        assert result.ok, result.reason
        assert result.model_id is not None


class TestAuthEncryption:
    def test_credentials_are_not_stored_in_plaintext(self, isolated_store, tmp_path, monkeypatch):
        from kalash.core.paths import kalash_home
        from kalash.tui import auth_store

        monkeypatch.setattr(auth_store, "kalash_home", lambda: tmp_path)
        auth_store.save_credential("groq", "gsk-secret-key")

        raw = (tmp_path / "auth.json").read_text(encoding="utf-8")
        assert "gsk-secret-key" not in raw
        assert auth_store.get_credential("groq") == "gsk-secret-key"
