"""Per-model limits: output caps, context windows, throughput, tool support.

Nothing in the codebase asked a model what it could do, so the loop sent a
hardcoded ``max_tokens=8192`` and the full tool schema block to every provider.
Against real endpoints that produced three separate hard failures:

* ``max_completion_tokens must be less than or equal to 4096`` — the output
  reservation exceeded the model's cap.
* ``Request too large … TPM Limit 8000, Requested 11220`` — Groq counts the
  output reservation toward tokens-per-minute, so a 5k prefix plus an 8k
  reservation exceeded the whole per-minute budget before the user typed
  anything.
* ``'tool calling' is not supported with this model`` — tools were sent to a
  model that cannot accept them.

All three are knowable before the request. This module is that knowledge.

Matching is by substring against the model id, longest pattern first, so
``gpt-oss-120b`` resolves before ``gpt-oss``. Anything unrecognized gets
conservative defaults: it is better to under-request and succeed than to
over-request and fail.
"""

from __future__ import annotations

from dataclasses import dataclass

# Deliberately small. An unknown model that accepts more loses a little
# headroom; an unknown model that accepts less fails the request outright.
DEFAULT_MAX_OUTPUT = 4096
DEFAULT_CONTEXT_WINDOW = 32_768

# Reserve for the reply when throughput is the binding constraint. Enough for a
# substantial answer plus a few tool calls.
MIN_USABLE_OUTPUT = 1024

# What the tool-profile chooser holds back for the reply. An agent that can call
# every tool but only emit a sentence cannot complete a task, so the reply is
# reserved before the schema block is fitted.
TARGET_OUTPUT_TOKENS = 2048

# Fraction of a per-minute allowance a single request may claim.
#
# This was 0.75, which was arbitrary and actively harmful: a tool loop issues
# several requests per minute regardless, so throttling each individual request
# does not prevent minute-level exhaustion — it just makes every request weaker.
# The visible cost was that `web_search` and `fetch` did not fit the budget on an
# 8k/min model, so the agent correctly reported having no web search tool.
#
# 0.9 leaves genuine slack for provider-side accounting differences while letting
# the full tool set fit a small model.
TPM_REQUEST_SHARE = 0.9

# When throughput-bound, each loop iteration uses a fixed slice of the allowance
# rather than trying to cram the whole task into one request.
ITERATION_OUTPUT_SHARE = 0.25
ITERATION_CARRY_SHARE = 0.45


@dataclass(frozen=True, slots=True)
class IterationBudget:
    """Fixed per-iteration token envelope for phased agent work.

    Throughput-limited providers bill ``prompt + max_output`` per request.
    Large tasks are split across many iterations at this fixed size; durable
    state lives in the scratchpad/plan, and a final synthesis pass produces
    the user-facing answer.
    """

    request_allowance: int
    """Total tokens one API call may claim (prompt + output reservation)."""

    max_output_tokens: int
    """Fixed output reservation for each work iteration."""

    max_prompt_tokens: int
    """Target assembled prompt size before tools are added."""

    max_carry_tokens: int
    """Conversation history carried into the next iteration; roll up above this."""

    phased: bool = False
    """When true, roll up context between iterations and synthesize at the end."""


@dataclass(frozen=True, slots=True)
class ModelLimits:
    """What a model will accept."""

    max_output_tokens: int = DEFAULT_MAX_OUTPUT
    context_window: int = DEFAULT_CONTEXT_WINDOW
    supports_tools: bool = True
    tokens_per_minute: int | None = None
    """Provider throughput ceiling, when it constrains a single request."""

    source: str = "default"


# Provider-level TPM defaults for models not in the table. Used when the Kalash
# provider id is known (``groq``, ``together``, …) but the model string does not
# match a row — e.g. a newly released Groq-hosted checkpoint.
_PROVIDER_TPM_DEFAULTS: dict[str, int] = {
    "groq": 8_000,
    "together": 6_000,
    "cerebras": 60_000,
    "fireworks": 60_000,
    "deepinfra": 20_000,
    "openrouter": 20_000,
}


