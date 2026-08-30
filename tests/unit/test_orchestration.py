"""Tests for orchestration patterns — Fanout, Pipeline, Critic, Supervisor."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kalash.core.budget import BudgetState
from kalash.core.errors import KalashError
from kalash.orchestration.subagent import (
    SubagentConfig,
    ResultEnvelope,
)
from kalash.orchestration.team import (
    TeamConfig,
    TeamResult,
    FanoutPattern,
    PipelinePattern,
    CriticPattern,
    SupervisorPattern,
    ConcurrencyCapError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(name: str = "agent", prompt: str = "do work") -> SubagentConfig:
    return SubagentConfig(agent=name, prompt=prompt, max_tokens=100, max_turns=5)


def _make_result(status: str = "completed", result: Any = "done") -> ResultEnvelope:
    return ResultEnvelope(run_id="run_1", status=status, result=result)


class _MockEngine:
    """Minimal StorageEngine stand-in for team tests."""
    pass


# ---------------------------------------------------------------------------
# TeamConfig / TeamResult
# ---------------------------------------------------------------------------


class TestTeamConfig:
    def test_defaults(self):
        c = TeamConfig()
        assert c.max_concurrency == 8
        assert isinstance(c.budget, BudgetState)

    def test_custom_concurrency(self):
        c = TeamConfig(max_concurrency=2)
        assert c.max_concurrency == 2


class TestTeamResult:
    def test_defaults(self):
        r = TeamResult(pattern="fanout")
        assert r.status == "completed"
        assert r.results == []
        assert r.merged_output is None


# ---------------------------------------------------------------------------
# FanoutPattern
# ---------------------------------------------------------------------------


class TestFanoutPattern:
    @pytest.fixture
    def pattern(self):
        config = TeamConfig(max_concurrency=4)
        return FanoutPattern(_MockEngine(), config)

    @pytest.mark.asyncio
    async def test_empty_agents_returns_completed(self, pattern):
        result = await pattern.execute([])
        assert result.status == "completed"
        assert result.results == []

    @pytest.mark.asyncio
    async def test_cycle_detection_raises(self, pattern):
        agents = [
            _make_config("agent_a", "do thing"),
            _make_config("agent_a", "do thing"),  # duplicate
        ]
        with pytest.raises(KalashError, match="Cycle"):
            await pattern.execute(agents)

    @pytest.mark.asyncio
    async def test_spawn_with_cap_respects_semaphore(self, pattern):
        """Verify that _spawn_with_cap tracks active count."""
        mock_result = _make_result()

        async def _fake_spawn(**kwargs):
            return mock_result

        pattern._runner = MagicMock()
        pattern._runner.spawn = AsyncMock(return_value=mock_result)

        config = _make_config()
        result = await pattern._spawn_with_cap(config)
        assert result.status == "completed"
        assert pattern._active_count == 0  # restored after completion


# ---------------------------------------------------------------------------
# PipelinePattern
# ---------------------------------------------------------------------------


class TestPipelinePattern:
    @pytest.fixture
    def pattern(self):
        config = TeamConfig(max_concurrency=4)
        return PipelinePattern(_MockEngine(), config)

    @pytest.mark.asyncio
    async def test_empty_pipeline_completes(self, pattern):
        result = await pattern.execute([])
        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_cycle_detection_raises(self, pattern):
        agents = [
            _make_config("a", "prompt"),
            _make_config("a", "prompt"),
        ]
        with pytest.raises(KalashError, match="Cycle"):
            await pattern.execute(agents)


# ---------------------------------------------------------------------------
# CriticPattern
# ---------------------------------------------------------------------------


class TestCriticPattern:
    @pytest.fixture
    def pattern(self):
        config = TeamConfig(max_concurrency=4)
        return CriticPattern(_MockEngine(), config, max_rounds=3)

    @pytest.mark.asyncio
    async def test_requires_at_least_two_agents(self, pattern):
        with pytest.raises(KalashError, match="at least 2"):
            await pattern.execute([_make_config()])

    def test_max_rounds_configurable(self):
        config = TeamConfig()
        pattern = CriticPattern(_MockEngine(), config, max_rounds=5)
        assert pattern._max_rounds == 5


# ---------------------------------------------------------------------------
# SupervisorPattern
# ---------------------------------------------------------------------------


class TestSupervisorPattern:
    @pytest.fixture
    def pattern(self):
        config = TeamConfig(max_concurrency=4)
        return SupervisorPattern(_MockEngine(), config, max_delegations=5)

    @pytest.mark.asyncio
    async def test_requires_at_least_supervisor(self, pattern):
        with pytest.raises(KalashError, match="at least a supervisor"):
            await pattern.execute([])

    def test_parse_delegations_valid_json(self, pattern):
        workers = {"worker_a": _make_config("worker_a")}
        output = '[{"agent": "worker_a", "prompt": "do work"}]'
        delegations = pattern._parse_delegations(output, workers)
        assert len(delegations) == 1
        assert delegations[0].agent == "worker_a"

    def test_parse_delegations_json_in_code_block(self, pattern):
        workers = {"worker_b": _make_config("worker_b")}
        output = '```json\n[{"agent": "worker_b", "prompt": "test"}]\n```'
        delegations = pattern._parse_delegations(output, workers)
        assert len(delegations) == 1

    def test_parse_delegations_malformed_returns_empty(self, pattern):
        workers = {"w": _make_config("w")}
        delegations = pattern._parse_delegations("not json at all", workers)
        assert delegations == []

    def test_parse_delegations_unknown_agent_skipped(self, pattern):
        workers = {"known": _make_config("known")}
        output = '[{"agent": "unknown_agent", "prompt": "test"}]'
        delegations = pattern._parse_delegations(output, workers)
        assert delegations == []

    def test_max_delegations_configurable(self):
        config = TeamConfig()
        pattern = SupervisorPattern(_MockEngine(), config, max_delegations=3)
        assert pattern._max_delegations == 3


# ---------------------------------------------------------------------------
# SubagentConfig
# ---------------------------------------------------------------------------


class TestSubagentConfig:
    def test_fields(self):
        c = SubagentConfig(agent="test", prompt="hello", max_tokens=500, max_turns=10)
        assert c.agent == "test"
        assert c.max_tokens == 500

    def test_result_envelope_defaults(self):
        r = ResultEnvelope(run_id="r1", status="completed")
        assert r.result is None
        assert r.error is None
