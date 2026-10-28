"""Built-in model catalog: limits, reasoning controls and prices.

Data was read from each provider's official documentation on 2026-10-23.
Provider APIs rarely expose this metadata, and prices change, so treat it as a
default: ``kalash models --refresh`` merges live availability and limits from the providers'
model-list endpoints, and settings (``pricing``, ``context_window``, ``max_output_tokens``)
always win. Unknown model ids are allowed everywhere and get safe defaults.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

CATALOG_VERIFIED = "2026-10-23"


@dataclass(frozen=True)
class ModelListing:
    """An available model with only the metadata the provider has actually reported."""

    context: int | None = None
    vision: bool | None = None
    tools: bool | None = None


# Availability must not wait for complete pricing/output documentation. Sarvam
# GET /v2/models reported these fields on 2026-10-28; live trials confirmed the ID.
# Source: GET https://api.sarvam.ai/v2/models (2026-10-28).
MODEL_LISTINGS: dict[str, dict[str, ModelListing]] = {
    "sarvam": {"glm5.3-flash": ModelListing(context=1_048_576, vision=True, tools=True)}
}

Thinking = Literal[
    "none",  # no reasoning control
    "effort",  # a reasoning-effort enum (OpenAI-style)
    "adaptive",  # Anthropic >=4.7: omit `thinking`, steer with output_config.effort
    "budget",  # Anthropic <4.7 / Gemini 2.5: a token budget
    "level",  # Gemini 3+/4: thinkingLevel enum
    "always",  # always reasons, effort optional
]


@dataclass(frozen=True)
class Price:
    """USD per million tokens. ``cache_read``/``cache_write`` fall back to ``input``."""

    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None


@dataclass(frozen=True)
class Tier:
    """A different price once a single request's prompt exceeds ``above`` tokens."""

    above: int
    price: Price


@dataclass(frozen=True)
class ModelInfo:
    id: str
    context: int
    max_output: int
    price: Price | None = None
    tiers: tuple[Tier, ...] = ()
    vision: bool = False
    tools: bool = True
    thinking: Thinking = "none"
    efforts: tuple[str, ...] = ()  # accepted values for the reasoning control
    default_effort: str | None = None
    can_disable_thinking: bool = True
    sampling: bool = True  # accepts temperature/top_p
    replay_field: str | None = None  # chat-completions field that carries reasoning
    stream_usage: bool = True  # send stream_options.include_usage
    note: str = ""
    extra: dict[str, object] = field(default_factory=dict, compare=False, hash=False)

    def price_for(self, prompt_tokens: int) -> Price | None:
        chosen = self.price
        for tier in sorted(self.tiers, key=lambda t: t.above):
            if prompt_tokens > tier.above:
                chosen = tier.price
        return chosen


M = 1_000_000
K = 1_000
_E5 = ("low", "medium", "high", "xhigh", "max")
_E6 = ("none", "low", "medium", "high", "xhigh", "max")


def _info(models: list[ModelInfo]) -> dict[str, ModelInfo]:
    return {m.id: m for m in models}


ANTHROPIC = _info(
    [
        ModelInfo(
            "claude-opus-5-5",
            M,
            128 * K,
            Price(4, 20, 0.20, 5),
            vision=True,
            thinking="adaptive",
            efforts=_E5,
            default_effort="medium",
            can_disable_thinking=False,
            sampling=False,
            note="recommended starting model",
        ),
        ModelInfo(
            "claude-fable-5-1",
            M,
            128 * K,
            Price(10, 50, 0.25, 12.5),
            vision=True,
            thinking="adaptive",
            efforts=_E5,
            default_effort="high",
            can_disable_thinking=False,
            sampling=False,
            note="hardest reasoning",
        ),
        ModelInfo(
            "claude-sonnet-5-5",
            M,
            128 * K,
            Price(2, 10, 0.10, 2.5),
            vision=True,
            thinking="adaptive",
            efforts=_E5,
            default_effort="medium",
            can_disable_thinking=False,
            sampling=False,
        ),
        ModelInfo(
            "claude-haiku-5-5",
            M,
            128 * K,
            Price(0.10, 0.50, 0.01, 0.125),
            tiers=(Tier(100 * K, Price(0.50, 2.50, 0.05, 0.625)),),
            vision=True,
            thinking="adaptive",
            efforts=_E5,
            default_effort="medium",
            sampling=False,
            note="cheapest; higher price above 100k prompt tokens",
        ),
    ]
)

