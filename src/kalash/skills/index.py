"""Skill catalog with token-budget-aware compression levels.

Maintains name→SkillEntry mapping. Provides progressive compression
for context budget management: full → name+description → names only → omitted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from kalash.core.budget import estimate_tokens
from kalash.skills.loader import SkillEntry, SkillLoader


class CompressionLevel(StrEnum):
    """Compression levels for skill catalog in context."""

    FULL = "full"  # name + description + body excerpt
    SUMMARY = "summary"  # name + description only
    NAMES_ONLY = "names_only"  # just skill names
    OMITTED = "omitted"  # nothing (budget too tight)


@dataclass
class CatalogStats:
    """Statistics about the skill catalog."""

    total_skills: int = 0
    loaded_bodies: int = 0
    total_token_cost: int = 0
    compression_level: CompressionLevel = CompressionLevel.SUMMARY


class SkillIndex:
    """Skill catalog with token-budget-aware context contribution.

    Progressive disclosure:
    - Discovery: only names + descriptions loaded
    - Invocation: full body loaded on demand
    - Context: compression level chosen based on available budget
    """

    def __init__(self, loader: SkillLoader) -> None:
        self._loader = loader
        self._entries: dict[str, SkillEntry] = {}
        self._active: set[str] = set()  # currently active (body loaded) skills

    async def refresh(self) -> None:
        """Refresh the catalog from all discovery sources."""
        self._entries = await self._loader.discover()

    def get(self, name: str) -> SkillEntry | None:
        """Get a skill entry by name."""
        return self._entries.get(name)

    def list_entries(self) -> list[SkillEntry]:
        """List all skill entries."""
        return list(self._entries.values())

    def list_names(self) -> list[str]:
        """List all skill names."""
        return list(self._entries.keys())

    async def activate(self, name: str) -> str | None:
        """Activate a skill by loading its body.

        Returns the body text, or None if not found.
        """
        body = await self._loader.load_body(name)
        if body is not None:
            self._active.add(name)
        return body

    async def deactivate(self, name: str) -> None:
        """Deactivate a skill (release body from active set)."""
        self._active.discard(name)

    def is_active(self, name: str) -> bool:
        """Check if a skill is currently active (body loaded)."""
        return name in self._active

    def token_cost(self, level: CompressionLevel | None = None) -> int:
        """Estimate token cost for the catalog at a given compression level.

        Args:
            level: Compression level to estimate. If None, uses current best.

        Returns:
            Estimated token count for catalog contribution.
        """
        if level == CompressionLevel.OMITTED:
            return 0

        if level == CompressionLevel.NAMES_ONLY:
            text = "\n".join(self._entries.keys())
            return estimate_tokens(text, mode="prose")

        if level == CompressionLevel.SUMMARY or level is None:
            lines: list[str] = []
            for entry in self._entries.values():
                lines.append(f"- {entry.metadata.name}: {entry.metadata.description}")
            text = "\n".join(lines)
            return estimate_tokens(text, mode="prose")

        if level == CompressionLevel.FULL:
            lines = []
            for entry in self._entries.values():
                lines.append(f"## {entry.metadata.name}")
                lines.append(entry.metadata.description)
                if entry.is_loaded and entry.body:
                    # Include first 500 chars of body
                    lines.append(entry.body[:500])
                lines.append("")
            text = "\n".join(lines)
            return estimate_tokens(text, mode="prose")

        return 0

    def choose_compression(self, available_tokens: int) -> CompressionLevel:
        """Choose the best compression level that fits the token budget.

        Tries from most detailed to least, returning the first that fits.
        """
        for level in (
            CompressionLevel.FULL,
            CompressionLevel.SUMMARY,
            CompressionLevel.NAMES_ONLY,
        ):
            cost = self.token_cost(level)
            if cost <= available_tokens:
                return level

        return CompressionLevel.OMITTED

    def render_for_context(self, available_tokens: int) -> str:
        """Render the catalog for inclusion in model context.

        Automatically chooses the best compression level that fits.
        """
        level = self.choose_compression(available_tokens)

        if level == CompressionLevel.OMITTED:
            return ""

        if level == CompressionLevel.NAMES_ONLY:
            names = ", ".join(sorted(self._entries.keys()))
            return f"Available skills: {names}"

        if level == CompressionLevel.SUMMARY:
            lines = ["Available skills:"]
            for entry in sorted(self._entries.values(), key=lambda e: e.metadata.name):
                lines.append(f"- {entry.metadata.name}: {entry.metadata.description}")
            return "\n".join(lines)

        if level == CompressionLevel.FULL:
            lines = ["Available skills:"]
            for entry in sorted(self._entries.values(), key=lambda e: e.metadata.name):
                lines.append(f"\n## {entry.metadata.name}")
                lines.append(entry.metadata.description)
                if entry.is_loaded and entry.body:
                    preview = entry.body[:500]
                    if len(entry.body) > 500:
                        preview += "\n..."
                    lines.append(preview)
            return "\n".join(lines)

        return ""

    def stats(self) -> CatalogStats:
        """Get catalog statistics."""
        loaded = sum(1 for e in self._entries.values() if e.is_loaded)
        return CatalogStats(
            total_skills=len(self._entries),
            loaded_bodies=loaded,
            total_token_cost=self.token_cost(CompressionLevel.SUMMARY),
            compression_level=CompressionLevel.SUMMARY,
        )
