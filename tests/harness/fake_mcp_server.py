"""A tiny MCP stdio server for tests. Behaviour is selected with the MODE environment variable."""

from __future__ import annotations

import json
import os
import sys
import time

MODE = os.environ.get("MODE", "normal")
PAGES = [
    [
        {
            "name": "echo",
            "description": "Echo text",
            "annotations": {"readOnlyHint": True},
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        }
    ],
    [
        {"name": "big", "description": "Huge result", "inputSchema": {"type": "object"}},
        {"name": "fail", "description": "Always fails", "inputSchema": {"type": "object"}},
        {"name": "slow", "description": "Sleeps", "inputSchema": {"type": "object"}},
    ],
]


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> None:
    if MODE == "crash_on_start":
        sys.stderr.write("fatal: cannot bind socket\n")
        sys.exit(3)
    for line in sys.stdin:
        message = json.loads(line)
        method, request_id = message.get("method"), message.get("id")
        if method == "initialize":
            version = (
                "1999-01-01" if MODE == "bad_version" else message["params"]["protocolVersion"]
            )
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": version,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake", "version": "1"},
                    },
                }
            )
        elif method == "tools/list":
            cursor = message.get("params", {}).get("cursor")
            page = 1 if cursor == "p2" else 0
            result: dict = {"tools": PAGES[page]}
            if page == 0:
                result["nextCursor"] = "p2"
            send({"jsonrpc": "2.0", "id": request_id, "result": result})
        elif method == "tools/call":
            name = message["params"]["name"]
            args = message["params"].get("arguments", {})
            # A request FROM the server whose id equals ours must not be mistaken for a response.
            send({"jsonrpc": "2.0", "id": request_id, "method": "ping"})
            print("stray non-json line on stdout", flush=True)
            if name == "echo":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [{"type": "text", "text": f"echo: {args.get('text')}"}]
                        },
                    }
                )
            elif name == "big":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [{"type": "text", "text": "x" * 200_000 + "THE-END"}]
                        },
                    }
                )
            elif name == "slow":
                time.sleep(30)
            else:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "isError": True,
                            "content": [{"type": "text", "text": "it failed"}],
                        },
                    }
                )
        elif request_id is not None and method is None:
            continue  # response to our ping
        elif request_id is not None:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"unknown method {method}"},
                }
            )


main()
