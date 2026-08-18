"""Plugin manifest parsing and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class Capability:
    """A capability declaration for a plugin.

    Plugins declare what they need access to, whether it's required or optional,
    and a human-readable reason for the capability.
    """

    name: str
    required: bool = True
    reason: str = ""


@dataclass(frozen=True)
class PluginManifest:
    """Parsed plugin manifest describing a Kalash plugin.

    Attributes:
        name: Plugin identifier (e.g. 'my-plugin').
        version: Semantic version string.
        description: Human-readable description.
        author: Plugin author or organization.
        entry_point: Module path or entry point spec.
        capabilities: List of capability declarations.
        provides: What the plugin provides (tools, memory_provider, model_provider, etc.).
        integrity_hash: SHA-256 hash for trust pinning.
        min_kalash_version: Minimum compatible Kalash version.
        homepage: URL to plugin documentation or repo.
        license: SPDX license identifier.
        metadata: Additional arbitrary metadata.
    """

    name: str
    version: str
    description: str = ""
    author: str = ""
    entry_point: str = ""
    capabilities: list[Capability] = field(default_factory=list)
    provides: list[str] = field(default_factory=list)
    integrity_hash: Optional[str] = None
    min_kalash_version: Optional[str] = None
    homepage: Optional[str] = None
    license: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, path: str | Path) -> "PluginManifest":
        """Parse a plugin manifest from a JSON file.

        Args:
            path: Path to the manifest JSON file.

        Returns:
            A validated PluginManifest instance.

        Raises:
            FileNotFoundError: If the manifest file doesn't exist.
            ManifestError: If the manifest is malformed or missing required fields.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Manifest not found: {path}")

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ManifestError(f"Invalid JSON in manifest: {e}") from e

        return cls._from_dict(data, source_path=path)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PluginManifest":
        """Parse a manifest from a dictionary.

        Args:
            data: Manifest data as a dictionary.

        Returns:
            A validated PluginManifest instance.
        """
        return cls._from_dict(data)

    @classmethod
    def _from_dict(
        cls, data: dict[str, Any], source_path: Optional[Path] = None
    ) -> "PluginManifest":
        """Internal parser from dictionary."""
        # Required fields
        name = data.get("name")
        if not name:
            raise ManifestError("Manifest missing required field: 'name'")

        version = data.get("version")
        if not version:
            raise ManifestError("Manifest missing required field: 'version'")

        # Parse capabilities
        raw_caps = data.get("capabilities", [])
        capabilities: list[Capability] = []
        for cap in raw_caps:
            if isinstance(cap, str):
                capabilities.append(Capability(name=cap))
            elif isinstance(cap, dict):
                capabilities.append(
                    Capability(
                        name=cap["name"],
                        required=cap.get("required", True),
                        reason=cap.get("reason", ""),
                    )
                )
            else:
                raise ManifestError(f"Invalid capability entry: {cap!r}")

        return cls(
            name=name,
            version=version,
            description=data.get("description", ""),
            author=data.get("author", ""),
            entry_point=data.get("entry_point", ""),
            capabilities=capabilities,
            provides=data.get("provides", []),
            integrity_hash=data.get("integrity_hash"),
            min_kalash_version=data.get("min_kalash_version"),
            homepage=data.get("homepage"),
            license=data.get("license"),
            metadata=data.get("metadata", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize manifest to a dictionary."""
        result: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
        }

        if self.description:
            result["description"] = self.description
        if self.author:
            result["author"] = self.author
        if self.entry_point:
            result["entry_point"] = self.entry_point
        if self.capabilities:
            result["capabilities"] = [
                {"name": c.name, "required": c.required, "reason": c.reason}
                for c in self.capabilities
            ]
        if self.provides:
            result["provides"] = self.provides
        if self.integrity_hash:
            result["integrity_hash"] = self.integrity_hash
        if self.min_kalash_version:
            result["min_kalash_version"] = self.min_kalash_version
        if self.homepage:
            result["homepage"] = self.homepage
        if self.license:
            result["license"] = self.license
        if self.metadata:
            result["metadata"] = self.metadata

        return result

    def validate_capabilities(
        self, granted: set[str]
    ) -> tuple[list[str], list[str]]:
        """Check which capabilities are satisfied and which are missing.

        Args:
            granted: Set of capability names that have been granted.

        Returns:
            Tuple of (satisfied, missing_required) capability names.
        """
        satisfied: list[str] = []
        missing: list[str] = []

        for cap in self.capabilities:
            if cap.name in granted:
                satisfied.append(cap.name)
            elif cap.required:
                missing.append(cap.name)

        return satisfied, missing


class ManifestError(Exception):
    """Raised when a plugin manifest is invalid or malformed."""
