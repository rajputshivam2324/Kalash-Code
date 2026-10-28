"""Known provider routes and construction of a configured adapter.

A model is addressed as ``provider/model`` (the model part may itself contain ``/``, as on
OpenRouter). Credentials come from the provider's environment variables first, then from
``kalash provider``. Model metadata (limits, reasoning controls, prices) lives in
``catalog.py``; this module only says *how to reach* each provider.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import httpx

from kalash.credentials import CredentialError, get_credential
from kalash.llm import catalog
from kalash.llm.anthropic import AnthropicAdapter
from kalash.llm.catalog import ModelInfo
from kalash.llm.gemini import GeminiAdapter
from kalash.llm.http import Adapter, make_client
from kalash.llm.openai_chat import OpenAIChatAdapter, OpenAIDialect
from kalash.llm.openai_responses import OpenAIResponsesAdapter

# openai: Chat Completions | responses: OpenAI Responses | anthropic: Anthropic Messages |
# deepseek / compat: Messages-compatible endpoints (thinking replay differs) | gemini: native
Kind = Literal["openai", "responses", "anthropic", "deepseek", "compat", "gemini"]
KINDS = ("openai", "responses", "anthropic", "deepseek", "compat", "gemini")


@dataclass
class ProviderSpec:
    id: str
    name: str
    kind: Kind
    base_url: str
    env_keys: tuple[str, ...] = ()
    context_window: int = 128_000  # for ids the catalog does not know
    default_max_tokens: int | None = None  # ditto; catalog models use their own limit
    requires_key: bool = True
    token_param: str = "max_tokens"  # chat completions: max_tokens | max_completion_tokens
    reasoning_style: str = "effort"  # chat completions: effort | object (OpenRouter)
    extra_headers: dict[str, str] = field(default_factory=dict)
    key_header: str | None = None  # send the key in this header instead of Authorization
    model_base_url: dict[str, str] = field(default_factory=dict)  # per-model endpoint override
    anthropic_version: str = "2023-06-01"
    docs: str = ""

    @property
    def models(self) -> tuple[str, ...]:
        """Built-in IDs, including available models with incomplete metadata."""
        return tuple(
            dict.fromkeys(
                (
                    *catalog.CATALOG.get(self.id, {}),
                    *catalog.MODEL_LISTINGS.get(self.id, {}),
                )
            )
        )


# glm5.3-flash confirmed by GET /v2/models on 2026-10-28. See the live judge report.
_SARVAM_V2 = {"glm5.3", "glm5.3-flash", "gemma4", "deepseekv4.1-flash"}

PROVIDERS: dict[str, ProviderSpec] = {
    p.id: p
    for p in [
        ProviderSpec(
            "anthropic",
            "Anthropic",
            "anthropic",
            "https://api.anthropic.com",
            ("ANTHROPIC_API_KEY",),
            context_window=1_000_000,
            default_max_tokens=64_000,
        ),
        ProviderSpec(
            "openai",
            "OpenAI",
            "responses",
            "https://api.openai.com/v1",
            ("OPENAI_API_KEY",),
            context_window=1_050_000,
            default_max_tokens=64_000,
        ),
        ProviderSpec(
            "google",
            "Google Gemini",
            "gemini",
            "https://generativelanguage.googleapis.com/v1beta",
            ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
            context_window=1_048_576,
            default_max_tokens=32_000,
        ),
        ProviderSpec(
            "deepseek",
            "DeepSeek",
            "deepseek",
            "https://api.deepseek.com/anthropic",
            ("DEEPSEEK_API_KEY",),
            context_window=1_000_000,
            default_max_tokens=64_000,
        ),
        ProviderSpec(
            "xai",
            "xAI Grok",
            "responses",
            "https://api.x.ai/v1",
            ("XAI_API_KEY",),
            context_window=500_000,
            default_max_tokens=64_000,
        ),
        ProviderSpec(
            "moonshot",
            "Moonshot Kimi",
            "openai",
            "https://api.moonshot.ai/v1",
            ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
            context_window=1_000_000,
            default_max_tokens=64_000,
        ),
        ProviderSpec(
            "zai",
            "Z.ai GLM",
            "openai",
            "https://api.z.ai/api/paas/v4",
            ("ZAI_API_KEY", "ZHIPU_API_KEY"),
            context_window=1_000_000,
            default_max_tokens=64_000,
        ),
        ProviderSpec(
            "minimax",
            "MiniMax",
            "compat",
            "https://api.minimax.io/anthropic",
            ("MINIMAX_API_KEY",),
            context_window=1_000_000,
            default_max_tokens=64_000,
        ),
        ProviderSpec(
            "sarvam",
            "Sarvam",
            "openai",
            "https://api.sarvam.ai/v1",
            ("SARVAM_API_KEY",),
            context_window=128_000,
            default_max_tokens=8_192,
            key_header="api-subscription-key",
            model_base_url=dict.fromkeys(_SARVAM_V2, "https://api.sarvam.ai/v2"),
        ),
        ProviderSpec(
            "openrouter",
            "OpenRouter",
            "openai",
            "https://openrouter.ai/api/v1",
            ("OPENROUTER_API_KEY",),
            context_window=200_000,
            default_max_tokens=32_000,
            reasoning_style="object",
            extra_headers={
                "X-OpenRouter-Title": "Kalash",
                "HTTP-Referer": "https://github.com/kalash-code",
            },
        ),
        ProviderSpec(
            "groq",
            "Groq",
            "openai",
            "https://api.groq.com/openai/v1",
            ("GROQ_API_KEY",),
            context_window=131_072,
            default_max_tokens=16_384,
            token_param="max_completion_tokens",
        ),
        ProviderSpec(
            "together",
            "Together AI",
            "openai",
            "https://api.together.ai/v1",
            ("TOGETHER_API_KEY",),
            context_window=128_000,
            default_max_tokens=16_384,
        ),
        ProviderSpec(
            "fireworks",
            "Fireworks AI",
            "openai",
            "https://api.fireworks.ai/inference/v1",
            ("FIREWORKS_API_KEY",),
            context_window=128_000,
            default_max_tokens=16_384,
        ),
        ProviderSpec(
            "cerebras",
            "Cerebras",
            "openai",
            "https://api.cerebras.ai/v1",
            ("CEREBRAS_API_KEY",),
            context_window=131_072,
            default_max_tokens=16_384,
        ),
        ProviderSpec(
            "ollama",
            "Ollama (local)",
            "openai",
            "http://localhost:11434/v1",
            (),
            context_window=32_768,
            default_max_tokens=8_192,
            requires_key=False,
        ),
    ]
}


class ProviderError(Exception):
    """The model route cannot be resolved or has no credential."""


@dataclass
class ModelRef:
    provider: str
    model: str

    def __str__(self) -> str:
        return f"{self.provider}/{self.model}"


def custom_spec(provider_id: str, raw: dict[str, Any]) -> ProviderSpec:
    kind = raw.get("kind", "openai")
    if kind not in KINDS:
        raise ProviderError(f"providers.{provider_id}.kind must be one of: {', '.join(KINDS)}")
    base = raw.get("base_url")
    if not isinstance(base, str) or not base:
        raise ProviderError(f"providers.{provider_id}.base_url is required")
    env = raw.get("api_key_env")
    return ProviderSpec(
        provider_id,
        str(raw.get("name", provider_id)),
        kind,
        base,
        (env,) if isinstance(env, str) else (),
        context_window=int(raw.get("context_window", 128_000)),
        default_max_tokens=raw.get("max_tokens"),
        requires_key=bool(raw.get("requires_key", isinstance(env, str))),
        token_param=str(raw.get("token_param", "max_tokens")),
    )


# Models a user lists for their own provider (not in the catalog) are remembered here so that
# `parse_model` can resolve bare names and the model picker can show them.
_CUSTOM_MODELS: dict[str, tuple[str, ...]] = {}


def all_specs(custom: dict[str, dict[str, Any]] | None = None) -> dict[str, ProviderSpec]:
    specs = dict(PROVIDERS)
    for provider_id, raw in (custom or {}).items():
        if isinstance(raw.get("models"), list):
            _CUSTOM_MODELS[provider_id] = tuple(str(m) for m in raw["models"])
        if provider_id in specs and "kind" not in raw:
            # Override fields of a built-in provider (e.g. base_url for a proxy).
            allowed = {"base_url", "context_window", "anthropic_version"}
            specs[provider_id] = replace(
                specs[provider_id], **{k: v for k, v in raw.items() if k in allowed}
            )
        else:
            specs[provider_id] = custom_spec(provider_id, raw)
    return specs


def catalog_for(spec: ProviderSpec) -> dict[str, ModelInfo]:
    """The curated catalog overlaid with the last live discovery (if cached and fresh)."""
    from kalash.llm import discovery

    static = dict(catalog.CATALOG.get(spec.id, {}))
    for model_id in spec.models:
        if model_id not in static:
            listing = catalog.MODEL_LISTINGS[spec.id][model_id]
            info = catalog.default_info(
                spec.id,
                model_id,
                context=listing.context or spec.context_window,
                max_output=spec.default_max_tokens or 16_384,
            )
            static[model_id] = replace(
                info,
                vision=listing.vision if listing.vision is not None else info.vision,
                tools=listing.tools if listing.tools is not None else info.tools,
                note="output limit/pricing unverified",
            )
    live = discovery.load_cache().get(spec.id)
    if not live:
        return static
    return discovery.merge(
        static,
        live,
        default_context=spec.context_window,
        default_output=spec.default_max_tokens or 16_384,
    )


def known_models(spec: ProviderSpec) -> tuple[str, ...]:
    ids = tuple(catalog_for(spec))
    return ids or _CUSTOM_MODELS.get(spec.id, ())


def parse_model(value: str, specs: dict[str, ProviderSpec]) -> ModelRef:
    """Parse ``provider/model``. A bare model name resolves only when exactly one provider
    lists it."""
    value = value.strip()
    prefix, sep, rest = value.partition("/")
    if sep and prefix in specs and rest:
        return ModelRef(prefix, rest)
    owners = [p.id for p in specs.values() if value in known_models(p)]
    if len(owners) == 1:
        return ModelRef(owners[0], value)
    known = ", ".join(sorted(specs))
    if len(owners) > 1:
        raise ProviderError(
            f"model {value!r} is offered by {', '.join(owners)}; write it as provider/model"
        )
    raise ProviderError(
        f"cannot resolve model {value!r}; use provider/model with provider one of: {known}"
    )


def api_key_for(spec: ProviderSpec) -> str | None:
    for name in spec.env_keys:
        if os.environ.get(name):
            return os.environ[name]
    try:
        return get_credential(spec.id)
    except CredentialError as error:
        raise ProviderError(str(error)) from error


def has_credential(spec: ProviderSpec) -> bool:
    if not spec.requires_key:
        return True
    try:
        return bool(api_key_for(spec))
    except ProviderError:
        return False


def base_url_for(spec: ProviderSpec, model: str) -> str:
    try:
        override = get_credential(f"{spec.id}:base_url")
    except CredentialError:
        override = None
    if override:
        return override
    if spec.id == "ollama" and os.environ.get("OLLAMA_HOST"):
        host = os.environ["OLLAMA_HOST"]
        host = host if host.startswith("http") else f"http://{host}"
        return f"{host.rstrip('/')}/v1"
    return spec.model_base_url.get(model, spec.base_url)


def model_info(spec: ProviderSpec, model: str, *, window: int | None = None) -> ModelInfo:
    """Catalog entry for the model, or conservative defaults sized from the provider spec."""
    max_output = spec.default_max_tokens or 16_384
    info = catalog_for(spec).get(model) or catalog.default_info(
        spec.id, model, context=spec.context_window, max_output=max_output
    )
    if window and window != info.context:
        info = replace(info, context=window)
    return info


def model_problem(provider: str, model: str) -> tuple[str, str] | None:
    """('error', text) for a retired id, ('warning', text) for one retiring or renamed."""
    entry = catalog.retired(provider, model)
    if entry is None:
        return None
    status, text = entry
    return ("error" if status == "gone" else "warning", f"{provider}/{model}: {text}")


def build_adapter(
    ref: ModelRef,
    specs: dict[str, ProviderSpec],
    *,
    context_window: int | None = None,
    idle_timeout: float = 300.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Adapter:
    spec = specs.get(ref.provider)
    if spec is None:
        raise ProviderError(f"unknown provider {ref.provider!r}")
    problem = model_problem(ref.provider, ref.model)
    if problem and problem[0] == "error":
        raise ProviderError(problem[1])
    key = api_key_for(spec)
    if spec.requires_key and not key:
        env = " or ".join(spec.env_keys) or "an API key"
        raise ProviderError(
            f"no API key for {spec.name}: set {env} or run `kalash provider {spec.id}`"
        )
    info = model_info(spec, ref.model, window=context_window)
    base = base_url_for(spec, ref.model)
    # Gemini asks for >= 20 minute client timeouts on long decodes; others use the idle default.
    client = make_client(
        max(idle_timeout, 1200.0) if spec.kind == "gemini" else idle_timeout, transport
    )
    common: dict[str, Any] = {
        "provider": spec.id,
        "model": ref.model,
        "base_url": base,
        "api_key": key,
        "context_window": info.context,
        "client": client,
    }
    if spec.kind == "responses":
        return OpenAIResponsesAdapter(**common, info=info, extra_headers=dict(spec.extra_headers))
    if spec.kind == "gemini":
        return GeminiAdapter(**common, info=info)
    if spec.kind in ("anthropic", "deepseek", "compat"):
        return AnthropicAdapter(
            **common,
            default_max_tokens=info.max_output,
            dialect=spec.kind,
            info=info,
            version=spec.anthropic_version,
        )
    headers = dict(spec.extra_headers)
    auth = "bearer"
    if spec.key_header:
        auth = "none"
        if key:
            headers[spec.key_header] = key
    dialect = OpenAIDialect(
        token_param=spec.token_param,
        reasoning_style=spec.reasoning_style,
        auth=auth,
        extra_headers=headers,
    )
    return OpenAIChatAdapter(**common, dialect=dialect, info=info)
