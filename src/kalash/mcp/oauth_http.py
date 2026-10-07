"""Bounded OAuth token exchange; credentials never follow redirects."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
from typing import Any
from urllib.parse import urlsplit

import httpx


async def token_request(url: str, fields: dict[str, str]) -> dict[str, Any]:
    parsed = urlsplit(url)
    loopback = parsed.hostname == "localhost"
    with contextlib.suppress(ValueError):
        loopback = loopback or ipaddress.ip_address(parsed.hostname or "").is_loopback
    if (
        (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("OAuth token endpoint requires HTTPS or loopback HTTP")
    async with (
        asyncio.timeout(15),
        httpx.AsyncClient(timeout=15, follow_redirects=False) as client,
        client.stream("POST", url, data=fields) as response,
    ):
        response.raise_for_status()
        if response.is_redirect:
            raise ValueError("OAuth token redirects are forbidden")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > 64_000:
                raise ValueError("OAuth token response exceeds 64000 bytes")
            body.extend(chunk)
    data = json.loads(body)
    if not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
        raise ValueError("OAuth response is missing its access token")
    return data
