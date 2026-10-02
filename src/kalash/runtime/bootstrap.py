"""Shared startup for CLI, TUI, SDK and scheduler."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kalash.runtime.agent import Agent


async def prepare_agent(agent: Agent) -> None:
    from kalash.core.logging import configure_logging

    configure_logging()
    await agent.prepare()
