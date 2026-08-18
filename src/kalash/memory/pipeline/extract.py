"""Candidate memory extraction from tool outputs and conversations.

Extractors identify candidate memories from raw content:
- Rule-based extractors handle structured patterns (package.json, Cargo.toml, etc.)
- LLM extraction handles unstructured text (deferred to batch processing)
- Extraction is skipped when a provider declares LLM_EXTRACTION capability
  (the provider does it server-side).

Each extractor returns zero or more MemoryWrite candidates for the router.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Sequence

import structlog

from kalash.memory.protocol import (
    MemoryKind,
    MemoryWrite,
    Provenance,
    ProviderCapability,
    Scope,
    Source,
    Trust,
)

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Extraction result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtractionCandidate:
    """A candidate memory extracted from content."""

    kind: MemoryKind
    content: str
    confidence: Decimal = field(default_factory=lambda: Decimal("0.8"))
    salience: Decimal = field(default_factory=lambda: Decimal("0.5"))
    subject_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    extractor: str = ""  # Name of the extractor that found this


# ---------------------------------------------------------------------------
# Extractor protocol
# ---------------------------------------------------------------------------


class Extractor(ABC):
    """Base class for memory extractors."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique extractor name."""
        ...

    @abstractmethod
    async def extract(self, content: str, context: ExtractionContext) -> list[ExtractionCandidate]:
        """Extract candidate memories from content.

        Args:
            content: Raw text content (tool output, user message, etc.)
            context: Metadata about the content source.

        Returns:
            List of extraction candidates.
        """
        ...


@dataclass(frozen=True, slots=True)
class ExtractionContext:
    """Context about the content being extracted from."""

    source: Source = Source.TOOL_DERIVED
    file_path: str | None = None
    tool_name: str | None = None
    session_id: str | None = None
    turn_seq: int | None = None


# ---------------------------------------------------------------------------
# Rule-based extractors
# ---------------------------------------------------------------------------


class PackageManagerExtractor(Extractor):
    """Detects package manager and dependencies from manifest files."""

    name = "package_manager"

    # Patterns: (regex, kind, subject_key_prefix)
    PATTERNS: list[tuple[str, str, str]] = [
        # Node.js
        (r'"name"\s*:\s*"([^"]+)"', "project_name", "project"),
        (r'"packageManager"\s*:\s*"([^"]+)"', "package_manager", "tooling"),
        # Python
        (r'name\s*=\s*"([^"]+)"', "project_name", "project"),
        (r"requires-python\s*=\s*\"([^\"]+)\"", "python_version", "runtime"),
    ]

    DEPENDENCY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
        (re.compile(r'"dependencies"\s*:\s*\{([^}]+)\}', re.DOTALL), "runtime_deps"),
        (re.compile(r'"devDependencies"\s*:\s*\{([^}]+)\}', re.DOTALL), "dev_deps"),
        (re.compile(r'\[project\.dependencies\]\s*\n((?:[^\[].+\n?)+)'), "runtime_deps"),
    ]

    async def extract(self, content: str, context: ExtractionContext) -> list[ExtractionCandidate]:
        candidates: list[ExtractionCandidate] = []

        for pattern, label, subject_prefix in self.PATTERNS:
            match = re.search(pattern, content)
            if match:
                candidates.append(ExtractionCandidate(
                    kind=MemoryKind.SEMANTIC,
                    content=f"{label}: {match.group(1)}",
                    confidence=Decimal("0.95"),
                    salience=Decimal("0.7"),
                    subject_key=f"{subject_prefix}.{label}",
                    metadata={"extractor_pattern": label},
                    extractor=self.name,
                ))

        # Extract key dependencies
        for pattern, dep_type in self.DEPENDENCY_PATTERNS:
            match = pattern.search(content)
            if match:
                deps_text = match.group(1).strip()
                # Extract just the package names (first 10 most important)
                dep_names = re.findall(r'"([^"@][^"]*)"', deps_text)[:10]
                if dep_names:
                    candidates.append(ExtractionCandidate(
                        kind=MemoryKind.SEMANTIC,
                        content=f"{dep_type}: {', '.join(dep_names)}",
                        confidence=Decimal("0.9"),
                        salience=Decimal("0.6"),
                        subject_key=f"deps.{dep_type}",
                        metadata={"dep_count": len(dep_names)},
                        extractor=self.name,
                    ))

        return candidates


