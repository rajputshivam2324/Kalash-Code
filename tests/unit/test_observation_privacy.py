"""Credential canaries do not enter persisted observations or outgoing model text."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from kalash.core.budget import BudgetState
from kalash.core.events import Event, EventBus, EventType
from kalash.core.privacy import scrub_metadata, scrub_text
from kalash.core.redact import redact
from kalash.models.normalize import Message, Role, TextBlock, ToolUseBlock
from kalash.runtime.request import request
from kalash.runtime.scratchpad import get_scratchpad, reset_cache
from kalash.runtime.transcript import Transcript
from tests.unit.test_agent_loop import AutoApprover, FakeGateway, make_host, text_turn

CANARY = "fixture-credential-0a9fXQz7v82s"


@pytest.fixture(autouse=True)
def private_env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KALASH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TEST_API_KEY", CANARY)
    reset_cache()


def test_complete_pem_body_and_quoted_json_are_scrubbed() -> None:
    key = "-----BEGIN RSA PRIVATE KEY-----\nunique-private-body\n-----END RSA PRIVATE KEY-----"
    assert "unique-private-body" not in redact(key).text
    assert "unique-private-body" not in redact(key.split("-----END")[0]).text
    assert CANARY not in redact('{"api_key": "' + CANARY + '"}').text
    assert CANARY not in scrub_text("output: " + CANARY)


def test_metadata_preserves_counters_and_redacts_explicit_credentials() -> None:
    original = {"api_key": "low-entropy", "exit_code": 0, "token_count": 17}
    result = scrub_metadata(original)
    assert result["api_key"].startswith("[REDACTED:")
    assert result["exit_code"] == 0
    assert result["token_count"] == 17
    assert original["api_key"] == "low-entropy"


async def test_host_scrubs_before_deferral_without_changing_effects(tmp_path: Any) -> None:
    host = make_host(tmp_path, ui=AutoApprover())
    observed: list[Event] = []

    async def capture(event: Event) -> None:
        observed.append(event)

    host.event_bus.on_all(capture)
    source = tmp_path / "credentials.txt"
    written = await host.execute_result(
        "write", {"path": str(source), "content": CANARY}, tool_use_id="write"
    )
    assert written.evidence["tool"] == "write"
    assert written.evidence["path"] == str(source)
    assert len(written.evidence["content_digest"]) == 64
    assert source.read_text() == CANARY
    result = await host.execute_result("read", {"path": str(source)}, tool_use_id="read")
    assert CANARY not in result.content
    assert CANARY not in str([event.data for event in observed])
    pad = get_scratchpad("ses_it")
    stored = pad.put("other", "canary " + CANARY, ("long " * 2000) + CANARY)
    assert CANARY not in str(stored.observation)
    assert CANARY not in pad.expand(stored.ref, max_bytes=16_384).content
    for path in (tmp_path / "home").rglob("*"):
        if path.is_file():
            assert CANARY.encode() not in path.read_bytes()


async def test_transcript_scrubs_input_copy_and_keeps_protocol_ids() -> None:
    repo = AsyncMock()
    call = ToolUseBlock("call-1", "write", {"content": CANARY})
    await Transcript(repo, "session").append(Message(Role.ASSISTANT, [call]))
    payload = repo.append_turn.call_args.kwargs["content"]
    assert CANARY not in payload
    assert "call-1" in payload
    assert call.input["content"] == CANARY


async def test_request_scrubs_last_boundary_without_mutating_caller() -> None:
    gateway = FakeGateway([text_turn("done")])
    original = Message(Role.USER, [TextBlock(CANARY)])
    await request(
        gateway,
        BudgetState(),
        asyncio.Event(),
        [original],
        system=CANARY,
        tools=None,
        max_output_tokens=100,
        temperature=0,
        on_text_delta=None,
    )
    assert CANARY not in str(gateway.calls)
    assert original.content[0].text == CANARY


async def test_event_subscribers_receive_scrubbed_copies() -> None:
    bus = EventBus()
    received: list[Event] = []

    async def capture(event: Event) -> None:
        received.append(event)

    bus.on_all(capture)
    original = Event(EventType.TOOL_OUTPUT, {"text": CANARY})
    await bus.emit(original)
    assert CANARY not in str(received[0].data)
    assert original.data["text"] == CANARY
