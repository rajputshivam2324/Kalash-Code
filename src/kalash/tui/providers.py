"""Provider registry with API-verified models.

Models are fetched from provider APIs at connect time where possible.
Static lists below are only used as fallback and are sourced from official docs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderInfo:
    """A known LLM provider."""

    id: str
    name: str
    env_key: str
    base_url: str
    models_url: str = ""  # endpoint to fetch available models
    models: list[str] = field(default_factory=list)  # static fallback
    requires_key: bool = True
    tool_use: bool = True
    context_window: int = 128_000


# Providers with models sourced from official API docs (August 2026)
PROVIDERS: list[ProviderInfo] = [
    ProviderInfo(
        id="anthropic",
        name="Anthropic",
        env_key="ANTHROPIC_API_KEY",
        base_url="https://api.anthropic.com",
        models_url="https://api.anthropic.com/v1/models",
        # Source: platform.claude.com/docs/en/about-claude/models
        models=[
            "claude-fable-5",
            "claude-mythos-5",
            "claude-opus-4-20250514",
            "claude-sonnet-4-20250514",
            "claude-haiku-4-5-20241022",
        ],
        context_window=200_000,
    ),
    ProviderInfo(
        id="openai",
        name="OpenAI",
        env_key="OPENAI_API_KEY",
        base_url="https://api.openai.com/v1",
        models_url="https://api.openai.com/v1/models",
        # Source: developers.openai.com/api/docs/models
        models=[
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-4o",
            "gpt-4o-mini",
            "o3-mini",
        ],
        context_window=128_000,
    ),
    ProviderInfo(
        id="google",
        name="Google Gemini",
        env_key="GEMINI_API_KEY",
        base_url="https://generativelanguage.googleapis.com",
        models=[
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.5-flash-lite",
            "gemini-flash-latest",
            "gemini-pro-latest",
        ],
        context_window=1_048_576,
    ),
    ProviderInfo(
        id="openrouter",
        name="OpenRouter",
        env_key="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
        models_url="https://openrouter.ai/api/v1/models",
        # Source: openrouter.ai/models — dynamic, fetched at runtime
        models=[],  # populated dynamically from API
        context_window=200_000,
    ),
    ProviderInfo(
        id="groq",
        name="Groq",
        env_key="GROQ_API_KEY",
        base_url="https://api.groq.com/openai/v1",
        models_url="https://api.groq.com/openai/v1/models",
        # Source: console.groq.com/docs/models (August 2026)
        models=[
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "qwen/qwen3.6-27b",
        ],
        context_window=131_072,
    ),
    ProviderInfo(
        id="deepseek",
        name="DeepSeek",
        env_key="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com",
        models_url="https://api.deepseek.com/models",
        # Source: api-docs.deepseek.com (August 2026)
        models=[
            "deepseek-v4-pro",
            "deepseek-v4-flash",
        ],
        context_window=128_000,
    ),
    ProviderInfo(
        id="together",
        name="Together AI",
        env_key="TOGETHER_API_KEY",
        base_url="https://api.together.xyz/v1",
        models_url="https://api.together.xyz/v1/models",
        # Populated dynamically — Together has 100+ models
        models=[],
        context_window=128_000,
    ),
    ProviderInfo(
        id="fireworks",
        name="Fireworks AI",
        env_key="FIREWORKS_API_KEY",
        base_url="https://api.fireworks.ai/inference/v1",
        models_url="https://api.fireworks.ai/inference/v1/models",
        # Populated dynamically
        models=[],
        context_window=128_000,
    ),
    ProviderInfo(
        id="cerebras",
        name="Cerebras",
        env_key="CEREBRAS_API_KEY",
        base_url="https://api.cerebras.ai/v1",
        models_url="https://api.cerebras.ai/v1/models",
        models=[],
        context_window=128_000,
    ),
    ProviderInfo(
        id="nvidia",
        name="NVIDIA",
        env_key="NVIDIA_API_KEY",
        base_url="https://integrate.api.nvidia.com/v1",
        models_url="https://integrate.api.nvidia.com/v1/models",
        models=[],
        context_window=128_000,
    ),
    ProviderInfo(
        id="ollama",
        name="Ollama (local)",
        env_key="OLLAMA_HOST",
        base_url="http://localhost:11434/v1",
        models_url="http://localhost:11434/api/tags",
        # Populated dynamically from local Ollama instance
        models=[],
        requires_key=False,
        tool_use=False,
        context_window=32_000,
    ),
    ProviderInfo(
        id="custom",
        name="Other (OpenAI-compatible)",
        env_key="KALASH_API_KEY",
        base_url="",
        models=[],
        context_window=32_000,
    ),
]


def get_provider(provider_id: str) -> ProviderInfo | None:
    """Look up a provider by ID."""
    for p in PROVIDERS:
        if p.id == provider_id:
            return p
    return None


async def fetch_models(provider_id: str, api_key: str = "") -> list[str]:
    """Fetch available models from a provider's API.

    Uses the provider's models endpoint. Falls back to static list if fetch fails.
    Returns a list of model ID strings.
    """
    provider = get_provider(provider_id)
    if not provider or not provider.models_url:
        return provider.models if provider else []

    try:
        import httpx

        headers: dict[str, str] = {"Content-Type": "application/json"}

        # Set auth header based on provider
        if api_key:
            if provider_id == "anthropic":
                headers["x-api-key"] = api_key
                headers["anthropic-version"] = "2023-06-01"
            else:
                headers["Authorization"] = f"Bearer {api_key}"

        url = provider.models_url
        # For Ollama, use the tags endpoint with different host if configured
        if provider_id == "ollama":
            import os
            host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
            url = f"{host}/api/tags"

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)

            if response.status_code != 200:
                return provider.models  # fallback to static

            data = response.json()

            # Parse response based on provider format
            if provider_id == "ollama":
                # Ollama returns {"models": [{"name": "llama3.1:latest", ...}]}
                models_data = data.get("models", [])
                return [m.get("name", "").split(":")[0] for m in models_data if m.get("name")]

            elif provider_id == "openrouter":
                # OpenRouter returns {"data": [{"id": "anthropic/claude-...", ...}]}
                models_data = data.get("data", [])
                # Filter to chat models only, take top ones
                chat_models = [
                    m["id"] for m in models_data
                    if m.get("id") and "/chat" not in m["id"]
                ]
                return chat_models[:50]  # cap to avoid overwhelming list

            else:
                # Standard OpenAI format: {"data": [{"id": "model-name", ...}]}
                models_data = data.get("data", [])
                model_ids = []
                for m in models_data:
                    model_id = m.get("id", "")
                    if not model_id:
                        continue
                    # Filter out embedding, whisper, tts, dall-e models for chat providers
                    skip_prefixes = ("text-embedding", "whisper", "tts", "dall-e", "davinci", "babbage")
                    if any(model_id.startswith(p) for p in skip_prefixes):
                        continue
                    model_ids.append(model_id)
                return sorted(model_ids) if model_ids else provider.models

    except Exception:
        # Network error, timeout, import error — fall back to static list
        return provider.models if provider else []