class ConstraintCache:
    """Runtime limits learned from provider error responses.

    Account tiers differ from the static table; when a provider states
    ``Limit 8000, Requested 9150`` we remember 8000 for that model for the rest
    of the process so the next request preflights correctly.
    """

    _tpm: dict[str, int] = {}
    _max_output: dict[str, int] = {}

    @classmethod
    def _key(cls, model_id: str) -> str:
        return model_id.strip().lower()

    @classmethod
    def record_tpm(cls, model_id: str, tpm: int) -> None:
        if tpm <= 0:
            return
        key = cls._key(model_id)
        existing = cls._tpm.get(key)
        if existing is None or tpm < existing:
            cls._tpm[key] = tpm

    @classmethod
    def record_max_output(cls, model_id: str, cap: int) -> None:
        if cap <= 0:
            return
        key = cls._key(model_id)
        existing = cls._max_output.get(key)
        if existing is None or cap < existing:
            cls._max_output[key] = cap

    @classmethod
    def observed_tpm(cls, model_id: str) -> int | None:
        return cls._tpm.get(cls._key(model_id))

    @classmethod
    def observed_max_output(cls, model_id: str) -> int | None:
        return cls._max_output.get(cls._key(model_id))

    @classmethod
    def reset(cls) -> None:
        cls._tpm.clear()
        cls._max_output.clear()


