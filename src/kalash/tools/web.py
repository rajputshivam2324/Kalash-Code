"""Web tools: fetch and web_search.

- FetchTool: URL -> text, HTTPS enforced, size caps, redirect limits, content wrapped as untrusted
- WebSearchTool: external search with untrusted results
- Uses httpx for async HTTP
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from kalash.tools.base import (
    SideEffect,
    ToolContext,
    ToolEnvelope,
    TruncationInfo,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_RESPONSE_BYTES = 5_242_880  # 5 MiB
_MAX_REDIRECTS = 5
_REQUEST_TIMEOUT_S = 30.0
_ALLOWED_CONTENT_TYPES = frozenset({
    "text/html",
    "text/plain",
    "text/css",
    "text/javascript",
    "application/json",
    "application/xml",
    "text/xml",
    "text/markdown",
    "text/csv",
})


# ---------------------------------------------------------------------------
# FetchTool
# ---------------------------------------------------------------------------


class FetchParams(BaseModel):
    """Parameters for URL fetching."""

    url: str = Field(description="URL to fetch (HTTP auto-upgraded to HTTPS)")
    mode: str = Field(
        default="truncated",
        description="Fetch mode: 'truncated' (first 8KB), 'full', or 'selective'",
    )
    search_phrase: str = Field(
        default="",
        description="For 'selective' mode: only return sections containing this phrase",
    )
    max_bytes: int = Field(
        default=_MAX_RESPONSE_BYTES,
        gt=0,
        le=_MAX_RESPONSE_BYTES,
        description="Maximum response bytes to accept",
    )


class FetchTool:
    """Fetches URL content as text. HTTPS enforced, size capped, untrusted content."""

    @property
    def name(self) -> str:
        return "fetch"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return (
            "Fetch text content from a URL. HTTP is auto-upgraded to HTTPS. "
            "Content is treated as untrusted. Max 5 MiB."
        )

    @property
    def params(self) -> type[BaseModel]:
        return FetchParams

    @property
    def side_effect(self) -> SideEffect:
        return SideEffect.READ

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"network.fetch"})

    @property
    def timeout_s(self) -> float:
        return _REQUEST_TIMEOUT_S

    @property
    def max_output_bytes(self) -> int:
        return _MAX_RESPONSE_BYTES

    @property
    def idempotent(self) -> bool:
        return True

    @property
    def cancellable(self) -> bool:
        return True

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:
        return frozenset()

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        assert isinstance(args, FetchParams)

        try:
            import httpx
        except ImportError:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message="httpx is not installed. Install with: pip install httpx",
                recoverable=False,
            )

        # Enforce HTTPS
        url = args.url
        if url.startswith("http://"):
            url = "https://" + url[7:]
        elif not url.startswith("https://"):
            url = "https://" + url

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                max_redirects=_MAX_REDIRECTS,
                timeout=httpx.Timeout(_REQUEST_TIMEOUT_S),
            ) as client:
                response = await client.get(url)

                # Verify final URL is still HTTPS after redirects
                if not str(response.url).startswith("https://"):
                    return ToolEnvelope.fail(
                        code="KALASH_TOOL_ERROR",
                        message="Redirect landed on non-HTTPS URL — refused for security.",
                        recoverable=False,
                    )

                # Check content type
                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if content_type and not any(ct in content_type for ct in _ALLOWED_CONTENT_TYPES):
                    return ToolEnvelope.fail(
                        code="KALASH_TOOL_ERROR",
                        message=f"Unsupported content type: {content_type}",
                        recoverable=True,
                        remediation="Only text-based content types are supported.",
                    )

                # Size check
                content_length = len(response.content)
                if content_length > args.max_bytes:
                    return ToolEnvelope.fail(
                        code="KALASH_TOOL_ERROR",
                        message=f"Response too large: {content_length} bytes (max {args.max_bytes})",
                        recoverable=True,
                        remediation="Try 'truncated' mode or reduce max_bytes.",
                    )

                text = response.text

        except httpx.TooManyRedirects:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"Too many redirects (>{_MAX_REDIRECTS})",
                recoverable=True,
            )
        except httpx.TimeoutException:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_TIMEOUT",
                message=f"Request timed out after {_REQUEST_TIMEOUT_S}s",
                recoverable=True,
            )
        except httpx.HTTPError as exc:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"HTTP error: {exc}",
                recoverable=True,
            )

        # Apply mode
        truncated = False
        if args.mode == "truncated":
            max_chars = 8192
            if len(text) > max_chars:
                text = text[:max_chars]
                truncated = True
        elif args.mode == "selective" and args.search_phrase:
            text = _extract_selective(text, args.search_phrase)
            if not text:
                text = "(No sections matched the search phrase.)"

        # Mark content as untrusted
        content = f"[UNTRUSTED EXTERNAL CONTENT]\n{text}"

        truncation = (
            TruncationInfo(
                total_lines=text.count("\n"),
                shown_lines=content.count("\n"),
                total_bytes=content_length,
                retrieval_hint="Use 'full' mode to get complete content.",
            )
            if truncated
            else None
        )

        return ToolEnvelope.success(
            content=content,
            metadata={
                "url": str(url),
                "status_code": response.status_code,
                "content_type": content_type,
                "content_length": content_length,
                "untrusted": True,
            },
            truncated=truncated,
            truncation=truncation,
        )


def _extract_selective(text: str, phrase: str) -> str:
    """Extract sections containing the search phrase with surrounding context."""
    lines = text.splitlines()
    selected_ranges: list[tuple[int, int]] = []
    context_window = 5

    for i, line in enumerate(lines):
        if phrase.lower() in line.lower():
            start = max(0, i - context_window)
            end = min(len(lines), i + context_window + 1)
            selected_ranges.append((start, end))

    # Merge overlapping ranges
    merged: list[tuple[int, int]] = []
    for start, end in sorted(selected_ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    # Build output
    sections: list[str] = []
    for start, end in merged:
        sections.append("\n".join(lines[start:end]))

    return "\n---\n".join(sections)


# ---------------------------------------------------------------------------
# WebSearchTool
# ---------------------------------------------------------------------------


class WebSearchParams(BaseModel):
    """Parameters for web search."""

    query: str = Field(description="Search query (max 200 characters)", max_length=200)
    max_results: int = Field(default=5, ge=1, le=10, description="Maximum results to return")


class WebSearchTool:
    """External web search with untrusted results.

    This is a placeholder that delegates to configured search providers.
    Results are always marked as untrusted external content.
    """

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Search the web for current information. Results are untrusted."

    @property
    def params(self) -> type[BaseModel]:
        return WebSearchParams

    @property
    def side_effect(self) -> SideEffect:
        return SideEffect.READ

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"network.search"})

    @property
    def timeout_s(self) -> float:
        return 30.0

    @property
    def max_output_bytes(self) -> int:
        return 65536

    @property
    def idempotent(self) -> bool:
        return True

    @property
    def cancellable(self) -> bool:
        return True

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:
        return frozenset()

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        assert isinstance(args, WebSearchParams)

        # This is a framework hook — actual search provider is injected at runtime.
        # For now, return a not-configured error that the orchestration layer
        # would override with a real search provider.
        return ToolEnvelope.fail(
            code="KALASH_TOOL_ERROR",
            message="Web search provider not configured.",
            recoverable=True,
            remediation="Configure a search provider in settings.",
        )