class FrameworkExtractor(Extractor):
    """Detects frameworks and tooling from configuration files."""

    name = "framework"

    # (indicator regex, framework name, kind)
    INDICATORS: list[tuple[str, str, str]] = [
        # Frontend
        (r"(?:next|@next/)", "Next.js", "framework"),
        (r"(?:react|react-dom)", "React", "framework"),
        (r"(?:vue|@vue/)", "Vue.js", "framework"),
        (r"(?:svelte|@sveltejs/)", "Svelte", "framework"),
        (r"(?:angular|@angular/)", "Angular", "framework"),
        # Backend
        (r"(?:fastapi|uvicorn)", "FastAPI", "framework"),
        (r"(?:django)", "Django", "framework"),
        (r"(?:flask)", "Flask", "framework"),
        (r"(?:express)", "Express.js", "framework"),
        (r"(?:nestjs|@nestjs/)", "NestJS", "framework"),
        # Build tools
        (r"(?:vite|@vitejs/)", "Vite", "build_tool"),
        (r"(?:webpack)", "Webpack", "build_tool"),
        (r"(?:turbo|turborepo)", "Turborepo", "build_tool"),
        (r"(?:esbuild)", "esbuild", "build_tool"),
        # Testing
        (r"(?:jest|@jest/)", "Jest", "test_framework"),
        (r"(?:vitest)", "Vitest", "test_framework"),
        (r"(?:pytest)", "pytest", "test_framework"),
        (r"(?:mocha)", "Mocha", "test_framework"),
        # ORM / DB
        (r"(?:prisma|@prisma/)", "Prisma", "orm"),
        (r"(?:sqlalchemy)", "SQLAlchemy", "orm"),
        (r"(?:drizzle)", "Drizzle", "orm"),
        (r"(?:typeorm)", "TypeORM", "orm"),
    ]

    async def extract(self, content: str, context: ExtractionContext) -> list[ExtractionCandidate]:
        candidates: list[ExtractionCandidate] = []
        seen: set[str] = set()

        for pattern, name, category in self.INDICATORS:
            if name in seen:
                continue
            if re.search(pattern, content, re.IGNORECASE):
                seen.add(name)
                candidates.append(ExtractionCandidate(
                    kind=MemoryKind.SEMANTIC,
                    content=f"{category}: {name}",
                    confidence=Decimal("0.85"),
                    salience=Decimal("0.7"),
                    subject_key=f"tooling.{category}.{name.lower().replace('.', '_')}",
                    metadata={"category": category},
                    extractor=self.name,
                ))

        return candidates


class GitExtractor(Extractor):
    """Extracts repository information from git output."""

    name = "git"

    async def extract(self, content: str, context: ExtractionContext) -> list[ExtractionCandidate]:
        candidates: list[ExtractionCandidate] = []

        # Branch detection
        branch_match = re.search(r"On branch (\S+)", content)
        if branch_match:
            candidates.append(ExtractionCandidate(
                kind=MemoryKind.SESSION,
                content=f"current_branch: {branch_match.group(1)}",
                confidence=Decimal("0.99"),
                salience=Decimal("0.4"),
                subject_key="git.branch",
                extractor=self.name,
            ))

        # Remote URL
        remote_match = re.search(r"origin\s+(\S+)\s+\(fetch\)", content)
        if remote_match:
            candidates.append(ExtractionCandidate(
                kind=MemoryKind.SEMANTIC,
                content=f"repository_url: {remote_match.group(1)}",
                confidence=Decimal("0.99"),
                salience=Decimal("0.6"),
                subject_key="git.remote.origin",
                extractor=self.name,
            ))

        return candidates


class ErrorPatternExtractor(Extractor):
    """Extracts error patterns and their resolutions."""

    name = "error_pattern"

    ERROR_INDICATORS = [
        re.compile(r"(?:Error|ERROR|error):\s*(.+)", re.MULTILINE),
        re.compile(r"(?:Exception|EXCEPTION):\s*(.+)", re.MULTILINE),
        re.compile(r"(?:FAILED|failed)\s+(.+)", re.MULTILINE),
        re.compile(r"(?:Cannot|cannot|can't)\s+(.+)", re.MULTILINE),
    ]

    async def extract(self, content: str, context: ExtractionContext) -> list[ExtractionCandidate]:
        candidates: list[ExtractionCandidate] = []
        seen_errors: set[str] = set()

        for pattern in self.ERROR_INDICATORS:
            for match in pattern.finditer(content):
                error_text = match.group(1).strip()[:200]  # Cap length
                if error_text in seen_errors:
                    continue
                seen_errors.add(error_text)

                candidates.append(ExtractionCandidate(
                    kind=MemoryKind.EPISODIC,
                    content=f"error_encountered: {error_text}",
                    confidence=Decimal("0.7"),
                    salience=Decimal("0.6"),
                    subject_key="errors.recent",
                    metadata={"source_tool": context.tool_name},
                    extractor=self.name,
                ))

                # Limit to avoid flooding
                if len(candidates) >= 3:
                    break
            if len(candidates) >= 3:
                break

        return candidates


