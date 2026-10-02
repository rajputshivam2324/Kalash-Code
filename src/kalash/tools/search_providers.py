"""Web search provider adapters.

Search is deliberately not a single hardcoded vendor. Each adapter normalizes
one provider's response into :class:`SearchResult`, and :func:`run_search`
picks whichever provider the environment has credentials for. That keeps the
tool layer free of vendor shapes and lets a user bring their own key without a
code change.

Selection order is by how useful the response is to an agent rather than by
popularity: Tavily returns extracted page content, Brave returns a general web
index with short descriptions, Exa returns semantic matches. Set
``KALASH_SEARCH_PROVIDER`` to pin one explicitly.

Every adapter returns content that originated outside the trust boundary. None
of them mark it — that is the caller's job, and it happens once in
:class:`~kalash.tools.web.WebSearchTool` so it cannot be forgotten per-provider.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

# Search APIs are interactive-path calls; a slow provider should fail rather
# than hold the turn open.
SEARCH_TIMEOUT_S = 20.0

# Snippets are stored, not inlined, so a generous cap costs context nothing.
MAX_SNIPPET_CHARS = 4000


class SearchProviderError(RuntimeError):
    """A provider could not answer. Message is safe to show the user."""


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One normalized search hit."""

    title: str
    url: str
    snippet: str = ""
    published: str = ""

    @property
    def display_url(self) -> str:
        """URL without the scheme.

        ``fetch`` re-adds ``https://`` to a bare URL, so dropping the scheme
        here is lossless and saves a few tokens on every result line.
        """
        for prefix in ("https://", "http://"):
            if self.url.startswith(prefix):
                return self.url[len(prefix) :]
        return self.url


@dataclass(frozen=True, slots=True)
class SearchResponse:
    """Results plus which provider produced them."""

    provider: str
    query: str
    results: tuple[SearchResult, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """How to detect and invoke one provider."""

    name: str
    env_keys: tuple[str, ...]
    call: Callable[[str, str, int], Coroutine[Any, Any, list[SearchResult]]]

    def credential(self) -> str:
        """First non-empty configured key, or empty string."""
        for key in self.env_keys:
            value = os.environ.get(key, "").strip()
            if value:
                return value
        return ""


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


async def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Issue one JSON request, translating transport failures to our error type."""
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - environment dependent
        msg = "httpx is not installed. Install with: pip install httpx"
        raise SearchProviderError(msg) from exc

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(SEARCH_TIMEOUT_S)) as client:
            response = await client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
            )
            if response.status_code == 401 or response.status_code == 403:
                msg = f"Search provider rejected the credential (HTTP {response.status_code})."
                raise SearchProviderError(msg)
            if response.status_code == 429:
                msg = "Search provider rate limit reached (HTTP 429)."
                raise SearchProviderError(msg)
            if response.status_code >= 400:
                msg = f"Search provider returned HTTP {response.status_code}."
                raise SearchProviderError(msg)
            return dict(response.json())
    except SearchProviderError:
        raise
    except httpx.TimeoutException as exc:
        msg = f"Search timed out after {SEARCH_TIMEOUT_S:.0f}s."
        raise SearchProviderError(msg) from exc
    except httpx.HTTPError as exc:
        msg = f"Search request failed: {exc}"
        raise SearchProviderError(msg) from exc
    except ValueError as exc:
        msg = "Search provider returned a malformed response."
        raise SearchProviderError(msg) from exc


def _clip(text: Any) -> str:
    """Coerce to a trimmed string under the snippet cap."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    return str(text).strip()[:MAX_SNIPPET_CHARS]


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


async def tavily_search(key: str, query: str, max_results: int) -> list[SearchResult]:
    """Tavily — returns extracted page content, best default for agents."""
    payload = await _request(
        "POST",
        "https://api.tavily.com/search",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        # Older deployments authenticate via the body; newer ones via the
        # header. Sending both keeps either working.
        json_body={
            "api_key": key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        },
    )
    return [
        SearchResult(
            title=_clip(item.get("title")) or "(untitled)",
            url=_clip(item.get("url")),
            snippet=_clip(item.get("content")),
            published=_clip(item.get("published_date")),
        )
        for item in (payload.get("results") or [])
        if item.get("url")
    ]


async def brave_search(key: str, query: str, max_results: int) -> list[SearchResult]:
    """Brave — independent general web index."""
    payload = await _request(
        "GET",
        "https://api.search.brave.com/res/v1/web/search",
        headers={
            "X-Subscription-Token": key,
            "Accept": "application/json",
        },
        params={"q": query, "count": max_results},
    )
    results = (payload.get("web") or {}).get("results") or []
    return [
        SearchResult(
            title=_clip(item.get("title")) or "(untitled)",
            url=_clip(item.get("url")),
            snippet=_clip(item.get("description")),
            published=_clip(item.get("page_age")),
        )
        for item in results
        if item.get("url")
    ]


async def exa_search(key: str, query: str, max_results: int) -> list[SearchResult]:
    """Exa — semantic retrieval, good for conceptual queries."""
    payload = await _request(
        "POST",
        "https://api.exa.ai/search",
        headers={
            "x-api-key": key,
            "Content-Type": "application/json",
        },
        json_body={
            "query": query,
            "numResults": max_results,
            "contents": {"text": {"maxCharacters": MAX_SNIPPET_CHARS}},
        },
    )
    return [
        SearchResult(
            title=_clip(item.get("title")) or "(untitled)",
            url=_clip(item.get("url")),
            snippet=_clip(item.get("text")),
            published=_clip(item.get("publishedDate")),
        )
        for item in (payload.get("results") or [])
        if item.get("url")
    ]


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec("tavily", ("TAVILY_API_KEY",), tavily_search),
    ProviderSpec("brave", ("BRAVE_API_KEY", "BRAVE_SEARCH_API_KEY"), brave_search),
    ProviderSpec("exa", ("EXA_API_KEY",), exa_search),
)


