"""Tests for subagent definition loading."""

from __future__ import annotations

from kalash.agents.loader import load_agent_definition


def test_load_agent_definition_from_project(tmp_path, monkeypatch):
    agents_dir = tmp_path / ".kalash" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.md").write_text(
        """---
name: reviewer
description: Code review specialist
tools: [read, search]
model: sonnet
max_turns: 10
mode: plan
---

Review the diff carefully and list findings.
""",
        encoding="utf-8",
    )

    from kalash.agents import loader as agents_loader

    monkeypatch.setattr(agents_loader, "agents_dirs", lambda cwd=None: [agents_dir])

    defn = load_agent_definition("reviewer")
    assert defn is not None
    assert defn.name == "reviewer"
    assert defn.model == "sonnet"
    assert defn.max_turns == 10
    assert defn.mode == "plan"
    assert "Review the diff" in defn.body
    assert defn.tools == ["read", "search"]


def test_unknown_agent_returns_none(tmp_path, monkeypatch):
    from kalash.agents import loader as agents_loader

    monkeypatch.setattr(
        agents_loader, "agents_dirs", lambda cwd=None: [tmp_path / ".kalash" / "agents"]
    )
    assert load_agent_definition("missing") is None