class UserPreferenceExtractor(Extractor):
    """Extracts stated user preferences from conversation."""

    name = "user_preference"

    PREFERENCE_PATTERNS = [
        (re.compile(r"(?:I prefer|I like|I always use|I want)\s+(.+?)(?:\.|$)", re.IGNORECASE), "preference"),
        (re.compile(r"(?:don't|never|avoid)\s+(.+?)(?:\.|$)", re.IGNORECASE), "anti_preference"),
        (re.compile(r"(?:use|using)\s+(\w+)\s+(?:for|as)\s+(.+?)(?:\.|$)", re.IGNORECASE), "tool_choice"),
    ]

    async def extract(self, content: str, context: ExtractionContext) -> list[ExtractionCandidate]:
        if context.source != Source.USER_STATED:
            return []

        candidates: list[ExtractionCandidate] = []

        for pattern, pref_type in self.PREFERENCE_PATTERNS:
            for match in pattern.finditer(content):
                pref_text = match.group(0).strip()[:150]
                candidates.append(ExtractionCandidate(
                    kind=MemoryKind.SEMANTIC,
                    content=f"user_{pref_type}: {pref_text}",
                    confidence=Decimal("0.9"),
                    salience=Decimal("0.8"),
                    subject_key=f"user.{pref_type}",
                    metadata={"preference_type": pref_type},
                    extractor=self.name,
                ))

        return candidates


# ---------------------------------------------------------------------------
# LLM extraction (stub)
# ---------------------------------------------------------------------------


class LLMExtractor(Extractor):
    """Batch LLM extraction for unstructured content.

    This is a stub — actual LLM calls are deferred to a background batch
    process to avoid blocking the hot path. The extractor queues content
    for later processing.
    """

    name = "llm"

    def __init__(self) -> None:
        self._queue: list[tuple[str, ExtractionContext]] = []

    async def extract(self, content: str, context: ExtractionContext) -> list[ExtractionCandidate]:
        """Queue content for batch LLM extraction. Returns empty immediately.

        Actual extraction happens in a background sweep that processes
        the queue periodically and writes results directly to the router.
        """
        # Only queue substantial content
        if len(content) > 100:
            self._queue.append((content, context))
            logger.debug("llm_extraction_queued", queue_size=len(self._queue))

        return []

    @property
    def pending_count(self) -> int:
        """Number of items queued for batch extraction."""
        return len(self._queue)

    def drain_queue(self) -> list[tuple[str, ExtractionContext]]:
        """Drain the queue for batch processing."""
        items = list(self._queue)
        self._queue.clear()
        return items


# ---------------------------------------------------------------------------
# Extraction pipeline
# ---------------------------------------------------------------------------


# Default extractors in priority order
DEFAULT_EXTRACTORS: list[Extractor] = [
    PackageManagerExtractor(),
    FrameworkExtractor(),
    GitExtractor(),
    ErrorPatternExtractor(),
    UserPreferenceExtractor(),
    LLMExtractor(),
]


async def run_extraction(
    content: str,
    context: ExtractionContext,
    scope: Scope,
    extractors: list[Extractor] | None = None,
    provider_capabilities: frozenset[ProviderCapability] | None = None,
) -> list[MemoryWrite]:
    """Run extraction pipeline on content, returning write candidates.

    Args:
        content: Raw content to extract from.
        context: Source metadata.
        scope: Scope for generated memories.
        extractors: Custom extractor list (defaults to DEFAULT_EXTRACTORS).
        provider_capabilities: If provider has LLM_EXTRACTION, skip LLM extractor.

    Returns:
        List of MemoryWrite intents ready for the router.
    """
    if not content or not content.strip():
        return []

    active_extractors = extractors or DEFAULT_EXTRACTORS

    # Skip LLM extractor if provider handles it
    if provider_capabilities and ProviderCapability.LLM_EXTRACTION in provider_capabilities:
        active_extractors = [e for e in active_extractors if e.name != "llm"]

    # Run all extractors
    all_candidates: list[ExtractionCandidate] = []
    for extractor in active_extractors:
        try:
            candidates = await extractor.extract(content, context)
            all_candidates.extend(candidates)
        except Exception as exc:
            logger.warning(
                "extractor_failed",
                extractor=extractor.name,
                error=str(exc),
            )

    # Convert candidates to MemoryWrite intents
    writes: list[MemoryWrite] = []
    for candidate in all_candidates:
        provenance = Provenance(
            source=context.source,
            trust=Trust.MEDIUM,
            session_id=context.session_id,
            turn_seq=context.turn_seq,
            file_path=context.file_path,
        )

        writes.append(MemoryWrite(
            kind=candidate.kind,
            content=candidate.content,
            scope=scope,
            provenance=provenance,
            confidence=candidate.confidence,
            salience=candidate.salience,
            subject_key=candidate.subject_key,
            metadata={**candidate.metadata, "extractor": candidate.extractor},
        ))

    logger.debug(
        "extraction_complete",
        content_len=len(content),
        candidates=len(all_candidates),
        extractors_run=len(active_extractors),
    )

    return writes