OPENAI = _info(
    [
        ModelInfo(
            "gpt-6-astra",
            1_050_000,
            128 * K,
            Price(10, 50, 1.00),
            tiers=(Tier(272 * K, Price(20, 75, 2.00)),),
            vision=True,
            thinking="effort",
            efforts=("low", "medium", "high", "xhigh", "max"),
            default_effort="medium",
            can_disable_thinking=False,
            sampling=False,
            note="flagship; Responses API only",
        ),
        ModelInfo(
            "gpt-6.1-sol",
            1_050_000,
            128 * K,
            Price(2, 10, 0.10),
            tiers=(Tier(272 * K, Price(4, 15, 0.20)),),
            vision=True,
            thinking="effort",
            efforts=("low", "medium", "high", "xhigh", "max"),
            default_effort="medium",
            can_disable_thinking=False,
            sampling=False,
            note="Responses API only",
        ),
        ModelInfo(
            "gpt-6-sol",
            1_050_000,
            128 * K,
            Price(2, 10, 0.20),
            tiers=(Tier(272 * K, Price(4, 15, 0.40)),),
            vision=True,
            thinking="effort",
            efforts=_E6,
            default_effort="medium",
            sampling=False,
        ),
        ModelInfo(
            "gpt-6-luna",
            1_050_000,
            128 * K,
            Price(0.10, 0.50, 0.01),
            tiers=(Tier(272 * K, Price(0.20, 0.75, 0.02)),),
            vision=True,
            thinking="effort",
            efforts=_E6,
            default_effort="medium",
            sampling=False,
        ),
        ModelInfo(
            "gpt-5.6-sol",
            1_050_000,
            128 * K,
            Price(4, 20, 0.40),
            tiers=(Tier(272 * K, Price(8, 30, 0.80)),),
            vision=True,
            thinking="effort",
            efforts=_E6,
            default_effort="medium",
            sampling=False,
            note="launch price valid at least through 2026-11-21",
        ),
        ModelInfo(
            "gpt-5.6-terra",
            1_050_000,
            128 * K,
            Price(2, 12, 0.20),
            tiers=(Tier(272 * K, Price(4, 18, 0.40)),),
            vision=True,
            thinking="effort",
            efforts=_E6,
            default_effort="medium",
            sampling=False,
        ),
        ModelInfo(
            "gpt-5.6-luna",
            1_050_000,
            128 * K,
            Price(0.20, 1.20, 0.02),
            tiers=(Tier(272 * K, Price(0.40, 1.80, 0.04)),),
            vision=True,
            thinking="effort",
            efforts=_E6,
            default_effort="medium",
            sampling=False,
        ),
    ]
)

