"""Provider discovery, registration, and lifecycle management.

Providers are discovered via:
1. Entry points (group: 'kalash.memory_providers')
2. Explicit config in memory.providers
3. Runtime registration (for testing / embedded use)

Each provider is instantiated once and health-checked before first use.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

try:
    import structlog

    logger = structlog.get_logger()
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

from kalash.core.config import MemoryConfig
from kalash.memory.protocol import (
    MemoryProvider,
    ProviderCapability,
    ProviderHealth,
)

# Type for provider factory functions
ProviderFactory = Callable[[dict[str, Any]], MemoryProvider]


@dataclass
class ProviderEntry:
    """Registry entry for a memory provider."""

    name: str
    provider: MemoryProvider
    capabilities: frozenset[ProviderCapability]
    healthy: bool = True
    last_health_check: datetime | None = None
    consecutive_failures: int = 0
    registered_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class MemoryRegistry:
    """Discovers, registers, and manages memory provider lifecycle.

    The registry owns provider instances and exposes them to the router.
    It performs periodic health checks and removes unhealthy providers
    from the active set (without destroying them — they can recover).
    """

    ENTRY_POINT_GROUP = "kalash.memory_providers"
    HEALTH_CHECK_INTERVAL_S = 60.0
    MAX_CONSECUTIVE_FAILURES = 3

    def __init__(self, config: MemoryConfig) -> None:
        self._config = config
        self._providers: dict[str, ProviderEntry] = {}
        self._factories: dict[str, ProviderFactory] = {}
        from kalash.memory.providers.local import create_local_provider

        self._factories["local"] = create_local_provider
        self._health_task: asyncio.Task[None] | None = None

    def discover_entry_points(self) -> dict[str, ProviderFactory]:
        """Scan installed packages for memory provider entry points.

        Entry points should be registered under the 'kalash.memory_providers'
        group and point to a factory function: (config: dict) -> MemoryProvider.
        """
        discovered: dict[str, ProviderFactory] = {}
        try:
            eps = importlib.metadata.entry_points(group=self.ENTRY_POINT_GROUP)
            for ep in eps:
                try:
                    factory = ep.load()
                    discovered[ep.name] = factory
                    logger.info("memory_provider_discovered", name=ep.name, module=ep.value)
                except Exception as exc:
                    logger.warning(
                        "memory_provider_discovery_failed",
                        name=ep.name,
                        error=str(exc),
                    )
        except Exception as exc:
            logger.warning("entry_point_scan_failed", error=str(exc))

        self._factories.update(discovered)
        return discovered

    def register_factory(self, name: str, factory: ProviderFactory) -> None:
        """Register a provider factory for later instantiation."""
        self._factories[name] = factory
        logger.debug("memory_provider_factory_registered", name=name)

    async def register(self, provider: MemoryProvider) -> None:
        """Register an already-instantiated provider."""
        entry = ProviderEntry(
            name=provider.name,
            provider=provider,
            capabilities=provider.capabilities,
        )
        self._providers[provider.name] = entry
        logger.info(
            "memory_provider_registered",
            name=provider.name,
            capabilities=[c.value for c in provider.capabilities],
        )

    async def instantiate(
        self, name: str, provider_config: dict[str, Any] | None = None
    ) -> MemoryProvider:
        """Instantiate a provider from a registered factory.

        Args:
            name: Provider name (must have a factory registered).
            provider_config: Provider-specific configuration dict.

        Returns:
            The instantiated and registered MemoryProvider.

        Raises:
            KeyError: If no factory is registered for the name.
        """
        if name not in self._factories:
            raise KeyError(f"No factory registered for provider '{name}'")

        factory = self._factories[name]
        config = provider_config or {}
        provider = factory(config)
        initialize = getattr(provider, "initialize", None)
        if initialize is not None:
            await initialize()
        await self.register(provider)
        return provider

    async def initialize(self, provider_configs: dict[str, dict[str, Any]] | None = None) -> None:
        """Initialize the registry: discover, instantiate, and health-check.

        Args:
            provider_configs: Per-provider config dicts keyed by provider name.
        """
        # Discover entry points
        self.discover_entry_points()

        # Instantiate configured providers
        configs = provider_configs or {}
        for name in self._config.providers:
            if name in self._providers:
                continue  # Already registered
            if name not in self._factories:
                raise ValueError(f"Unknown memory provider: {name}")
            if name in self._factories:
                try:
                    await self.instantiate(name, configs.get(name))
                except Exception as exc:
                    logger.error(
                        "memory_provider_instantiation_failed",
                        name=name,
                        error=str(exc),
                    )

        # Initial health check
        await self.check_all_health()

    async def check_health(self, name: str) -> ProviderHealth:
        """Check health of a single provider."""
        entry = self._providers.get(name)
        if entry is None:
            return ProviderHealth(provider=name, healthy=False, error="not_registered")

        start = time.perf_counter()
        try:
            health = await asyncio.wait_for(
                entry.provider.health(),
                timeout=5.0,
            )
            latency_ms = (time.perf_counter() - start) * 1000

            entry.healthy = health.healthy
            entry.last_health_check = datetime.now(UTC)
            if health.healthy:
                entry.consecutive_failures = 0
            else:
                entry.consecutive_failures += 1

            logger.debug(
                "memory_provider_health",
                name=name,
                healthy=health.healthy,
                latency_ms=round(latency_ms, 1),
            )
            return health

        except Exception as exc:  # includes TimeoutError
            entry.healthy = False
            entry.consecutive_failures += 1
            entry.last_health_check = datetime.now(UTC)

            error_msg = "timeout" if isinstance(exc, asyncio.TimeoutError) else str(exc)
            logger.warning(
                "memory_provider_health_failed",
                name=name,
                consecutive_failures=entry.consecutive_failures,
                error=error_msg,
            )
            return ProviderHealth(
                provider=name,
                healthy=False,
                latency_ms=(time.perf_counter() - start) * 1000,
                error=error_msg,
            )

    async def check_all_health(self) -> dict[str, ProviderHealth]:
        """Health-check all registered providers concurrently."""
        results: dict[str, ProviderHealth] = {}
        if not self._providers:
            return results

        tasks = {name: asyncio.create_task(self.check_health(name)) for name in self._providers}
        for name, task in tasks.items():
            results[name] = await task

        return results

    def start_health_monitor(self) -> None:
        """Start periodic background health checking."""
        if self._health_task is not None:
            return
        self._health_task = asyncio.create_task(self._health_loop())

    async def _health_loop(self) -> None:
        """Periodic health check loop."""
        while True:
            await asyncio.sleep(self.HEALTH_CHECK_INTERVAL_S)
            try:
                await self.check_all_health()
            except Exception as e:
                logger.error("health_loop_error", exc_info=e)

    def stop_health_monitor(self) -> None:
        """Stop the periodic health monitor."""
        if self._health_task is not None:
            self._health_task.cancel()
            self._health_task = None

    def get(self, name: str) -> MemoryProvider | None:
        """Get a provider by name (None if not registered)."""
        entry = self._providers.get(name)
        return entry.provider if entry else None

    def get_healthy(self) -> list[MemoryProvider]:
        """Get all healthy providers."""
        return [e.provider for e in self._providers.values() if e.healthy]

    def get_primary(self) -> MemoryProvider | None:
        """Get the configured primary provider."""
        return self.get(self._config.primary)

    def get_with_capability(self, capability: ProviderCapability) -> list[MemoryProvider]:
        """Get all healthy providers that declare a capability."""
        return [
            e.provider
            for e in self._providers.values()
            if e.healthy and capability in e.capabilities
        ]

    @property
    def all_providers(self) -> dict[str, ProviderEntry]:
        """All registered providers (including unhealthy)."""
        return dict(self._providers)

    @property
    def provider_names(self) -> list[str]:
        """Names of all registered providers."""
        return list(self._providers.keys())

    def is_healthy(self, name: str) -> bool:
        """Check if a provider is currently healthy."""
        entry = self._providers.get(name)
        return entry.healthy if entry else False

    async def shutdown(self) -> None:
        """Gracefully shut down all providers and stop monitoring."""
        self.stop_health_monitor()
        for entry in self._providers.values():
            close = getattr(entry.provider, "close", None)
            if close is not None:
                await close()
        self._providers.clear()
        self._factories.clear()
        logger.info("memory_registry_shutdown")
