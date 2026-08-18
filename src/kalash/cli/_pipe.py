"""Headless mode — reads a prompt from stdin or argv, streams to stdout.

Uses the same provider resolution as the TUI, so a provider connected with
`/connect` works here too. Diagnostics go to stderr and data to stdout, so the
command composes in a pipeline.
"""

from __future__ import annotations

import asyncio
import sys


def run_pipe_mode(prompt: str | None = None) -> None:
    """Entry point for `kalash -p`."""
    text = prompt or _read_prompt()
    if not text:
        print("kalash: no prompt supplied", file=sys.stderr)
        raise SystemExit(2)

    raise SystemExit(asyncio.run(_run(text)))


def _read_prompt() -> str:
    """Read the prompt from stdin when it is piped in."""
    if sys.stdin.isatty():
        return ""
    return sys.stdin.read().strip()


async def _run(prompt: str) -> int:
    from kalash.models.normalize import (
        BlockDelta,
        Message,
        MessageStop,
        Role,
        StreamError,
        TextBlock,
    )
    from kalash.models.resolve import build_provider

    resolution = build_provider()
    if not resolution.ok:
        print(f"kalash: {resolution.reason}", file=sys.stderr)
        return 3

    from kalash.models.gateway import ModelGateway

    gateway = ModelGateway(primary=resolution.provider)
    messages = [Message(role=Role.USER, content=[TextBlock(text=prompt)])]
    system = "You are Kalash, a terminal coding assistant. Be direct and concise."

    wrote_output = False
    try:
        async for event in gateway.stream(messages, system=system, max_tokens=4096):
            if isinstance(event, BlockDelta):
                sys.stdout.write(event.delta)
                sys.stdout.flush()
                wrote_output = True
            elif isinstance(event, StreamError):
                if wrote_output:
                    sys.stdout.write("\n")
                print(f"kalash: {event.error}", file=sys.stderr)
                return 1
            elif isinstance(event, MessageStop):
                break
    except KeyboardInterrupt:
        if wrote_output:
            sys.stdout.write("\n")
        print("kalash: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        if wrote_output:
            sys.stdout.write("\n")
        print(f"kalash: {exc}", file=sys.stderr)
        return 1

    if wrote_output:
        sys.stdout.write("\n")
        sys.stdout.flush()
    else:
        print("kalash: model returned no output", file=sys.stderr)
        return 1

    return 0
