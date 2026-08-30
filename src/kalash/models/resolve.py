"""Single place that turns stored credentials into a live provider.

Both the TUI and headless mode go through this. They previously resolved
providers independently, so `kalash -p` ignored whatever `/connect` had saved
and only looked at environment variables — connecting in the TUI appeared to
do nothing for scripted runs.

Resolution order for a credential, highest priority first:

1. the value saved by `/connect`
2. the provider's environment variable

Note: the provider catalog and credential store currently live under
``kalash.tui``. They are not UI concerns and belong in ``models``/``core``;
they are imported lazily here so that headless callers do not pull in Textual.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Resolution:
    """The outcome of resolving a provider."""

    provider: Any | None
    provider_id: str | None
    model_id: str | None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.provider is not None


def active_selection() -> tuple[str | None, str | None]:
    """Return the saved (provider_id, model_id), falling back to the environment."""
    from kalash.tui.auth_store import get_active_provider
    from kalash.tui.providers import PROVIDERS

    saved = get_active_provider()
    if saved and saved[0]:
        return saved[0], saved[1]

    for provider in PROVIDERS:
        if provider.id == "custom":
            continue
        if os.environ.get(provider.env_key):
            model = provider.models[0] if provider.models else None
            return provider.id, model

    return None, None


def credential_for(provider_id: str) -> str:
    """Return the API key for a provider, preferring the saved value."""
    from kalash.tui.auth_store import get_credential
    from kalash.tui.providers import get_provider

    saved = get_credential(provider_id)
    if saved:
        return saved
    if provider_id in ("google", "gemini"):
        gemini_saved = get_credential("gemini") or get_credential("google")
        if gemini_saved:
            return gemini_saved
        return os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
    info = get_provider(provider_id)
    return os.environ.get(info.env_key, "") if info else ""


def base_url_for(provider_id: str) -> str:
    """Return the base URL for a provider, honouring a custom override."""
    from kalash.tui.auth_store import get_credential
    from kalash.tui.providers import get_provider

    override = get_credential(f"{provider_id}:base_url")
    if override:
        return override
    info = get_provider(provider_id)
    return info.base_url if info else ""


def build_provider(
    provider_id: str | None = None,
    model_id: str | None = None,
) -> Resolution:
    """Construct a model provider.

    With no arguments the saved selection is used. Never raises: a failure is
    reported through ``Resolution.reason`` so callers can present it.
    """
    from kalash.tui.providers import get_provider

    # Only fall back to the saved selection when no provider was named.
    # Treating "provider given, model omitted" as "resolve everything from
    # scratch" silently discarded the caller's provider.
    if provider_id is None:
        provider_id, saved_model = active_selection()
        if model_id is None:
            model_id = saved_model

    if not provider_id:
        return Resolution(
            provider=None,
            provider_id=None,
            model_id=None,
            reason="no provider configured — run /connect, or set a provider API key",
        )

    info = get_provider(provider_id)
    if info is None:
        return Resolution(
            provider=None,
            provider_id=provider_id,
            model_id=model_id,
            reason=f"unknown provider {provider_id!r}",
        )

    if not model_id:
        model_id = info.models[0] if info.models else None
    if not model_id:
        return Resolution(
            provider=None,
            provider_id=provider_id,
            model_id=None,
            reason=f"no model selected for {info.name} — run /models",
        )

    api_key = credential_for(provider_id)
    if info.requires_key and not api_key:
        return Resolution(
            provider=None,
            provider_id=provider_id,
            model_id=model_id,
            reason=f"no API key for {info.name} — run /connect or set {info.env_key}",
        )

    provider: Any = None  # explicit annotation prevents type-narrowing (S-2)
    try:
        if provider_id == "anthropic":
            from kalash.models.providers.anthropic import AnthropicProvider

            provider = AnthropicProvider(api_key=api_key, model=model_id)
        elif provider_id in ("google", "gemini"):
            from kalash.models.providers.google import GeminiProvider

            provider = GeminiProvider(api_key=api_key, model=model_id)
        elif provider_id == "openai":
            from kalash.models.providers.openai import OpenAIProvider

            provider = OpenAIProvider(api_key=api_key, model=model_id)
        else:
            from kalash.models.providers.openai_compatible import (
                OpenAICompatibleProvider,
            )

            provider = OpenAICompatibleProvider(
                base_url=base_url_for(provider_id),
                model=model_id,
                api_key=api_key or "not-needed",
                tool_use=info.tool_use,
                context_window=info.context_window,
                provider_name=info.id,
            )
    except ImportError as exc:
        return Resolution(
            provider=None,
            provider_id=provider_id,
            model_id=model_id,
            reason=f"provider package not installed: {exc}",
        )

    return Resolution(provider=provider, provider_id=provider_id, model_id=model_id)

def build_gateway(model_identifiers: list[str]) -> Resolution:
    """Construct a ModelGateway with primary and fallback providers.
    
    Each identifier can be 'provider/model' or just 'provider'.
    If an identifier is just 'provider', it uses the default model for that provider.
    """
    if not model_identifiers:
        return build_provider()
        
    providers: list[Any] = []
    
    for identifier in model_identifiers:
        if "/" in identifier:
            provider_id, model_id = identifier.split("/", 1)
        else:
            provider_id, model_id = identifier, None
            
        res = build_provider(provider_id=provider_id, model_id=model_id)
        if res.ok:
            providers.append(res.provider)
        else:
            # If the primary provider fails to build, we fail the whole gateway
            # If a fallback fails, we might just warn, but for now let's just fail
            if not providers:
                return res

    # Filter out any None values that slipped through (S-3)
    valid_providers = [p for p in providers if p is not None]
    if not valid_providers:
        return Resolution(None, None, None, "No providers could be built")
        
    from kalash.models.gateway import ModelGateway
    
    gateway = ModelGateway(primary=valid_providers[0], fallbacks=valid_providers[1:])
    
    # Return resolution with the gateway as the provider
    # provider_id and model_id are from the primary provider
    primary_id = model_identifiers[0]
    if "/" in primary_id:
        p_id, m_id = primary_id.split("/", 1)
    else:
        p_id, m_id = primary_id, None
        
    return Resolution(provider=gateway, provider_id=p_id, model_id=m_id)
