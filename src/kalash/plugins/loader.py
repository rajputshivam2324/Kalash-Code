"""Plugin loading with entry point discovery and trust pinning."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

from kalash.plugins.manifest import ManifestError, PluginManifest

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Plugin protocol
# ---------------------------------------------------------------------------


class PluginProtocol(Protocol):
    """Protocol that all Kalash plugins must satisfy."""

    def activate(self, context: "PluginContext") -> None:
        """Called when the plugin is loaded and activated."""
        ...

    def deactivate(self) -> None:
        """Called when the plugin is unloaded."""
        ...


@dataclass
class PluginContext:
    """Context passed to plugins during activation."""

    kalash_version: str
    granted_capabilities: set[str] = field(default_factory=set)
    config: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Entry point groups
# ---------------------------------------------------------------------------

ENTRY_POINT_GROUPS = (
    "kalash.plugins",
    "kalash.memory_providers",
    "kalash.model_providers",
    "kalash.tools",
)


# ---------------------------------------------------------------------------
# Trust store
# ---------------------------------------------------------------------------


@dataclass
class TrustEntry:
    """A pinned hash entry for a trusted plugin."""

    plugin_name: str
    hash_algorithm: str  # "sha256"
    hash_value: str
    pinned_version: Optional[str] = None


class TrustStore:
    """Manages hash-pinned trust for plugins.

    Uses the same trust-and-hash-pinning mechanism as hooks.
    """

    def __init__(self, trust_file: Optional[Path] = None) -> None:
        self._entries: dict[str, TrustEntry] = {}
        self._trust_file = trust_file

        if trust_file and trust_file.exists():
            self._load(trust_file)

    def _load(self, path: Path) -> None:
        """Load trust entries from file."""
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        for entry in data.get("trusted", []):
            te = TrustEntry(
                plugin_name=entry["name"],
                hash_algorithm=entry.get("algorithm", "sha256"),
                hash_value=entry["hash"],
                pinned_version=entry.get("version"),
            )
            self._entries[te.plugin_name] = te

    def verify(self, name: str, module_path: Path) -> bool:
        """Verify a plugin against its pinned hash.

        Returns True if the plugin is trusted (hash matches or not pinned).
        """
        entry = self._entries.get(name)
        if entry is None:
            # Not pinned — trusted by default (first-use)
            return True

        computed = self._compute_hash(module_path, entry.hash_algorithm)
        if computed != entry.hash_value:
            logger.warning(
                "Plugin %r failed hash verification (expected %s, got %s)",
                name,
                entry.hash_value[:16],
                computed[:16],
            )
            return False
        return True

    def pin(self, name: str, module_path: Path, version: Optional[str] = None) -> None:
        """Pin a plugin's current hash into the trust store."""
        hash_value = self._compute_hash(module_path, "sha256")
        self._entries[name] = TrustEntry(
            plugin_name=name,
            hash_algorithm="sha256",
            hash_value=hash_value,
            pinned_version=version,
        )
        self._save()

    def _compute_hash(self, path: Path, algorithm: str) -> str:
        """Compute hash of a file."""
        h = hashlib.new(algorithm)
        h.update(path.read_bytes())
        return h.hexdigest()

    def _save(self) -> None:
        """Persist trust store to disk."""
        if self._trust_file is None:
            return

        import json

        data = {
            "trusted": [
                {
                    "name": e.plugin_name,
                    "algorithm": e.hash_algorithm,
                    "hash": e.hash_value,
                    "version": e.pinned_version,
                }
                for e in self._entries.values()
            ]
        }
        self._trust_file.write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# Plugin loader
# ---------------------------------------------------------------------------


@dataclass
class LoadedPlugin:
    """A successfully loaded plugin instance."""

    manifest: PluginManifest
    module: Any
    instance: PluginProtocol
    entry_point_group: str


