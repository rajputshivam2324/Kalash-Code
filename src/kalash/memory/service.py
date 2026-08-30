"""Memory subsystem bootstrap — single entry point for runtime and CLI."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

try:
    import structlog
    logger = structlog.get_logger()
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from kalash.core.config import KalashConfig, load_config
from kalash.memory.registry import MemoryRegistry
from kalash.memory.router import MemoryRouter, ReadPolicy, WritePolicy


_service: MemoryService | None = None
_init_lock = asyncio.Lock()


@dataclass
class MemoryService:
    """Initialized memory stack: registry + router."""

    registry: MemoryRegistry
    router: MemoryRouter
    config: KalashConfig


def _policy_from_config(config: KalashConfig) -> tuple[WritePolicy, ReadPolicy]:
    write_name = config.memory.write_policy.replace("-", "_")
    read_name = config.memory.read_policy.replace("-", "_")
    return WritePolicy(write_name), ReadPolicy(read_name)


async def get_memory_service(*, config: KalashConfig | None = None) -> MemoryService:
    """Return the process-wide memory service, initializing on first use."""
    global _service
    if _service is not None:
        return _service

    async with _init_lock:
        if _service is not None:
            return _service

        cfg = config or load_config()
        if not cfg.memory.enabled:
            raise RuntimeError("memory layer is disabled")

        registry = MemoryRegistry(cfg.memory)
        await registry.initialize(cfg.memory.provider_configs)

        write_policy, read_policy = _policy_from_config(cfg)
        router = MemoryRouter(
            registry,
            write_policy=write_policy,
            read_policy=read_policy,
        )
        _service = MemoryService(registry=registry, router=router, config=cfg)
        logger.info(
            "memory_service_ready",
            providers=[p.name for p in registry.get_healthy()],
            primary=cfg.memory.primary,
        )
        return _service


async def reset_memory_service() -> None:
    """Clear the singleton (tests only)."""
    global _service
    async with _init_lock:
        _service = None
