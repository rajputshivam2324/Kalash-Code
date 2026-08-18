"""Tests for headless JSON output format."""

import json

import pytest


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

    async def stream(self, messages, *, system=None, tools=None, **kwargs):
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
    provider = FakeProvider("hello json")
    monkeypatch.setattr(
        "kalash.models.resolve.build_provider",
        lambda *a, **k: FakeResolution(provider),
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


@pytest.mark.asyncio
async def test_stream_json_emits_events(workspace, monkeypatch, capsys):
    provider = FakeProvider("streamed")
    monkeypatch.setattr(
        "kalash.models.resolve.build_provider",
        lambda *a, **k: FakeResolution(provider),
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
