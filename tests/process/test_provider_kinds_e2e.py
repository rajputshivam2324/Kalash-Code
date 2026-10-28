"""Every wire format, end to end: the real ``kalash -p`` command, a real tool call, a loopback server.

Unit tests prove each adapter parses and builds payloads; this tier proves the *wiring*: that a
provider ``kind`` reaches the right adapter, URL and auth header, that streaming survives real
HTTP, that the tool result goes back in that format's shape, and that the file really got written.
Payloads follow each provider's documented streaming format (see the adapters' module docs); the
real services were not called.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from kalash.paths import kalash_home
from tests.harness.mock_server import MockLLMServer, text_response, tool_call_response
from tests.harness.wire import sse_bytes

ARGS = {"file_path": "out.txt", "content": "written over the wire\n"}


# -- OpenAI Responses ------------------------------------------------------------------------


def responses_tool_call() -> bytes:
    item = {
        "id": "fc_1",
        "type": "function_call",
        "call_id": "call_1",
        "name": "write",
        "arguments": json.dumps(ARGS),
        "status": "completed",
    }
    usage = {"input_tokens": 40, "output_tokens": 12, "total_tokens": 52}
    return sse_bytes(
        [
            {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**item, "arguments": "", "status": "in_progress"},
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_1",
                "output_index": 0,
                "delta": json.dumps(ARGS),
            },
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_1",
                    "status": "completed",
                    "output": [item],
                    "usage": usage,
                },
            },
        ],
        named=True,
    )


def responses_text(text: str) -> bytes:
    message = {
        "id": "msg_1",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    return sse_bytes(
        [
            {"type": "response.created", "response": {"id": "resp_2", "status": "in_progress"}},
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**message, "status": "in_progress", "content": []},
            },
            {
                "type": "response.output_text.delta",
                "item_id": "msg_1",
                "output_index": 0,
                "content_index": 0,
                "delta": text,
            },
            {"type": "response.output_item.done", "output_index": 0, "item": message},
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_2",
                    "status": "completed",
                    "output": [message],
                    "usage": {"input_tokens": 60, "output_tokens": 5, "total_tokens": 65},
                },
            },
        ],
        named=True,
    )


# -- Anthropic Messages ----------------------------------------------------------------------


def anthropic_events(blocks: list[dict[str, Any]], stop: str) -> bytes:
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [],
                "usage": {"input_tokens": 40, "output_tokens": 1},
            },
        }
    ]
    for index, block in enumerate(blocks):
        if block["type"] == "tool_use":
            start = {**block, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        else:
            start = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
        events += [
            {"type": "content_block_start", "index": index, "content_block": start},
            {"type": "content_block_delta", "index": index, "delta": delta},
            {"type": "content_block_stop", "index": index},
        ]
    events += [
        {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 12}},
        {"type": "message_stop"},
    ]
    return sse_bytes(events, named=True)


def anthropic_tool_call() -> bytes:
    block = {"type": "tool_use", "id": "toolu_1", "name": "write", "input": ARGS}
    return anthropic_events([block], "tool_use")


def anthropic_text(text: str) -> bytes:
    return anthropic_events([{"type": "text", "text": text}], "end_turn")


# -- Gemini ----------------------------------------------------------------------------------


def gemini_chunk(parts: list[dict[str, Any]]) -> bytes:
    return sse_bytes(
        [
            {
                "candidates": [
                    {"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}
                ],
                "usageMetadata": {
                    "promptTokenCount": 40,
                    "candidatesTokenCount": 12,
                    "totalTokenCount": 52,
                },
            }
        ]
    )


def gemini_tool_call() -> bytes:
    return gemini_chunk([{"functionCall": {"name": "write", "args": ARGS}}])


def gemini_text(text: str) -> bytes:
    return gemini_chunk([{"text": text}])


# -- the matrix ------------------------------------------------------------------------------

DONE = "All done over the wire."


class Case:
    def __init__(
        self,
        kind: str,
        base_suffix: str,
        first: bytes,
        second: bytes,
        path_has: str,
        auth_header: str,
    ) -> None:
        self.kind, self.base_suffix = kind, base_suffix
        self.first, self.second = first, second
        self.path_has, self.auth_header = path_has, auth_header


CASES = {
    "responses": Case(
        "responses",
        "/v1",
        responses_tool_call(),
        responses_text(DONE),
        "/responses",
        "authorization",
    ),
    "anthropic": Case(
        "anthropic", "", anthropic_tool_call(), anthropic_text(DONE), "/v1/messages", "x-api-key"
    ),
    "gemini": Case(
        "gemini",
        "/v1beta",
        gemini_tool_call(),
        gemini_text(DONE),
        ":streamGenerateContent",
        "x-goog-api-key",
    ),
    "chat": Case(
        "openai",
        "/v1",
        tool_call_response("call_1", "write", ARGS),
        text_response(DONE),
        "/chat/completions",
        "authorization",
    ),
}


def tool_result_shape_present(kind: str, body: dict[str, Any]) -> bool:
    """Did the second request carry the tool's result in this wire format's own shape?"""
    text = json.dumps(body)
    if kind == "responses":
        return '"function_call_output"' in text and '"call_1"' in text
    if kind == "anthropic":
        return '"tool_result"' in text and '"toolu_1"' in text
    if kind == "gemini":
        return '"functionResponse"' in text and '"write"' in text
    return '"role": "tool"' in text and '"call_1"' in text


@pytest.mark.parametrize("name", list(CASES))
def test_a_tool_task_completes_over_each_wire_format(name: str, tmp_path: Path):
    case = CASES[name]
    server = MockLLMServer().start()
    try:
        host = server.base_url.removesuffix("/v1")
        kalash_home().mkdir(parents=True, exist_ok=True)
        (kalash_home() / "settings.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "wire": {
                            "kind": case.kind,
                            "base_url": host + case.base_suffix,
                            "models": ["m"],
                            "api_key_env": "WIRE_TEST_KEY",
                        }
                    }
                }
            )
        )
        server.reply(200, case.first)
        server.reply(200, case.second)
        work = tmp_path / "proj"
        work.mkdir()
        env = {
            **{k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")},
            "KALASH_HOME": str(kalash_home()),
            "HOME": str(tmp_path),
            "WIRE_TEST_KEY": "test-key-123",
        }
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "kalash",
                "-p",
                "write out.txt",
                "-m",
                "wire/m",
                "--output-format",
                "json",
            ],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr + proc.stdout
        result = json.loads(proc.stdout)
        assert result["reason"] == "completed" and result["result"] == DONE
        assert result["tool_calls"] == 1 and result["steps"] == 2
        assert (work / "out.txt").read_text() == "written over the wire\n"

        assert server.unexpected == 0 and len(server.requests) == 2
        assert all(case.path_has in path for path in server.paths), server.paths
        headers = server.headers[0]
        assert "test-key-123" in headers.get(case.auth_header, ""), sorted(headers)
        if name == "anthropic":
            assert headers.get("anthropic-version")
        assert tool_result_shape_present(case.kind, server.requests[1]), server.requests[1]
        assert result["usage"]["input_tokens"] > 0, "usage was read from the stream"
    finally:
        server.stop()