_GEM_LEVELS = ("minimal", "low", "medium", "high")
GOOGLE = _info(
    [
        ModelInfo(
            "gemini-4-argon",
            1_048_576,
            65_536,
            Price(4, 20, 0.20),
            vision=True,
            thinking="level",
            efforts=("low", "medium", "high"),
            default_effort="medium",
            can_disable_thinking=False,
            sampling=False,
            note="launch price $2/$10 until 2026-11-30; this is the later price",
        ),
        ModelInfo(
            "gemini-3.8-flash",
            1_048_576,
            65_536,
            Price(1.5, 7.5, 0.15),
            vision=True,
            thinking="level",
            efforts=("low", "medium", "high"),
            default_effort="medium",
            sampling=False,
            note="launch price $0.75/$3.75 until 2026-12-31",
        ),
        ModelInfo(
            "gemini-3.6-flash",
            1_048_576,
            65_536,
            Price(1.5, 7.5, 0.15),
            vision=True,
            thinking="level",
            efforts=_GEM_LEVELS,
            default_effort="medium",
            sampling=False,
        ),
        ModelInfo(
            "gemini-3.5-flash-lite",
            1_048_576,
            65_536,
            Price(0.30, 2.50, 0.03),
            vision=True,
            thinking="level",
            efforts=_GEM_LEVELS,
            default_effort="minimal",
            sampling=False,
        ),
        ModelInfo(
            "gemini-3.1-flash-lite",
            1_048_576,
            65_536,
            Price(0.25, 1.50, 0.025),
            vision=True,
            thinking="level",
            efforts=_GEM_LEVELS,
            default_effort="minimal",
            sampling=False,
        ),
        ModelInfo(
            "gemini-3.1-pro-preview",
            1_048_576,
            65_536,
            Price(2, 12, 0.20),
            tiers=(Tier(200 * K, Price(4, 18, 0.40)),),
            vision=True,
            thinking="level",
            efforts=("low", "medium", "high"),
            default_effort="high",
            sampling=False,
            note="preview",
        ),
        ModelInfo(
            "gemini-2.5-pro",
            1_048_576,
            65_536,
            Price(1.25, 10, 0.125),
            vision=True,
            thinking="budget",
            efforts=("low", "medium", "high"),
            default_effort="medium",
            can_disable_thinking=False,
            note="restricted to existing users since 2026-09-18",
        ),
        ModelInfo(
            "gemini-2.5-flash",
            1_048_576,
            65_536,
            Price(0.30, 2.50, 0.03),
            vision=True,
            thinking="budget",
            efforts=("none", "low", "medium", "high"),
            default_effort="medium",
            note="restricted to existing users since 2026-09-18",
        ),
    ]
)

_DS_EFFORT = ("low", "medium", "high", "max")
DEEPSEEK = _info(
    [
        ModelInfo(
            "deepseek-flash",
            1_000_000,
            393_216,
            Price(0.30, 1.20, 0.006),
            vision=True,
            thinking="always",
            efforts=_DS_EFFORT,
            default_effort="high",
            note="V4.1 Flash; prices shown are peak (off-peak is half)",
        ),
        ModelInfo(
            "deepseek-v4-pro",
            1_000_000,
            393_216,
            Price(1.32, 3.96, 0.044),
            thinking="always",
            efforts=_DS_EFFORT,
            default_effort="high",
            note="text only; prices are peak",
        ),
    ]
)