# (pattern, limits). Patterns are matched case-insensitively as substrings.
_TABLE: tuple[tuple[str, ModelLimits], ...] = (
    ("claude-opus-4", ModelLimits(32_000, 200_000, True, None, "anthropic")),
    ("claude-sonnet-4", ModelLimits(64_000, 200_000, True, None, "anthropic")),
    ("claude-haiku-4", ModelLimits(32_000, 200_000, True, None, "anthropic")),
    ("claude-3-7-sonnet", ModelLimits(64_000, 200_000, True, None, "anthropic")),
    ("claude-3-5-sonnet", ModelLimits(8_192, 200_000, True, None, "anthropic")),
    ("claude-3-5-haiku", ModelLimits(8_192, 200_000, True, None, "anthropic")),
    ("claude", ModelLimits(8_192, 200_000, True, None, "anthropic-generic")),
    # --- OpenAI ---
    ("gpt-5", ModelLimits(128_000, 400_000, True, None, "openai")),
    ("gpt-4.1", ModelLimits(32_768, 1_047_576, True, None, "openai")),
    ("gpt-4o-mini", ModelLimits(16_384, 128_000, True, None, "openai")),
    ("gpt-4o", ModelLimits(16_384, 128_000, True, None, "openai")),
    ("o3", ModelLimits(100_000, 200_000, True, None, "openai")),
    ("o1", ModelLimits(100_000, 200_000, True, None, "openai")),
    # --- Groq-hosted open models. Free tier is throughput-bound, not
    # window-bound, which is the constraint that actually broke here. ---
    ("gpt-oss-120b", ModelLimits(4_096, 131_072, True, 8_000, "groq")),
    ("gpt-oss-safeguard", ModelLimits(4_096, 131_072, True, 8_000, "groq")),
    ("gpt-oss-20b", ModelLimits(4_096, 131_072, True, 8_000, "groq")),
    ("safeguard-20b", ModelLimits(4_096, 131_072, True, 8_000, "groq")),
    ("gpt-oss", ModelLimits(4_096, 131_072, True, 8_000, "groq")),
    ("llama-3.3-70b", ModelLimits(32_768, 131_072, True, 12_000, "groq")),
    ("llama-3.1-8b", ModelLimits(8_192, 131_072, True, 20_000, "groq")),
    ("qwen3-32b", ModelLimits(40_960, 131_072, True, 8_000, "groq")),
    ("qwen3.6-27b", ModelLimits(8_192, 131_072, True, 8_000, "groq")),
    ("qwen", ModelLimits(8_192, 32_768, True, 8_000, "groq-generic")),
    ("kimi-k2", ModelLimits(16_384, 131_072, True, 10_000, "groq")),
    ("deepseek-r1", ModelLimits(8_192, 131_072, False, 8_000, "groq")),
    # Groq's compound systems run their own tool orchestration and reject a
    # caller-supplied tools array.
    ("groq/compound", ModelLimits(8_192, 131_072, False, 8_000, "groq")),
    ("compound-beta", ModelLimits(8_192, 131_072, False, 8_000, "groq")),
    # --- Google ---
    ("gemini-2.5-pro", ModelLimits(65_536, 1_048_576, True, None, "google")),
    ("gemini-2.5-flash", ModelLimits(65_536, 1_048_576, True, None, "google")),
    ("gemini", ModelLimits(8_192, 1_048_576, True, None, "google-generic")),
    # --- Local ---
    ("qwen3-coder", ModelLimits(8_192, 32_768, True, None, "local")),
    ("codellama", ModelLimits(4_096, 16_384, True, None, "local")),
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


def extract_provider_id(model_id: str) -> str | None:
    """Kalash provider slug from a ``provider/model`` id, if present."""
    if not model_id or "/" not in model_id:
        return None
    return model_id.split("/", 1)[0].strip().lower() or None


def effective_limits(model_id: str, provider_id: str | None = None) -> ModelLimits:
    """Resolved limits: static table → observed cache → provider default."""
    base = lookup(model_id)
    observed_tpm = ConstraintCache.observed_tpm(model_id)
    observed_out = ConstraintCache.observed_max_output(model_id)

    tpm = base.tokens_per_minute
    source = base.source
    max_out = base.max_output_tokens

    if observed_tpm is not None:
        tpm = observed_tpm
        source = "observed-tpm"
    elif tpm is None:
        slug = (provider_id or extract_provider_id(model_id) or "").lower()
        if slug in _PROVIDER_TPM_DEFAULTS:
            tpm = _PROVIDER_TPM_DEFAULTS[slug]
            source = f"{slug}-default"

    if observed_out is not None:
        max_out = min(max_out, observed_out)
        if source == "default":
            source = "observed-output"

    if (
        tpm == base.tokens_per_minute
        and max_out == base.max_output_tokens
        and source == base.source
    ):
        return base

    return ModelLimits(
        max_output_tokens=max_out,
        context_window=base.context_window,
        supports_tools=base.supports_tools,
        tokens_per_minute=tpm,
        source=source,
    )


def binding_constraint(
    model_id: str,
    prompt_tokens: int,
    *,
    provider_id: str | None = None,
) -> str:
    """Which ceiling is tightest for this prompt: ``tpm``, ``window``, or ``none``."""
    limits = effective_limits(model_id, provider_id)
    window_headroom = limits.context_window - prompt_tokens - MIN_USABLE_OUTPUT
    if limits.tokens_per_minute:
        allowance = int(limits.tokens_per_minute * TPM_REQUEST_SHARE)
        tpm_headroom = allowance - prompt_tokens - MIN_USABLE_OUTPUT
        if tpm_headroom < window_headroom:
            return "tpm"
    if window_headroom < 2048:
        return "window"
    return "none"


def iteration_budget(
    model_id: str,
    *,
    provider_id: str | None = None,
) -> IterationBudget:
    """Fixed token envelope for one agent-loop iteration.

    TPM-bound models get a predictable split so a multi-step task runs as N
    same-sized requests instead of one oversized failure. High-capacity models
    get generous limits and roll up only when approaching the context window.
    """
    limits = effective_limits(model_id, provider_id)
    allowance = tpm_allowance(model_id, provider_id=provider_id)

    if allowance is not None:
        max_out = min(
            limits.max_output_tokens,
            max(MIN_USABLE_OUTPUT, int(allowance * ITERATION_OUTPUT_SHARE)),
            TARGET_OUTPUT_TOKENS,
        )
        max_prompt = max(1024, allowance - max_out)
        max_carry = max(512, int(max_prompt * ITERATION_CARRY_SHARE))
        return IterationBudget(
            request_allowance=allowance,
            max_output_tokens=max_out,
            max_prompt_tokens=max_prompt,
            max_carry_tokens=max_carry,
            phased=True,
        )

    max_out = min(limits.max_output_tokens, 8192)
    max_prompt = max(2048, limits.context_window - max_out - MIN_USABLE_OUTPUT)
    max_carry = max(4096, int(limits.context_window * 0.55))
    return IterationBudget(
        request_allowance=max_prompt + max_out,
        max_output_tokens=max_out,
        max_prompt_tokens=max_prompt,
        max_carry_tokens=max_carry,
        phased=False,
    )


def tpm_allowance(model_id: str, *, provider_id: str | None = None) -> int | None:
    """Single-request token budget for throughput-limited models."""
    limits = effective_limits(model_id, provider_id)
    if limits.tokens_per_minute is None:
        return None
    return int(limits.tokens_per_minute * TPM_REQUEST_SHARE)


def fit_request_size(
    model_id: str,
    prompt_tokens: int,
    requested_output: int,
    *,
    provider_id: str | None = None,
) -> tuple[int, bool]:
    """Return ``(max_output, needs_input_shrink)`` for a measured prompt size.

    Providers like Groq bill ``prompt + max_tokens`` against TPM. When the
    measured prompt already exceeds the allowance, shrinking output alone is
    not enough — the caller must also drop tools, memory, or use a compact prompt.
    """
    output = resolve_output_tokens(
        model_id, requested_output, prompt_tokens, provider_id=provider_id
    )
    allowance = tpm_allowance(model_id, provider_id=provider_id)
    if allowance is None:
        return output, False

    total = prompt_tokens + output
    if total <= allowance:
        return output, False

    while prompt_tokens + output > allowance and output > MIN_USABLE_OUTPUT:
        output = max(MIN_USABLE_OUTPUT, output // 2)

    return output, prompt_tokens + output > allowance


def resolve_output_tokens(
    model_id: str,
    requested: int,
    prompt_tokens: int = 0,
    *,
    provider_id: str | None = None,
) -> int:
    """Largest safe output reservation for a model.

    Clamped by the model's own cap and, when the provider is throughput-bound,
    by what remains of a single request's share of the per-minute allowance —
    because providers like Groq bill ``prompt + max_tokens`` against TPM, so a
    generous reservation makes even a one-word prompt fail.
    """
    limits = effective_limits(model_id, provider_id)
    ceiling = min(requested, limits.max_output_tokens)

    if limits.tokens_per_minute:
        allowance = int(limits.tokens_per_minute * TPM_REQUEST_SHARE)
        remaining = allowance - prompt_tokens
        ceiling = min(ceiling, max(MIN_USABLE_OUTPUT, remaining))

    return max(MIN_USABLE_OUTPUT, ceiling)


def input_budget(model_id: str, *, provider_id: str | None = None) -> int:
    """Tokens available for the prompt, before the output reservation.

    For a throughput-bound model this is far smaller than the context window,
    which is the distinction that made a 131k-context model reject a 12k request.
    """
    limits = effective_limits(model_id, provider_id)
    window_budget = limits.context_window - MIN_USABLE_OUTPUT

    if limits.tokens_per_minute:
        allowance = int(limits.tokens_per_minute * TPM_REQUEST_SHARE)
        return max(1024, min(window_budget, allowance - MIN_USABLE_OUTPUT))

    return max(1024, window_budget)


def supports_tools(model_id: str) -> bool:
    """Whether the model accepts a tools array."""
    return lookup(model_id).supports_tools


def describe(model_id: str, *, provider_id: str | None = None) -> str:
    """One-line summary, for `/status` and diagnostics."""
    limits = effective_limits(model_id, provider_id)
    parts = [
        f"window {limits.context_window:,}",
        f"max output {limits.max_output_tokens:,}",
    ]
    if limits.tokens_per_minute:
        parts.append(f"TPM {limits.tokens_per_minute:,}")
    if not limits.supports_tools:
        parts.append("no tool calling")
    binding = binding_constraint(model_id, 0, provider_id=provider_id)
    if binding == "tpm":
        parts.append("throughput-bound")
    return f"{', '.join(parts)} [{limits.source}]"
