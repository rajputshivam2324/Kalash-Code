"""Bounded MCP streamable HTTP requests, session headers and SSE responses."""

from __future__ import annotations

import json
from typing import Any

import httpx

MAX_RESPONSE_BYTES = 2_000_000


class HTTPTransport:
    def __init__(self, url: str, timeout: float) -> None:
        self.url = url
        self.client = httpx.AsyncClient(timeout=timeout)
        self.session: str | None = None
        self.protocol = "2025-06-18"

    async def send(self, message: dict[str, Any], headers: dict[str, str]) -> Any:
        headers = {
            **headers,
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol,
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        async with self.client.stream("POST", self.url, json=message, headers=headers) as response:
            response.raise_for_status()
            if message.get("method") == "initialize":
                self.session = response.headers.get("Mcp-Session-Id")
            if "id" not in message:
                return None
            if "text/event-stream" in response.headers.get("Content-Type", ""):
                lines: list[str] = []
                size = 0
                async for line in response.aiter_lines():
                    size += len(line.encode())
                    if size > MAX_RESPONSE_BYTES:
                        raise ValueError("MCP response exceeds size limit")
                    if line.startswith("data:"):
                        lines.append(line[5:].lstrip())
                    elif not line and lines:
                        data = json.loads("\n".join(lines))
                        lines = []
                        if data.get("id") == message["id"]:
                            return self._result(data)
                raise ValueError("MCP stream ended without a matching response")
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_RESPONSE_BYTES:
                    raise ValueError("MCP response exceeds size limit")
            data = json.loads(chunks)
            if data.get("id") != message["id"]:
                raise ValueError("MCP response ID does not match the request")
            return self._result(data)

    @staticmethod
    def _result(data: dict[str, Any]) -> Any:
        if "error" in data:
            raise ValueError(f"MCP server error: {data['error']}")
        return data.get("result")

    async def close(self, headers: dict[str, str]) -> None:
        try:
            if self.session:
                await self.client.delete(
                    self.url,
                    headers={
                        **headers,
                        "Mcp-Session-Id": self.session,
                        "MCP-Protocol-Version": self.protocol,
                    },
                )
        except httpx.HTTPError:
            pass
        finally:
            await self.client.aclose()
