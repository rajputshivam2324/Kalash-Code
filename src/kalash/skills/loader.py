"""SKILL.md discovery and loading with progressive disclosure.

Discovery order: ~/.kalash/skills/ → .kalash/skills/ → plugin-provided.
Only name+description in context initially; body loads on invocation;
references/ load only when body directs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kalash.core.paths import kalash_home


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
        self._loaded.clear()

        # Priority order: user > project > plugins
        # Later discoveries with same name override earlier ones
        sources: list[tuple[Path, str]] = []

        # Plugin-provided (lowest priority)
        for plugin_dir in self._plugin_dirs:
            sources.append((plugin_dir, "plugin"))

        # Project-level
        project_skills = self._project_dir / ".kalash" / "skills"
        sources.append((project_skills, "project"))

        # User-level (highest priority)
        user_skills = kalash_home() / "skills"
        sources.append((user_skills, "user"))

        for skills_dir, source in sources:
            if not skills_dir.is_dir():
                continue

            for skill_file in skills_dir.glob("**/*.md"):
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

        content = entry.path.read_text(encoding="utf-8")
        # Strip frontmatter to get body
        body = self._strip_frontmatter(content)
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
        for ref_file in refs_dir.iterdir():
            if ref_file.is_file():
                try:
                    references[ref_file.name] = ref_file.read_text(encoding="utf-8")
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

    def _parse_frontmatter(self, path: Path, source: str) -> SkillEntry | None:
        """Parse YAML frontmatter from a skill file.

        Expected format:
        ---
        name: skill-name
        description: What this skill does
        version: 1.0.0
        allowed-tools: [tool1, tool2]
        model: anthropic/claude-sonnet-4-5
        agent: code-review
        ---
        """
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

        # Extract frontmatter between --- markers
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if not match:
            return None

        frontmatter_text = match.group(1)
        metadata = self._parse_yaml_simple(frontmatter_text)

        return SkillEntry(
            metadata=SkillMetadata(
                name=metadata.get("name", path.stem),
                description=metadata.get("description", ""),
                version=metadata.get("version", "0.1.0"),
                allowed_tools=metadata.get("allowed-tools", []),
                model=metadata.get("model"),
                agent=metadata.get("agent"),
            ),
            path=path,
            source=source,
        )

    def _parse_yaml_simple(self, text: str) -> dict[str, Any]:
        """Simple YAML-like parser for frontmatter.

        Handles: key: value, key: [list, items].
        Not a full YAML parser — sufficient for skill frontmatter.
        """
        result: dict[str, Any] = {}
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue

            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()

            # Handle list values: [a, b, c]
            if value.startswith("[") and value.endswith("]"):
                items = [
                    item.strip().strip("\"'")
                    for item in value[1:-1].split(",")
                    if item.strip()
                ]
                result[key] = items
            elif value.lower() in ("true", "false"):
                result[key] = value.lower() == "true"
            elif value.isdigit():
                result[key] = int(value)
            elif value == "null" or value == "~" or value == "":
                result[key] = None
            else:
                # Strip quotes
                result[key] = value.strip("\"'")

        return result

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from content, returning the body."""
        match = re.match(r"^---\s*\n.*?\n---\s*\n", content, re.DOTALL)
        if match:
            return content[match.end():]
        return content