# ---------------------------------------------------------------------------
# Selection and entry point
# ---------------------------------------------------------------------------


def available_providers() -> list[str]:
    """Names of providers that currently have a usable credential."""
    return [spec.name for spec in PROVIDERS if spec.credential()]


def select_provider(preferred: str = "") -> ProviderSpec | None:
    """Resolve which provider to use.

    An explicit choice wins even if unconfigured, so the resulting error names
    the provider the user actually asked for rather than silently using another.
    """
    override = (preferred or os.environ.get("KALASH_SEARCH_PROVIDER", "")).strip().lower()
    if override:
        for spec in PROVIDERS:
            if spec.name == override:
                return spec
        return None
    for spec in PROVIDERS:
        if spec.credential():
            return spec
    return None


def configuration_hint() -> str:
    """Actionable guidance when no provider is configured."""
    names = ", ".join(f"{spec.name} ({spec.env_keys[0]})" for spec in PROVIDERS)
    return f"Set one of: {names}. Pin a provider with KALASH_SEARCH_PROVIDER."


async def run_search(
    query: str,
    *,
    max_results: int = 5,
    provider: str = "",
) -> SearchResponse:
    """Run a search against the resolved provider.

    Raises:
        SearchProviderError: no provider configured, or the provider failed.
    """
    spec = select_provider(provider)
    if spec is None:
        requested = (provider or os.environ.get("KALASH_SEARCH_PROVIDER", "")).strip()
        if requested:
            known = ", ".join(s.name for s in PROVIDERS)
            msg = f"Unknown search provider {requested!r}. Known providers: {known}."
            raise SearchProviderError(msg)
        msg = f"No web search provider configured. {configuration_hint()}"
        raise SearchProviderError(msg)

    key = spec.credential()
    if not key:
        msg = (
            f"Search provider {spec.name!r} is selected but has no credential. "
            f"Set {spec.env_keys[0]}."
        )
        raise SearchProviderError(msg)

    results = await spec.call(key, query, max_results)
    return SearchResponse(
        provider=spec.name,
        query=query,
        results=tuple(results[:max_results]),
    )