class PluginLoader:
    """Discovers, validates, and loads Kalash plugins.

    Supports entry point discovery from installed packages and local plugins.
    Enforces capability checking and trust-and-hash-pinning.
    """

    def __init__(
        self,
        trust_store: Optional[TrustStore] = None,
        granted_capabilities: Optional[set[str]] = None,
    ) -> None:
        self._trust_store = trust_store or TrustStore()
        self._granted_capabilities = granted_capabilities or set()
        self._loaded: dict[str, LoadedPlugin] = {}

    @property
    def loaded_plugins(self) -> dict[str, LoadedPlugin]:
        """Map of plugin name -> loaded plugin info."""
        return dict(self._loaded)

    def discover(self) -> list[PluginManifest]:
        """Discover all available plugins from entry points.

        Scans all registered entry point groups for Kalash plugins.

        Returns:
            List of discovered plugin manifests.
        """
        discovered: list[PluginManifest] = []

        for group in ENTRY_POINT_GROUPS:
            try:
                eps = importlib.metadata.entry_points(group=group)
            except TypeError:
                # Python 3.12 fallback
                eps = importlib.metadata.entry_points().get(group, [])

            for ep in eps:
                try:
                    manifest = self._manifest_from_entry_point(ep, group)
                    discovered.append(manifest)
                except Exception as e:  # includes ManifestError
                    logger.warning(
                        "Failed to load manifest for entry point %s: %s", ep.name, e
                    )

        return discovered

    def load(self, manifest: PluginManifest) -> LoadedPlugin:
        """Load and activate a plugin.

        Args:
            manifest: The plugin manifest to load.

        Returns:
            The loaded plugin instance.

        Raises:
            PluginLoadError: If the plugin fails to load or doesn't pass checks.
        """
        # Check capabilities
        _, missing = manifest.validate_capabilities(self._granted_capabilities)
        if missing:
            raise PluginLoadError(
                f"Plugin {manifest.name!r} requires capabilities not granted: {missing}"
            )

        # Load the module
        try:
            module = importlib.import_module(manifest.entry_point)
        except ImportError as e:
            raise PluginLoadError(
                f"Failed to import plugin {manifest.name!r}: {e}"
            ) from e

        # Verify trust
        module_path = Path(module.__file__) if hasattr(module, "__file__") else None
        if module_path and not self._trust_store.verify(manifest.name, module_path):
            raise PluginLoadError(
                f"Plugin {manifest.name!r} failed trust verification. "
                "Hash does not match pinned value."
            )

        # Instantiate and activate
        plugin_class = getattr(module, "Plugin", None) or getattr(
            module, f"{manifest.name.replace('-', '_').title()}Plugin", None
        )

        if plugin_class is None:
            raise PluginLoadError(
                f"Plugin {manifest.name!r} does not export a Plugin class."
            )

        instance = plugin_class()
        context = PluginContext(
            kalash_version=self._get_kalash_version(),
            granted_capabilities=self._granted_capabilities,
        )
        instance.activate(context)

        loaded = LoadedPlugin(
            manifest=manifest,
            module=module,
            instance=instance,
            entry_point_group=manifest.provides[0] if manifest.provides else "kalash.plugins",
        )

        self._loaded[manifest.name] = loaded

        # Pin hash on first successful load
        if module_path:
            self._trust_store.pin(manifest.name, module_path, manifest.version)

        logger.info("Loaded plugin: %s v%s", manifest.name, manifest.version)
        return loaded

    def unload(self, name: str) -> None:
        """Deactivate and unload a plugin.

        Args:
            name: Plugin name to unload.
        """
        loaded = self._loaded.pop(name, None)
        if loaded:
            try:
                loaded.instance.deactivate()
            except Exception as e:
                logger.warning("Error deactivating plugin %s: %s", name, e)
            logger.info("Unloaded plugin: %s", name)

    def load_all(self) -> list[LoadedPlugin]:
        """Discover and load all available plugins.

        Returns:
            List of successfully loaded plugins.
        """
        manifests = self.discover()
        loaded: list[LoadedPlugin] = []

        for manifest in manifests:
            try:
                loaded.append(self.load(manifest))
            except PluginLoadError as e:
                logger.warning("Skipping plugin %s: %s", manifest.name, e)

        return loaded

    def _manifest_from_entry_point(
        self, ep: importlib.metadata.EntryPoint, group: str
    ) -> PluginManifest:
        """Extract manifest information from an entry point."""
        # Try to load a manifest.json from the package
        dist = ep.dist
        if dist:
            metadata_dir = dist._path if hasattr(dist, "_path") else None  # type: ignore
            if metadata_dir:
                manifest_file = metadata_dir.parent / "kalash_manifest.json"
                if manifest_file.exists():
                    return PluginManifest.parse(manifest_file)

        # Fallback: construct minimal manifest from entry point metadata
        return PluginManifest(
            name=ep.name,
            version=dist.version if dist else "0.0.0",
            entry_point=ep.value,
            provides=[group],
        )

    def _get_kalash_version(self) -> str:
        """Get the current Kalash version."""
        try:
            return importlib.metadata.version("kalash")
        except importlib.metadata.PackageNotFoundError:
            return "0.0.0-dev"


class PluginLoadError(Exception):
    """Raised when a plugin fails to load."""
