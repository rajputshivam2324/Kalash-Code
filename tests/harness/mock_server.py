"""A loopback OpenAI-compatible chat server for process-level tests.

The real ``kalash`` console script talks to it over HTTP. Responses are scripted; the
request bodies are recorded; an exhausted script answers HTTP 500 and is reported.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def sse(chunks: list[dict[str, Any]]) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


def text_response(text: str, *, prompt_tokens: int = 50, completion_tokens: int = 10) -> bytes:
    half = max(1, len(text) // 2)
    chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": text[:half]}}]},
        {"choices": [{"index": 0, "delta": {"content": text[half:]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {
            "choices": [],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
    ]
    return sse(chunks)


def tool_call_response(call_id: str, name: str, arguments: dict[str, Any]) -> bytes:
    raw = json.dumps(arguments)
    cut = max(1, len(raw) // 2)
    chunks = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "type": "function",
                                "function": {"name": name, "arguments": raw[:cut]},
                            }
                        ],
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": raw[cut:]}}]},
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 60, "completion_tokens": 15}},
    ]
    return sse(chunks)


class MockLLMServer:
    def __init__(self) -> None:
        self.script: list[tuple[int, bytes, float]] = []
        self.requests: list[dict[str, Any]] = []
        self.paths: list[str] = []  # request target of each POST, query string included
        self.headers: list[dict[str, str]] = []  # lower-cased request headers of each POST
        self.unexpected = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(body)
                outer.paths.append(self.path)
                outer.headers.append({k.lower(): v for k, v in self.headers.items()})
                if not outer.script:
                    outer.unexpected += 1
                    status, payload = 500, b'{"error":{"message":"script exhausted"}}'
                else:
                    status, payload, delay = outer.script.pop(0)
                    if delay:
                        time.sleep(delay)
                self.send_response(status)
                ctype = "text/event-stream" if status == 200 else "application/json"
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def start(self) -> MockLLMServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def reply(self, status: int, payload: bytes, delay: float = 0.0) -> None:
        self.script.append((status, payload, delay))
