"""Tests for hooks system — events, payloads, runner dispatch, and loop protection."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kalash.hooks.events import (
    PAYLOAD_REGISTRY,
    CompactPayload,
    FileEditPayload,
    HookEvent,
    HookPayload,
    NotificationPayload,
    PluginLoadPayload,
    RecallPayload,
    SessionEndPayload,
    SessionStartPayload,
    StopPayload,
    SubagentPayload,
    ToolUsePayload,
)

# ---------------------------------------------------------------------------
# Event / payload unit tests
# ---------------------------------------------------------------------------


class TestHookEvents:
    """Hook event definitions and payload schemas."""

    def test_all_hook_events_are_strings(self):
        for event in HookEvent:
            assert isinstance(event.value, str)

    def test_payload_registry_covers_all_events(self):
        for event in HookEvent:
            assert event in PAYLOAD_REGISTRY, f"Missing payload for {event}"

    def test_session_start_payload(self):
        p = SessionStartPayload(
            event=HookEvent.SESSION_START,
            session_id="ses_1",
            project_dir="/home/test",
            agent="default",
        )
        assert p.project_dir == "/home/test"
        assert p.agent == "default"

    def test_session_end_payload(self):
        p = SessionEndPayload(
            event=HookEvent.SESSION_END,
            reason="user_exit",
            duration_ms=3000,
        )
        assert p.reason == "user_exit"
        assert p.duration_ms == 3000

    def test_tool_use_payload_pre(self):
        p = ToolUsePayload(
            event=HookEvent.PRE_TOOL_USE,
            tool_name="shell",
            arguments={"command": "ls"},
            blocked=False,
        )
        assert p.tool_name == "shell"
        assert not p.blocked

    def test_tool_use_payload_post(self):
        p = ToolUsePayload(
            event=HookEvent.POST_TOOL_USE,
            tool_name="shell",
            result="ok",
            duration_ms=250,
        )
        assert p.result == "ok"
        assert p.duration_ms == 250

    def test_file_edit_payload(self):
        p = FileEditPayload(
            event=HookEvent.PRE_FILE_EDIT,
            path="/tmp/test.py",
            operation="create",
            content_preview="import os",
        )
        assert p.operation == "create"

    def test_compact_payload(self):
        p = CompactPayload(
            event=HookEvent.POST_COMPACT,
            tokens_before=10000,
            tokens_after=3000,
            strategy="summary",
        )
        assert p.tokens_after < p.tokens_before

    def test_recall_payload(self):
        p = RecallPayload(
            event=HookEvent.POST_RECALL,
            query="test query",
            results_count=5,
            providers=["local"],
        )
        assert p.results_count == 5

    def test_subagent_payload(self):
        p = SubagentPayload(
            event=HookEvent.SUBAGENT_START,
            child_run_id="run_1",
            parent_run_id="run_0",
            depth=2,
        )
        assert p.depth == 2

    def test_notification_payload(self):
        p = NotificationPayload(
            event=HookEvent.NOTIFICATION,
            level="warning",
            title="Budget low",
        )
        assert p.level == "warning"

    def test_stop_payload(self):
        p = StopPayload(
            event=HookEvent.STOP,
            reason="budget",
        )
        assert p.reason == "budget"

    def test_plugin_load_payload(self):
        p = PluginLoadPayload(
            event=HookEvent.PLUGIN_LOAD,
            plugin_name="my_plugin",
            plugin_version="1.0.0",
        )
        assert p.plugin_name == "my_plugin"

    def test_base_payload_data_field(self):
        p = HookPayload(
            event=HookEvent.STOP,
            data={"key": "value"},
        )
        assert p.data["key"] == "value"

    def test_base_payload_defaults(self):
        p = HookPayload(event=HookEvent.STOP)
        assert p.session_id is None
        assert p.run_id is None
        assert p.timestamp == ""
        assert p.data == {}


# ---------------------------------------------------------------------------
# HookRunner tests (using mock configs)
# ---------------------------------------------------------------------------


class TestHookRunnerImport:
    """Verify runner module imports and basic runner setup."""

    def test_runner_importable(self):
        from kalash.hooks.runner import HookConfig, HookResult, HookRunner

        assert HookRunner is not None
        assert HookConfig is not None
        assert HookResult is not None

    def test_runner_max_chain_depth(self):
        from kalash.hooks.runner import HookRunner

        runner = HookRunner(engine=MagicMock())
        assert runner.MAX_CHAIN_DEPTH >= 1

    @pytest.mark.asyncio
    async def test_empty_dispatch_returns_empty(self):
        from kalash.hooks.runner import HookRunner

        runner = HookRunner(engine=MagicMock())
        payload = HookPayload(event=HookEvent.SESSION_START)
        results = await runner.dispatch(payload)
        assert results == []

    @pytest.mark.asyncio
    async def test_loop_protection_fires(self):
        """If chain depth exceeds MAX_CHAIN_DEPTH, dispatch returns empty."""
        from kalash.hooks.runner import HookRunner

        runner = HookRunner(engine=MagicMock())
        # Artificially exceed depth
        chain_id = "test_chain"
        runner._chain_depth[chain_id] = runner.MAX_CHAIN_DEPTH + 1
        payload = HookPayload(event=HookEvent.SESSION_START)
        results = await runner.dispatch(payload, chain_id=chain_id)
        assert results == []

    def test_register_hook(self):
        from kalash.hooks.runner import HandlerType, HookConfig, HookRunner

        runner = HookRunner(engine=MagicMock())
        config = HookConfig(
            id="hook_1",
            name="test_hook",
            event=HookEvent.SESSION_START,
            handler_type=HandlerType.COMMAND,
        )
        runner.register(config)
        assert HookEvent.SESSION_START in runner._hooks
        assert len(runner._hooks[HookEvent.SESSION_START]) == 1

    def test_register_python_handler(self):
        from kalash.hooks.runner import HookRunner

        runner = HookRunner(engine=MagicMock())

        async def my_handler(payload):
            pass

        runner.register_python("test", HookEvent.PRE_TOOL_USE, my_handler)
        assert HookEvent.PRE_TOOL_USE in runner._hooks


async def test_runtime_hooks_require_pinned_source_and_recheck_changes(tmp_path):
    import json

    from kalash.core.errors import HookDeniedError
    from kalash.core.trust import trust_file
    from kalash.hooks.load import build_hook_runner

    directory = tmp_path / ".kalash" / "hooks"
    directory.mkdir(parents=True)
    path = directory / "test.json"
    config = {
        "hooks": [
            {
                "name": "test",
                "trigger": "PreToolUse",
                "action": {"type": "command", "command": "echo authorized"},
            }
        ]
    }
    path.write_text(json.dumps(config))
    assert build_hook_runner(tmp_path) is None
    trust_file(path)
    runner = build_hook_runner(tmp_path)
    assert runner is not None
    path.write_text(json.dumps({"hooks": []}))
    with pytest.raises(HookDeniedError):
        await runner.dispatch(ToolUsePayload(event=HookEvent.PRE_TOOL_USE, tool_name="read"))