SARVAM = _info(
    [
        ModelInfo(
            "sarvam-105b",
            128_000,
            32_768,
            thinking="effort",
            efforts=("off", "low", "high"),
            default_effort="high",
            stream_usage=False,
            replay_field="reasoning_content",
            note="V1 route; max_tokens is capped by plan (4096 Starter, 16384 Pro)",
            # 2026-10-28: V1 reference documents explicit null to disable reasoning.
            extra={"route": "v1", "off_body": {"reasoning_effort": None}},
        ),
        ModelInfo(
            "sarvam-105b-conversations",
            32_000,
            8_192,
            thinking="effort",
            efforts=("off", "low", "high"),
            default_effort="low",
            stream_usage=False,
            replay_field="reasoning_content",
            extra={"route": "v1", "off_body": {"reasoning_effort": None}},
        ),
        ModelInfo(
            "glm5.3",
            1_048_576,
            131_072,
            thinking="effort",
            efforts=("low", "high", "max"),
            default_effort="high",
            replay_field="reasoning_content",
            extra={"route": "v2"},
            note="omitting reasoning_effort means max, which is expensive",
        ),
        ModelInfo("gemma4", 131_072, 32_768, vision=True, extra={"route": "v2"}),
        ModelInfo(
            "deepseekv4.1-flash",
            1_048_576,
            131_072,
            thinking="effort",
            efforts=("off", "low", "high", "max"),
            default_effort="high",
            replay_field="reasoning_content",
            extra={
                "route": "v2",
                # Sarvam documents this as a literal `extra_body` key in the JSON body.
                "off_body": {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
            },
        ),
    ]
)

MOONSHOT = _info(
    [
        ModelInfo(
            "kimi-k3",
            M,
            131_072,
            Price(3, 15, 0.30),
            thinking="effort",
            efforts=("low", "high", "max"),
            default_effort="max",
            sampling=False,
            replay_field="reasoning_content",
            note="reasoning must be replayed",
        ),
        ModelInfo(
            "kimi-k2.7-code",
            256 * K,
            65_536,
            Price(0.95, 4),
            thinking="always",
            sampling=False,
            replay_field="reasoning_content",
        ),
    ]
)

ZAI = _info(
    [
        ModelInfo(
            "glm-5.3",
            M,
            128 * K,
            Price(1.4, 4.4),
            thinking="effort",
            efforts=("low", "high", "max"),
            default_effort="high",
            can_disable_thinking=False,
            replay_field="reasoning_content",
        ),
        ModelInfo(
            "glm-5.3-flash",
            M,
            128 * K,
            Price(0.15, 0.50),
            thinking="effort",
            efforts=("low", "high", "max"),
            default_effort="high",
            can_disable_thinking=False,
            replay_field="reasoning_content",
        ),
    ]
)

MINIMAX = _info(
    [
        ModelInfo(
            "MiniMax-M3.1-Flash", M, 131_072, Price(0.15, 0.60), thinking="always", sampling=False
        ),
        ModelInfo("MiniMax-M3", M, 131_072, Price(0.30, 1.20), thinking="always", sampling=False),
    ]
)

XAI = _info(
    [
        ModelInfo(
            "grok-4.7",
            500 * K,
            128 * K,
            Price(2, 6),
            tiers=(Tier(200 * K, Price(4, 12)),),
            thinking="effort",
            efforts=("low", "medium", "high", "xhigh"),
            default_effort="high",
            can_disable_thinking=False,
            sampling=False,
        ),
        ModelInfo("grok-code-fast-1", 256 * K, 64 * K, Price(1, 2), thinking="always"),
    ]
)

OPENROUTER = _info(
    [
        ModelInfo(
            "anthropic/claude-fable-5.1",
            M,
            128 * K,
            thinking="effort",
            efforts=("low", "medium", "high", "xhigh", "max"),
            replay_field="reasoning",
        ),
        ModelInfo(
            "anthropic/claude-opus-5",
            M,
            128 * K,
            thinking="effort",
            efforts=("low", "medium", "high", "xhigh", "max"),
            replay_field="reasoning",
        ),
        ModelInfo(
            "openai/gpt-5.6-sol",
            1_050_000,
            128 * K,
            thinking="effort",
            efforts=_E6,
            replay_field="reasoning",
        ),
        ModelInfo("x-ai/grok-4.6", 500 * K, 450 * K, thinking="effort", replay_field="reasoning"),
        ModelInfo(
            "moonshotai/kimi-k3", 1_048_576, 943_718, thinking="effort", replay_field="reasoning"
        ),
        ModelInfo("z-ai/glm-5.3", 1_048_576, 262_144, thinking="effort", replay_field="reasoning"),
    ]
)

GROQ = _info(
    [
        ModelInfo(
            "openai/gpt-oss-120b",
            131_072,
            65_536,
            thinking="effort",
            efforts=("low", "medium", "high"),
            default_effort="medium",
            replay_field="reasoning",
        ),
        ModelInfo(
            "openai/gpt-oss-20b",
            131_072,
            65_536,
            thinking="effort",
            efforts=("low", "medium", "high"),
            default_effort="medium",
            replay_field="reasoning",
        ),
        ModelInfo(
            "qwen/qwen3.8-27b",
            131_072,
            16_384,
            thinking="effort",
            efforts=("none", "low", "medium", "high"),
            default_effort="medium",
            replay_field="reasoning",
            note="preview",
        ),
    ]
)

TOGETHER = _info(
    [
        ModelInfo(
            "zai-org/GLM-5.3",
            1_048_575,
            131_072,
            thinking="effort",
            efforts=("low", "medium", "high", "max"),
            replay_field="reasoning_content",
        ),
        ModelInfo(
            "moonshotai/Kimi-K3",
            1_048_576,
            131_072,
            thinking="effort",
            efforts=("low", "medium", "high", "max"),
            replay_field="reasoning_content",
        ),
        ModelInfo(
            "deepseek-ai/DeepSeek-V4-Pro-0813",
            1_048_576,
            131_072,
            thinking="effort",
            efforts=("low", "medium", "high"),
            replay_field="reasoning",
        ),
        ModelInfo(
            "deepseek-ai/DeepSeek-V4.1-Flash",
            1_000_000,
            131_072,
            thinking="effort",
            efforts=("low", "medium", "high"),
            replay_field="reasoning",
        ),
        ModelInfo(
            "openai/gpt-oss-120b",
            131_072,
            32_768,
            thinking="effort",
            efforts=("low", "medium", "high"),
            replay_field="reasoning",
        ),
    ]
)

FIREWORKS = _info(
    [
        ModelInfo(
            "accounts/fireworks/models/kimi-k3",
            1_040_000,
            131_072,
            thinking="effort",
            replay_field="reasoning_content",
        ),
        ModelInfo(
            "accounts/fireworks/models/glm-5p3",
            1_040_000,
            131_072,
            thinking="effort",
            replay_field="reasoning_content",
        ),
        ModelInfo(
            "accounts/fireworks/models/deepseek-v4p1-flash-1017",
            1_000_000,
            131_072,
            thinking="effort",
            replay_field="reasoning_content",
        ),
        ModelInfo(
            "accounts/fireworks/models/minimax-m3",
            512 * K,
            131_072,
            thinking="always",
            replay_field="reasoning_content",
        ),
        ModelInfo(
            "accounts/fireworks/models/gpt-oss-120b",
            131_072,
            32_768,
            thinking="effort",
            replay_field="reasoning_content",
        ),
    ]
)

CEREBRAS = _info(
    [
        ModelInfo(
            "qwen-3.8-27b",
            131_072,
            40_960,
            thinking="effort",
            efforts=("none", "low", "medium", "high"),
            replay_field="reasoning",
        ),
        ModelInfo(
            "gpt-oss-120b",
            131_072,
            40_960,
            thinking="effort",
            efforts=("low", "medium", "high"),
            replay_field="reasoning",
        ),
    ]
)

OLLAMA = _info(
    [
        ModelInfo("qwen3.8:27b", 256 * K, 32_768, thinking="always", replay_field="reasoning"),
        ModelInfo(
            "gpt-oss:20b",
            128 * K,
            32_768,
            thinking="effort",
            efforts=("low", "medium", "high"),
            replay_field="reasoning",
        ),
        ModelInfo(
            "gpt-oss:120b",
            128 * K,
            32_768,
            thinking="effort",
            efforts=("low", "medium", "high"),
            replay_field="reasoning",
        ),
    ]
)

CATALOG: dict[str, dict[str, ModelInfo]] = {
    "anthropic": ANTHROPIC,
    "openai": OPENAI,
    "google": GOOGLE,
    "deepseek": DEEPSEEK,
    "sarvam": SARVAM,
    "moonshot": MOONSHOT,
    "zai": ZAI,
    "minimax": MINIMAX,
    "xai": XAI,
    "openrouter": OPENROUTER,
    "groq": GROQ,
    "together": TOGETHER,
    "fireworks": FIREWORKS,
    "cerebras": CEREBRAS,
    "ollama": OLLAMA,
}

# Ids that were retired or are about to be. ``gone`` ids are refused up front with this message
# instead of failing later with a confusing 404; ``soon`` ids and renamed ids only warn.
Retired = tuple[Literal["gone", "soon", "alias"], str]
RETIRED: dict[str, dict[str, Retired]] = {
    "anthropic": {
        "claude-opus-4-1": ("gone", "retired 2026-08-05; use claude-opus-5-5"),
        "claude-opus-4": ("gone", "retired 2026-06-15; use claude-opus-5-5"),
        "claude-sonnet-4": ("gone", "retired 2026-06-15; use claude-sonnet-5-5"),
        "claude-3-7-sonnet": ("gone", "retired; use claude-sonnet-5-5"),
        "claude-3-5-haiku": ("gone", "retired; use claude-haiku-5-5"),
        "claude-sonnet-4-5": ("soon", "retires 2026-11-30; use claude-sonnet-5-5"),
    },
    "openai": {
        "o1": ("gone", "shut down 2026-10-23"),
        "o3-mini": ("gone", "shut down 2026-10-23"),
        "o4-mini": ("gone", "shut down 2026-10-23"),
        "gpt-4-turbo": ("gone", "shut down 2026-10-23"),
        "gpt-5-codex": ("gone", "shut down 2026-07-23; use gpt-5.6-sol"),
        "gpt-5": ("soon", "snapshot shuts down 2026-12-11; use gpt-5.6-sol or gpt-6.1-sol"),
        "o3": ("soon", "shuts down 2026-12-11"),
    },
    "deepseek": {
        "deepseek-chat": ("gone", "discontinued 2026-07-24; use deepseek-flash"),
        "deepseek-reasoner": ("gone", "discontinued 2026-07-24; use deepseek-flash"),
        "deepseek-v4-flash": ("alias", "retired alias; routed to deepseek-flash"),
    },
    "google": {
        "gemini-3-pro-preview": ("gone", "shut down 2026-03-09"),
        "gemini-2.0-flash": ("gone", "shut down 2026-06-01"),
        "gemini-3.7-flash": ("alias", "silently redirected to gemini-3.8-flash"),
        "gemini-3.5-flash": ("alias", "silently redirected to gemini-3.6-flash"),
    },
    "sarvam": {
        "deepseekv4-flash": ("gone", "old beta id; use deepseekv4.1-flash"),
        "sarvam-m": ("gone", "deprecated"),
        "sarvam-30b": ("gone", "deprecated (the API answers HTTP 400)"),
    },
}


def lookup(provider: str, model: str) -> ModelInfo | None:
    return CATALOG.get(provider, {}).get(model)


def retired(provider: str, model: str) -> Retired | None:
    return RETIRED.get(provider, {}).get(model)


_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max")
OFF_WORDS = frozenset({"off", "none", "disabled"})


def resolve_effort(info: ModelInfo | None, requested: str | None) -> str | None:
    """The reasoning value to put on the wire for ``requested``, or None to send nothing.

    Maps to the nearest value the model accepts (``minimal`` -> ``low`` on a model without
    ``minimal``), turns off-words into the model's own spelling, and drops the control entirely
    for models that have none, so a global setting never produces a 400 on an unrelated model.
    """
    if requested is None:
        return None
    wanted = requested.lower()
    if info is None or info.extra.get("unverified_metadata"):
        return wanted  # unknown model: trust the user's explicit choice
    if info.thinking == "none" or not info.efforts:
        return None
    if wanted in OFF_WORDS:
        for spelling in ("none", "off"):
            if spelling in info.efforts:
                return spelling
        return None
    if wanted in info.efforts:
        return wanted
    if wanted not in _ORDER:
        return None
    rank = _ORDER.index(wanted)
    ranked = [
        (abs(_ORDER.index(e) - rank), _ORDER.index(e), e) for e in info.efforts if e in _ORDER
    ]
    return min(ranked)[2] if ranked else None


def effort_is_off(requested: str | None) -> bool:
    return requested is not None and requested.lower() in OFF_WORDS


_CLAUDE = re.compile(r"claude-(?:opus|sonnet|haiku|fable|mythos)-(\d+)(?:[-.](\d+))?")


def anthropic_style(model: str) -> Thinking:
    """Thinking control for an Anthropic id that is not in the catalog (4.7+ is adaptive)."""
    match = _CLAUDE.search(model)
    if match:
        major = int(match.group(1))
        minor = int(match.group(2)) if match.group(2) and len(match.group(2)) < 3 else 0
        if (major, minor) >= (4, 7):
            return "adaptive"
        return "budget"
    return "adaptive"


def default_info(provider: str, model: str, *, context: int, max_output: int) -> ModelInfo:
    """Conservative metadata for an id we know nothing about."""
    return ModelInfo(model, context, max_output, extra={"unverified_metadata": True})


def resolve(provider: str, model: str, *, context: int, max_output: int) -> ModelInfo:
    return lookup(provider, model) or default_info(
        provider, model, context=context, max_output=max_output
    )
