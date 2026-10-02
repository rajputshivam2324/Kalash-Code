"""Model context and output capabilities. Provider quotas do not change prompts."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MAX_OUTPUT = 8192
DEFAULT_CONTEXT_WINDOW = 32_768


@dataclass(frozen=True, slots=True)
class ModelLimits:
    max_output_tokens: int = DEFAULT_MAX_OUTPUT
    context_window: int = DEFAULT_CONTEXT_WINDOW
    supports_tools: bool = True
    source: str = "default"


_TABLE: tuple[tuple[str, ModelLimits], ...] = (
    ("claude-opus-4", ModelLimits(32_000, 200_000, True, "anthropic")),
    ("claude-sonnet-4", ModelLimits(64_000, 200_000, True, "anthropic")),
    ("claude-haiku-4", ModelLimits(32_000, 200_000, True, "anthropic")),
    ("claude-3-7-sonnet", ModelLimits(64_000, 200_000, True, "anthropic")),
    ("claude-3-5-sonnet", ModelLimits(8_192, 200_000, True, "anthropic")),
    ("claude-3-5-haiku", ModelLimits(8_192, 200_000, True, "anthropic")),
    ("claude", ModelLimits(8_192, 200_000, True, "anthropic-generic")),
    # --- OpenAI ---
    ("gpt-5", ModelLimits(128_000, 400_000, True, "openai")),
    ("gpt-4.1", ModelLimits(32_768, 1_047_576, True, "openai")),
    ("gpt-4o-mini", ModelLimits(16_384, 128_000, True, "openai")),
    ("gpt-4o", ModelLimits(16_384, 128_000, True, "openai")),
    ("o3", ModelLimits(100_000, 200_000, True, "openai")),
    ("o1", ModelLimits(100_000, 200_000, True, "openai")),
    # Groq-hosted model capabilities; subscription quotas are provider errors.
    ("gpt-oss-120b", ModelLimits(4_096, 131_072, True, "groq")),
    ("gpt-oss-safeguard", ModelLimits(4_096, 131_072, True, "groq")),
    ("gpt-oss-20b", ModelLimits(4_096, 131_072, True, "groq")),
    ("safeguard-20b", ModelLimits(4_096, 131_072, True, "groq")),
    ("gpt-oss", ModelLimits(4_096, 131_072, True, "groq")),
    ("llama-3.3-70b", ModelLimits(32_768, 131_072, True, "groq")),
    ("llama-3.1-8b", ModelLimits(8_192, 131_072, True, "groq")),
    ("qwen3-32b", ModelLimits(40_960, 131_072, True, "groq")),
    ("qwen3.6-27b", ModelLimits(8_192, 131_072, True, "groq")),
    ("qwen", ModelLimits(8_192, 32_768, True, "groq-generic")),
    ("kimi-k2", ModelLimits(16_384, 131_072, True, "groq")),
    ("deepseek-r1", ModelLimits(8_192, 131_072, False, "groq")),
    # Groq's compound systems run their own tool orchestration and reject a
    # caller-supplied tools array.
    ("groq/compound", ModelLimits(8_192, 131_072, False, "groq")),
    ("compound-beta", ModelLimits(8_192, 131_072, False, "groq")),
    # --- Google ---
    ("gemini-2.5-pro", ModelLimits(65_536, 1_048_576, True, "google")),
    ("gemini-2.5-flash", ModelLimits(65_536, 1_048_576, True, "google")),
    ("gemini", ModelLimits(8_192, 1_048_576, True, "google-generic")),
    # --- OpenRouter / Poolside ---
    ("poolside", ModelLimits(8_192, 131_072, True, "openrouter")),
    ("laguna", ModelLimits(8_192, 131_072, True, "openrouter")),
    # --- Local ---
    ("qwen3-coder", ModelLimits(8_192, 32_768, True, "local")),
    ("codellama", ModelLimits(4_096, 16_384, True, "local")),
)


def normalize_model_id(model_id: str) -> str:
    """Strip provider prefix so ``openai/gpt-oss-safeguard-20b`` resolves like ``gpt-oss-safeguard-20b``."""
    if not model_id:
        return ""
    needle = model_id.strip().lower()
    if "/" in needle:
        needle = needle.rsplit("/", 1)[-1]
    return needle


def lookup(model_id: str) -> ModelLimits:
    """Resolve limits for a model id.

    Longest matching pattern wins, so a specific entry beats a family fallback.
    Both the raw id and the provider-stripped form are checked so entries like
    ``groq/compound`` still match while ``openai/gpt-oss-20b`` resolves via
    ``gpt-oss-20b``.
    """
    if not model_id:
        return ModelLimits()

    raw = model_id.strip().lower()
    normalized = normalize_model_id(model_id)
    needles = [raw] if raw == normalized else [raw, normalized]

    best: tuple[int, ModelLimits] | None = None
    for needle in needles:
        for pattern, limits in _TABLE:
            if pattern in needle and (best is None or len(pattern) > best[0]):
                best = (len(pattern), limits)
    return best[1] if best else ModelLimits()


def supports_tools(model_id: str) -> bool:
    return lookup(model_id).supports_tools


def describe(model_id: str, *, provider_id: str | None = None) -> str:
    limits = lookup(model_id)
    suffix = ", no tool calling" if not limits.supports_tools else ""
    return f"window {limits.context_window:,}, max output {limits.max_output_tokens:,}{suffix} [{limits.source}]"
