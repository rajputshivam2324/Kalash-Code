"""SKILL.md discovery and loading with progressive disclosure.

Discovery order: ~/.kalash/skills/ → .kalash/skills/ → plugin-provided.
Only name+description in context initially; body loads on invocation;
references/ load only when body directs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from kalash.core.frontmatter import read_body, read_metadata
from kalash.core.paths import kalash_home

logger = logging.getLogger(__name__)


@dataclass
class SkillMetadata:
    """YAML frontmatter parsed from a SKILL.md file."""

    name: str = ""
    description: str = ""
    version: str = "0.1.0"
    allowed_tools: list[str] = field(default_factory=list)
    model: str | None = None  # preferred model override
    agent: str | None = None  # dedicated agent type


@dataclass
class SkillEntry:
    """A loaded skill with progressive disclosure levels."""

    metadata: SkillMetadata
    path: Path
    source: str  # "user" | "project" | "plugin"

    # Lazy-loaded content
    _body: str | None = None
    _references: dict[str, str] = field(default_factory=dict)

    @property
    def body(self) -> str | None:
        """The full skill body (loaded on first access)."""
        return self._body

    @property
    def is_loaded(self) -> bool:
        """Whether the full body has been loaded."""
        return self._body is not None


class SkillLoader:
    """Discovers and loads SKILL.md files with progressive disclosure.

    Skills are discovered from multiple locations with precedence:
    1. ~/.kalash/skills/ (user-level)
    2. .kalash/skills/ (project-level)
    3. Plugin-provided paths
    """

    def __init__(self, project_dir: Path | None = None) -> None:
        self._project_dir = project_dir or Path.cwd()
        self._plugin_dirs: list[Path] = []
        self._loaded: dict[str, SkillEntry] = {}  # name → entry

    def add_plugin_skills_dir(self, path: Path) -> None:
        """Register an additional skills directory from a plugin."""
        self._plugin_dirs.append(path)

    async def discover(self) -> dict[str, SkillEntry]:
        """Discover all available skills across all sources.

        Only parses frontmatter (name+description) for catalog.
        Body is not loaded until explicitly requested.
        """
        return self.discover_now()

    def discover_now(self) -> dict[str, SkillEntry]:
        """Synchronous discovery.

        Discovery is directory globbing and frontmatter parsing with no I/O
        concurrency, so it needs no event loop. Exposing it synchronously lets
        agent assembly build the catalog without one.
        """
        self._loaded.clear()

        # Project definitions override user definitions; plugins have lowest priority.
        # Later discoveries with same name override earlier ones
        sources: list[tuple[Path, str]] = []

        # Plugin-provided (lowest priority)
        for plugin_dir in self._plugin_dirs:
            sources.append((plugin_dir, "plugin"))

        sources.extend(
            [
                (kalash_home() / "skills", "user"),
                (self._project_dir / ".kalash" / "skills", "project"),
            ]
        )

        for skills_dir, source in sources:
            if not skills_dir.is_dir():
                continue

            for skill_file in sorted(skills_dir.glob("**/SKILL.md")):
                entry = self._parse_frontmatter(skill_file, source)
                if entry and entry.metadata.name:
                    self._loaded[entry.metadata.name] = entry

        return dict(self._loaded)

    async def load_body(self, skill_name: str) -> str | None:
        """Load the full body of a skill (invocation-time).

        Returns the body text, or None if skill not found.
        """
        entry = self._loaded.get(skill_name)
        if entry is None:
            return None

        if entry.is_loaded:
            return entry.body

        body = read_body(entry.path)
        entry._body = body
        return body

    async def load_references(self, skill_name: str) -> dict[str, str]:
        """Load reference files for a skill.

        References are in a references/ subdirectory next to the skill file.
        Only loaded when the body directs (progressive disclosure).
        """
        entry = self._loaded.get(skill_name)
        if entry is None:
            return {}

        refs_dir = entry.path.parent / "references"
        if not refs_dir.is_dir():
            return {}

        references: dict[str, str] = {}
        remaining = 40_000
        for ref_file in sorted(refs_dir.iterdir()):
            if ref_file.is_file() and remaining > 0:
                if not ref_file.resolve().is_relative_to(entry.path.parent.resolve()):
                    continue
                try:
                    with ref_file.open(encoding="utf-8") as stream:
                        text = stream.read(min(remaining, 20_000))
                    references[ref_file.name] = text
                    remaining -= len(text)
                except (OSError, UnicodeDecodeError):
                    continue

        entry._references = references
        return references

    def get_entry(self, skill_name: str) -> SkillEntry | None:
        """Get a skill entry by name."""
        return self._loaded.get(skill_name)

    def list_names(self) -> list[str]:
        """List all discovered skill names."""
        return list(self._loaded.keys())

    def validate_skill(self, skill_dir: Path) -> list[str]:
        """Return validation errors for a skill directory, or [] if valid."""
        errors: list[str] = []
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            return [f"missing SKILL.md in {skill_dir}"]

        entry = self._parse_frontmatter(skill_md, "project")
        if entry is None:
            return ["could not parse SKILL.md frontmatter"]

        name = entry.metadata.name
        if not re.fullmatch(r"[a-z0-9-]{1,64}", name):
            errors.append(f"name {name!r} must match [a-z0-9-] (1–64 chars)")
        if skill_dir.name != name:
            errors.append(f"directory {skill_dir.name!r} must match skill name {name!r}")
        if not entry.metadata.description.strip():
            errors.append("description is required (1–1024 chars)")
        elif len(entry.metadata.description) > 1024:
            errors.append("description exceeds 1024 characters")
        return errors

    def _parse_frontmatter(self, path: Path, source: str) -> SkillEntry | None:
        try:
            metadata = read_metadata(path)
            name = metadata.get("name") or path.parent.name
            description = metadata.get("description", "")
            tools = metadata.get("allowed-tools") or []
            if isinstance(tools, str):
                tools = tools.split()
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", name):
                raise ValueError("Invalid skill name")
            if not isinstance(description, str) or not description.strip():
                raise ValueError("Skill description is required")
            if len(description) > 1024 or not isinstance(tools, list):
                raise ValueError("Invalid skill metadata")
            return SkillEntry(
                metadata=SkillMetadata(
                    name=name,
                    description=description,
                    version=str(metadata.get("version", "0.1.0")),
                    allowed_tools=[str(tool) for tool in tools],
                    model=metadata.get("model"),
                    agent=metadata.get("agent"),
                ),
                path=path,
                source=source,
            )
        except (OSError, UnicodeError, ValueError, yaml.YAMLError):
            logger.warning("Skipping invalid skill: %s", path, exc_info=True)
            return None
