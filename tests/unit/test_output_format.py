"""Tests for headless JSON output format."""

import json

import pytest


@pytest.mark.parametrize("fmt", ["json", "stream-json"])
def test_failure_output_preserves_run_verification_and_effects(fmt):
    import io

    from kalash.cli.output import OutputFormat, OutputSink, RunMetrics

    output = io.StringIO()
    metrics = RunMetrics(
        run_id="run-failed",
        model_id="primary",
        used_model="fallback",
        fallback_chain=["fallback"],
        files_changed=[{"path": "changed.py", "operation": "modified"}],
        verification=[{"command": "check", "passed": False, "exit_code": 1}],
    )
    sink = OutputSink(OutputFormat(fmt), stdout=output, metrics=metrics)
    sink.finish_error(code="KALASH_RUN_FAILED", message="independent check failed")
    envelope = json.loads(output.getvalue())
    assert envelope["run_id"] == "run-failed"
    payload = envelope["data"] if fmt == "stream-json" else envelope
    assert payload["verification"] == metrics.verification
    assert payload["files_changed"] == metrics.files_changed
    assert payload["model"]["used"] == "fallback"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("KALASH_HOME", str(home))
    monkeypatch.chdir(project)
    from kalash.runtime.scratchpad import reset_cache

    reset_cache()
    yield project
    reset_cache()


class FakeProvider:
    name = "fake/model-1"
    context_window = 200_000

    def __init__(self, text: str):
        self.text = text

    async def stream(self, _messages, **_kwargs):
        from kalash.models.normalize import (
            BlockDelta,
            BlockStart,
            BlockStop,
            MessageStart,
            MessageStop,
            StopReason,
            UsageUpdate,
        )

        yield MessageStart(id="m", model="fake/model-1")
        yield BlockStart(index=0, block_type="text")
        yield BlockDelta(index=0, delta=self.text)
        yield BlockStop(index=0)
        yield UsageUpdate(input_tokens=100, output_tokens=10)
        yield MessageStop(StopReason.END_TURN)


class FakeResolution:
    def __init__(self, provider):
        self.ok = True
        self.provider = provider
        self.reason = ""


@pytest.mark.asyncio
async def test_json_output_format(workspace, monkeypatch, capsys):
    assert workspace.is_dir()
    provider = FakeProvider("hello json")
    monkeypatch.setattr(
        "kalash.models.resolve.build_provider",
        lambda *_args, **_kwargs: FakeResolution(provider),
    )
    from kalash.cli._pipe import _run
    from kalash.cli.output import EXIT_OK, OutputFormat

    code = await _run(
        "hi",
        output_format=OutputFormat.JSON,
    )
    assert code == EXIT_OK

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["ok"] is True
    assert payload["result"] == "hello json"
    assert payload["schema_version"] == "1.0"
    assert payload["session_id"].startswith("ses_")
    assert payload["usage"]["input_tokens"] == 100
    assert payload["usage"]["output_tokens"] == 10
    assert payload["usage"]["source"] == "provider_reported"
    assert payload["budget"]["tokens"]["ceiling"] is None
    assert payload["budget"]["turns"]["ceiling"] == 100


@pytest.mark.asyncio
async def test_stream_json_emits_events(workspace, monkeypatch, capsys):
    assert workspace.is_dir()
    provider = FakeProvider("streamed")
    monkeypatch.setattr(
        "kalash.models.resolve.build_provider",
        lambda *_args, **_kwargs: FakeResolution(provider),
    )
    from kalash.cli._pipe import _run
    from kalash.cli.output import EXIT_OK, OutputFormat

    code = await _run("hi", output_format=OutputFormat.STREAM_JSON)
    assert code == EXIT_OK

    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    events = [line["event"] for line in lines]
    assert "session.started" in events
    assert "message.delta" in events
    assert events[-1] == "result"


@pytest.mark.asyncio
async def test_stream_json_preserves_long_tool_output_and_call_id(workspace, monkeypatch, capsys):
    from kalash.cli._pipe import _execute
    from kalash.cli.output import OutputFormat
    from kalash.core.config import KalashConfig, MemoryConfig
    from kalash.core.events import Event, EventType
    from kalash.runtime.agent import build_agent

    provider = FakeProvider("answer")
    agent, reason = build_agent(
        cwd=workspace,
        provider=provider,
        config=KalashConfig(project_dir=workspace, memory=MemoryConfig(enabled=False)),
        persist=False,
        extensions=False,
    )
    assert agent is not None, reason
    original_send = agent.send
    output = "α" * 1500

    async def send(prompt, **kwargs):
        for kind, data in (
            (EventType.TOOL_START, {"tool_name": "shell", "tool_use_id": "call-1"}),
            (EventType.TOOL_OUTPUT, {"tool_use_id": "call-1", "stream": "stdout", "chunk": output}),
            (
                EventType.TOOL_COMPLETE,
                {
                    "tool_name": "shell",
                    "tool_use_id": "call-1",
                    "ok": False,
                    "exit_code": 0,
                    "error_code": "FAILED",
                    "remediation": "Retry",
                },
            ),
        ):
            await agent.loop.event_bus.emit(Event(kind, data, agent.session_id))
        return await original_send(prompt, **kwargs)

    monkeypatch.setattr(agent, "send", send)
    try:
        await _execute(agent, "hi", OutputFormat.STREAM_JSON, "workspace-write")
    finally:
        await agent.close()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert (
        next(item for item in events if item["event"] == "tool.output")["data"]["chunk"] == output
    )
    completed = next(item for item in events if item["event"] == "tool.completed")["data"]
    assert completed["tool_use_id"] == "call-1"
    assert completed["exit_code"] == 0
    assert completed["remediation"] == "Retry"


@pytest.mark.asyncio
async def test_budget_refusal_has_budget_exit_and_actual_limits(workspace, capsys):
    from kalash.cli._pipe import _execute
    from kalash.cli.output import EXIT_BUDGET_EXCEEDED, OutputFormat
    from kalash.core.config import BudgetConfig, KalashConfig, MemoryConfig
    from kalash.runtime.agent import build_agent

    agent, reason = build_agent(
        cwd=workspace,
        provider=FakeProvider("must not dispatch"),
        persist=False,
        extensions=False,
        config=KalashConfig(
            project_dir=workspace,
            memory=MemoryConfig(enabled=False),
            budget=BudgetConfig(max_tokens=1),
        ),
    )
    assert agent is not None, reason
    try:
        code = await _execute(agent, "hi", OutputFormat.JSON, "workspace-write")
    finally:
        await agent.close()
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_BUDGET_EXCEEDED
    assert payload["error"]["class"] == "budget"
    assert payload["budget"]["tokens"] == {"used": 0, "ceiling": 1}
    assert payload["completion_status"] == "failed"  # No output or effects before admission.
