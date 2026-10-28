"""Live model discovery and its on-disk cache, so model lists and limits do not go stale.

``list_models`` asks a provider's models endpoint what it serves and normalises the answer to
``Discovered`` records; ``refresh`` does that for every configured provider at once and saves the
results; ``load_cache``/``merge`` fold the cache into the built-in catalog.

Wire shapes were read from the providers' official documentation on 2026-10-23:

* Anthropic ``GET /v1/models`` (``max_input_tokens``, ``max_tokens``, ``capabilities.*``,
  ``lifecycle``, ``retires_at``, cursor pagination with ``after_id``/``has_more``).
* Gemini ``GET /v1beta/models`` (``inputTokenLimit``, ``outputTokenLimit``,
  ``supportedGenerationMethods``, ``thinking``; ``pageToken`` pagination).
* OpenAI ``GET /v1/models`` (``id``/``created``/``owned_by``/``shutdown_date`` only), DeepSeek
  ``GET /models`` (``context_window``, ``max_output_tokens``, ``input_modalities``, ``effort``),
  Sarvam ``GET /v2/models`` (``id`` only), Cerebras (``id`` only), Groq (``context_window``,
  ``max_completion_tokens``, ``active``), Mistral (``max_context_length``, ``capabilities``,
  ``deprecation``; from the official Python SDK's model-card reference), Fireworks' gateway
  ``ListModels`` (``contextLength``, ``supportsTools``, ``supportsImageInput``,
  ``deprecationDate``), OpenRouter (``context_length``, ``top_provider``, per-token ``pricing``
  strings, ``supported_parameters``, ``reasoning.supported_efforts``, ``expiration_date``) and
  Ollama ``GET /api/tags`` + ``POST /api/show`` (``capabilities``, ``model_info``).

Not confirmed against current official docs (the pages do not document the response, and
unauthenticated calls are rejected): Together's ``/v1/models`` body and Fireworks'
OpenAI-compatible ``/inference/v1/models`` body. They are parsed leniently from the fields those
APIs have historically returned (Together: a bare list with ``type``/``context_length``/
``pricing.input|output`` in USD per MTok; Fireworks: ``supports_chat``/``supports_tools``/
``supports_image_input``/``context_length``). Every field is optional, so a wrong guess degrades
to "id only" rather than failing.

Policy: a provider's answer is untrusted input. Token counts are accepted only when they are
positive integers no larger than ``_MAX_TOKENS``; a cache that is not a private regular file
owned by the current user is ignored; the API key is sent only in request headers and is
scrubbed from every error message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from kalash.llm.catalog import ModelInfo, Price
from kalash.llm.errors import ErrorCode, LLMError, from_http
from kalash.llm.http import make_client
from kalash.paths import kalash_home
from kalash.secure_io import atomic_private_write

if TYPE_CHECKING:
    from kalash.llm.providers import ProviderSpec

log = logging.getLogger(__name__)

__all__ = [
    "Discovered",
    "cache_path",
    "kind_for",
    "list_models",
    "load_cache",
    "merge",
    "refresh",
    "save_cache",
]

# Larger values are treated as a provider bug rather than a context window.
_MAX_TOKENS = 100_000_000
_MAX_PAGES = 20
_OLLAMA_CONCURRENCY = 8
_CACHE_VERSION = 1
_FUTURE_SKEW_S = 300.0
_ANTHROPIC_VERSION = "2023-06-01"

_DEFAULT_BASE = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "deepseek": "https://api.deepseek.com",
    "sarvam": "https://api.sarvam.ai",
    "ollama": "http://localhost:11434",
}
_OPENAI_KINDS = frozenset({"openai", "responses", "openai-compat"})


@dataclass(frozen=True)
class Discovered:
    """What a provider's models endpoint says about one model. ``None`` means "not reported"."""

    id: str
    display_name: str | None = None
    context: int | None = None
    max_output: int | None = None
    vision: bool | None = None
    tools: bool | None = None
    reasoning: bool | None = None
    price: Price | None = None  # USD per million tokens
    efforts: tuple[str, ...] = ()
    retired: bool = False  # deprecated, past retires_at/shutdown_date/expiration, or inactive
    note: str = ""


# --------------------------------------------------------------------------- value helpers


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _wall() -> float:
    return time.time()


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _tokens(value: Any) -> int | None:
    """A plausible token count: a positive integer (also integral floats / digit strings)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = int(value) if len(value) <= 12 and value.isascii() and value.isdigit() else None
    elif isinstance(value, float):
        value = int(value) if value.is_integer() else None
    if isinstance(value, int) and 0 < value <= _MAX_TOKENS:
        return value
    return None


def _first_tokens(source: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        found = _tokens(source.get(key))
        if found is not None:
            return found
    return None


def _modalities(value: Any) -> set[str] | None:
    if isinstance(value, list | tuple):
        names = {v.lower() for v in value if isinstance(v, str)}
        return names or None
    return None


def _money(value: Any, scale: int) -> float | None:
    """``value * scale`` as a float via Decimal (so "0.00000015" -> 0.15 exactly)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value).strip()) * scale
        result = float(amount)
    except (ArithmeticError, ValueError):
        return None
    return result if amount.is_finite() and 0 <= result < 1e9 else None


def _price(pricing: Any) -> Price | None:
    """OpenRouter-style ``prompt``/``completion`` are USD per *token*; ``input``/``output``
    (Together) are already per million tokens. Negative values (OpenRouter's ``-1`` for
    "variable") mean there is no usable price."""
    if not isinstance(pricing, dict):
        return None
    if "prompt" in pricing or "completion" in pricing:
        scale = 1_000_000
        inp, out = pricing.get("prompt"), pricing.get("completion")
        cache_read, cache_write = pricing.get("input_cache_read"), pricing.get("input_cache_write")
    else:
        scale = 1
        inp, out = pricing.get("input"), pricing.get("output")
        cache_read, cache_write = pricing.get("cache_read"), pricing.get("cache_write")
    inp_m, out_m = _money(inp, scale), _money(out, scale)
    if inp_m is None or out_m is None:
        return None
    return Price(inp_m, out_m, _money(cache_read, scale), _money(cache_write, scale))


_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def _efforts(values: Any) -> tuple[str, ...]:
    """Distinct effort names, weakest first (providers list them in arbitrary order)."""
    if not isinstance(values, list | tuple):
        return ()
    seen = list(dict.fromkeys(v for v in values if isinstance(v, str) and v))
    known = [e for e in _EFFORT_ORDER if e in seen]
    return tuple(known + [v for v in seen if v not in _EFFORT_ORDER])


def _when(value: Any) -> datetime | None:
    """An ISO date/datetime string or a ``{year, month, day}`` object, as an aware UTC time."""
    try:
        if isinstance(value, str) and value.strip():
            parsed = datetime.fromisoformat(value.strip())
        elif isinstance(value, dict) and isinstance(value.get("year"), int):
            parsed = datetime(value["year"], value.get("month") or 1, value.get("day") or 1)
        else:
            return None
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _sunset(value: Any, now: datetime) -> tuple[bool, str]:
    """(already passed, human note) for a retirement date; ``(False, "")`` if unparseable."""
    when = _when(value)
    if when is None:
        return False, ""
    day = when.date().isoformat()
    return (True, f"retired {day}") if when <= now else (False, f"retires {day}")


# --------------------------------------------------------------------------- HTTP


def _scrub(text: str, secret: str | None) -> str:
    """Hide the API key (e.g. a server echoing it back, or h11 quoting a bad header value)."""
    return text.replace(secret, "***") if secret and len(secret) >= 4 else text


async def _request_json(
    http: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    provider: str,
    secret: str | None,
    timeout: float,
    headers: Mapping[str, str],
    params: Mapping[str, str | int] | None = None,
    body: Mapping[str, Any] | None = None,
) -> Any:
    """One request; every failure is an ``LLMError`` and never mentions the API key."""
    try:
        response = await http.request(
            method,
            url,
            headers=dict(headers),
            params=params,
            json=None if body is None else dict(body),
            timeout=timeout,
        )
    except httpx.TimeoutException as error:
        raise LLMError(
            f"request timed out ({type(error).__name__})", ErrorCode.TIMEOUT, provider=provider
        ) from error
    # The two handlers below drop the exception chain (``from None``): the original message
    # can quote request headers, and a traceback would print it unscrubbed.
    except httpx.InvalidURL as error:
        raise LLMError(
            f"invalid URL: {_scrub(str(error), secret)}",
            ErrorCode.INVALID_REQUEST,
            provider=provider,
        ) from None
    except httpx.HTTPError as error:
        raise LLMError(
            f"connection failed ({type(error).__name__}): {_scrub(str(error), secret)}",
            ErrorCode.TRANSPORT,
            provider=provider,
        ) from None
    if not 200 <= response.status_code < 300:
        failure = from_http(response.status_code, response.content, response.headers, provider)
        raise LLMError(
            _scrub(failure.message, secret),
            failure.code,
            status=failure.status,
            retry_after=failure.retry_after,
            provider=provider,
        )
    try:
        return response.json()
    except ValueError as error:
        raise LLMError(
            "model list is not valid JSON",
            ErrorCode.MALFORMED_RESPONSE,
            status=response.status_code,
            provider=provider,
        ) from error


def _items(payload: Any, provider: str, *keys: str) -> list[Any]:
    """The model array of a list response (a bare array is accepted too)."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            if isinstance(payload.get(key), list):
                found: list[Any] = payload[key]
                return found
    raise LLMError(
        "model list has an unexpected shape", ErrorCode.MALFORMED_RESPONSE, provider=provider
    )


def _collect(
    items: list[Any], parse: Callable[[dict[str, Any]], Discovered | None]
) -> list[Discovered]:
    found: list[Discovered] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        model = parse(item)
        if model is not None and model.id not in seen:
            seen.add(model.id)
            found.append(model)
    return found


def _dedupe(models: list[Discovered]) -> list[Discovered]:
    first: dict[str, Discovered] = {}
    for model in models:
        first.setdefault(model.id, model)
    return list(first.values())


def _bearer(key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"} if key else {}


# --------------------------------------------------------------------------- parsers

_NON_CHAT_TYPES = frozenset(
    {
        "embedding",
        "embeddings",
        "image",
        "image_generation",
        "rerank",
        "reranker",
        "moderation",
        "audio",
        "transcribe",
        "transcription",
        "stt",
        "tts",
        "speech",
        "video",
    }
)
# Ids that are plainly not chat models, for lists (OpenAI, Groq, Mistral) with no type field.
_NON_CHAT_ID = re.compile(
    r"(?:^|[/\-_.:])(?:tts|stt|whisper|dall-e|sora|gpt-image|rerank\w*|embed\w*"
    r"|\w*moderation|\w*transcribe)"
)


def _not_chat(item: Mapping[str, Any], model_id: str, *, by_id: bool) -> bool:
    for key in ("type", "kind", "mode"):
        kind = item.get(key)
        if isinstance(kind, str) and kind.lower().removesuffix("_model") in _NON_CHAT_TYPES:
            return True
    if item.get("supports_chat") is False or item.get("supportsChat") is False:
        return True
    caps = item.get("capabilities")
    if isinstance(caps, dict) and caps.get("completion_chat") is False:
        return True
    arch = item.get("architecture")
    arch = arch if isinstance(arch, dict) else {}
    out = _modalities(_first(item.get("output_modalities"), arch.get("output_modalities")))
    if out is not None and "text" not in out:
        return True
    return by_id and _NON_CHAT_ID.search(model_id.lower()) is not None


def _parse_openai_like(
    item: dict[str, Any], now: datetime, *, by_id: bool = True
) -> Discovered | None:
    """OpenAI-shaped entries plus the optional extras compat providers add (see module doc)."""
    model_id = _text(item.get("id"))
    if model_id is None:
        name = _text(item.get("name"))  # Fireworks gateway: resource name is the model id
        model_id = name if name and name.startswith("accounts/") else None
    if model_id is None or _not_chat(item, model_id, by_id=by_id):
        return None
    caps = item.get("capabilities")
    caps = caps if isinstance(caps, dict) else {}
    arch = item.get("architecture")
    arch = arch if isinstance(arch, dict) else {}
    top = item.get("top_provider")
    top = top if isinstance(top, dict) else {}
    params = item.get("supported_parameters")
    params = params if isinstance(params, list) else None

    inputs = _modalities(_first(item.get("input_modalities"), arch.get("input_modalities")))
    vision = _first(
        _flag(item.get("supports_image_input")),
        _flag(item.get("supportsImageInput")),
        _flag(caps.get("vision")),
        None if inputs is None else "image" in inputs,
    )
    tools = _first(
        _flag(item.get("supports_tools")),
        _flag(item.get("supportsTools")),
        _flag(caps.get("function_calling")),
        None if params is None else "tools" in params,
    )
    reasoning_block = item.get("reasoning")
    effort_block = item.get("effort")
    efforts = _efforts(
        effort_block.get("supported_levels") if isinstance(effort_block, dict) else None
    )
    if not efforts and isinstance(reasoning_block, dict):
        efforts = _efforts(reasoning_block.get("supported_efforts"))
    reasoning = _first(
        _flag(reasoning_block),
        True if isinstance(reasoning_block, dict) else None,
        True if isinstance(effort_block, dict) else None,
        _flag(caps.get("reasoning")),
        None if params is None else ("reasoning" in params or "include_reasoning" in params),
    )

    retired, notes = False, []
    for key in ("shutdown_date", "expiration_date", "deprecation", "deprecationDate", "retires_at"):
        gone, note = _sunset(item.get(key), now)
        retired = retired or gone
        if note:
            notes.append(note)
    if item.get("active") is False:
        retired = True
        notes.append("inactive")

    display = _text(_first(item.get("display_name"), item.get("displayName"), item.get("name")))
    return Discovered(
        id=model_id,
        display_name=display if display != model_id else None,
        context=_first(
            _first_tokens(
                item,
                "context_length",
                "context_window",
                "max_model_len",
                "max_context_length",
                "contextLength",
            ),
            _tokens(top.get("context_length")),
        ),
        max_output=_first(
            _first_tokens(item, "max_completion_tokens", "max_output_tokens"),
            _tokens(top.get("max_completion_tokens")),
        ),
        vision=vision,
        tools=tools,
        reasoning=reasoning,
        price=_price(item.get("pricing")),
        efforts=efforts,
        retired=retired,
        note="; ".join(notes),
    )


_ANTHROPIC_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _supported(node: Any) -> bool | None:
    return _flag(node.get("supported")) if isinstance(node, dict) else None


def _parse_anthropic(item: dict[str, Any], now: datetime) -> Discovered | None:
    model_id = _text(item.get("id"))
    if model_id is None:
        return None
    caps = item.get("capabilities")
    caps = caps if isinstance(caps, dict) else {}
    effort = caps.get("effort")
    efforts: tuple[str, ...] = ()
    if isinstance(effort, dict) and _supported(effort) is not False:
        efforts = tuple(level for level in _ANTHROPIC_EFFORTS if _supported(effort.get(level)))
    lifecycle = item.get("lifecycle")
    notes: list[str] = ["deprecated"] if lifecycle == "deprecated" else []
    gone, note = _sunset(item.get("retires_at"), now)
    if note:
        notes.append(note)
    elif lifecycle == "retired":
        notes.append("retired")
    retired = lifecycle in ("deprecated", "retired")
    return Discovered(
        id=model_id,
        display_name=_text(item.get("display_name")),
        context=_tokens(item.get("max_input_tokens")),  # docs' sample shows a 0 placeholder
        max_output=_tokens(item.get("max_tokens")),
        vision=_supported(caps.get("image_input")),
        reasoning=_supported(caps.get("thinking")),
        efforts=efforts,
        retired=retired or gone,
        note="; ".join(notes),
    )


_GEMINI_SKIP_SUBSTRINGS = ("embedding", "imagen")
_GEMINI_SKIP_TOKENS = frozenset({"aqa", "veo", "tts"})


def _parse_gemini(item: dict[str, Any], now: datetime) -> Discovered | None:
    name = _text(item.get("name"))
    methods = item.get("supportedGenerationMethods")
    if name is None or not isinstance(methods, list) or "generateContent" not in methods:
        return None
    model_id = name.removeprefix("models/")
    lowered = model_id.lower()
    if any(s in lowered for s in _GEMINI_SKIP_SUBSTRINGS):
        return None
    if _GEMINI_SKIP_TOKENS.intersection(re.split(r"[-_.:/]", lowered)):
        return None
    return Discovered(
        id=model_id,
        display_name=_text(item.get("displayName")),
        context=_tokens(item.get("inputTokenLimit")),
        max_output=_tokens(item.get("outputTokenLimit")),
        reasoning=_flag(item.get("thinking")),
    )


def _str_list(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _ollama_context(info: Any) -> int | None:
    """``model_info`` keys are prefixed by the architecture: ``<arch>.context_length``."""
    if not isinstance(info, dict):
        return None
    arch = info.get("general.architecture")
    if isinstance(arch, str):
        found = _tokens(info.get(f"{arch}.context_length"))
        if found is not None:
            return found
    for key, value in info.items():
        if isinstance(key, str) and key.endswith(".context_length") and key.count(".") == 1:
            found = _tokens(value)
            if found is not None:
                return found
    return None


def _parse_ollama(
    name: str, summary: Mapping[str, Any], show: Mapping[str, Any] | None
) -> Discovered | None:
    caps = _str_list((show or {}).get("capabilities")) or _str_list(summary.get("capabilities"))
    if "embedding" in caps and "completion" not in caps:
        return None
    thinking = (show or {}).get("thinking")
    levels = thinking.get("values") if isinstance(thinking, dict) else None
    known = bool(caps)
    return Discovered(
        id=name,
        context=_ollama_context((show or {}).get("model_info")),
        vision=("vision" in caps) if known else None,
        tools=("tools" in caps) if known else None,
        reasoning=("thinking" in caps) if known else None,
        efforts=_efforts(levels),
    )


# --------------------------------------------------------------------------- list_models

_Lister = Callable[
    [httpx.AsyncClient, str, str, str | None, float, datetime], Awaitable[list[Discovered]]
]


def _root(base_url: str, kind: str) -> str:
    return (base_url or _DEFAULT_BASE.get(kind, "")).rstrip("/")


async def _list_openai(
    http: httpx.AsyncClient,
    provider: str,
    base_url: str,
    key: str | None,
    timeout: float,
    now: datetime,
) -> list[Discovered]:
    payload = await _request_json(
        http,
        "GET",
        f"{_root(base_url, 'openai')}/models",
        provider=provider,
        secret=key,
        timeout=timeout,
        headers=_bearer(key),
    )
    return _collect(
        _items(payload, provider, "data", "models"), lambda i: _parse_openai_like(i, now)
    )


async def _list_openrouter(
    http: httpx.AsyncClient,
    provider: str,
    base_url: str,
    key: str | None,
    timeout: float,
    now: datetime,
) -> list[Discovered]:
    payload = await _request_json(
        http,
        "GET",
        f"{_root(base_url, 'openrouter')}/models",
        provider=provider,
        secret=key,
        timeout=timeout,
        headers=_bearer(key),
    )
    return _collect(
        _items(payload, provider, "data"), lambda i: _parse_openai_like(i, now, by_id=False)
    )


async def _list_deepseek(
    http: httpx.AsyncClient,
    provider: str,
    base_url: str,
    key: str | None,
    timeout: float,
    now: datetime,
) -> list[Discovered]:
    # The configured route is the Anthropic-format one; the list lives at the OpenAI-format root.
    root = _root(base_url, "deepseek").removesuffix("/anthropic")
    payload = await _request_json(
        http,
        "GET",
        f"{root}/models",
        provider=provider,
        secret=key,
        timeout=timeout,
        headers=_bearer(key),
    )
    return _collect(_items(payload, provider, "data"), lambda i: _parse_openai_like(i, now))


async def _list_sarvam(
    http: httpx.AsyncClient,
    provider: str,
    base_url: str,
    key: str | None,
    timeout: float,
    now: datetime,
) -> list[Discovered]:
    # Only /v2/models exists; the configured base is usually the /v1 chat route.
    root = re.sub(r"/v[12]$", "", _root(base_url, "sarvam"))
    headers = {"api-subscription-key": key} if key else {}
    payload = await _request_json(
        http,
        "GET",
        f"{root}/v2/models",
        provider=provider,
        secret=key,
        timeout=timeout,
        headers=headers,
    )
    return _collect(_items(payload, provider, "data"), lambda i: _parse_openai_like(i, now))


async def _list_anthropic(
    http: httpx.AsyncClient,
    provider: str,
    base_url: str,
    key: str | None,
    timeout: float,
    now: datetime,
) -> list[Discovered]:
    root = _root(base_url, "anthropic").removesuffix("/v1")
    headers = {"anthropic-version": _ANTHROPIC_VERSION}
    if key:
        headers["x-api-key"] = key
    found: list[Discovered] = []
    after: str | None = None
    seen_cursors: set[str] = set()
    for _ in range(_MAX_PAGES):
        params: dict[str, str | int] = {"limit": 1000}
        if after:
            params["after_id"] = after
        payload = await _request_json(
            http,
            "GET",
            f"{root}/v1/models",
            provider=provider,
            secret=key,
            timeout=timeout,
            headers=headers,
            params=params,
        )
        items = _items(payload, provider, "data")
        found.extend(_collect(items, lambda i: _parse_anthropic(i, now)))
        if not isinstance(payload, dict) or payload.get("has_more") is not True:
            break
        tail = items[-1] if items and isinstance(items[-1], dict) else {}
        last = _text(payload.get("last_id")) or _text(tail.get("id"))
        if not last or last in seen_cursors:
            break
        seen_cursors.add(last)
        after = last
    return _dedupe(found)


async def _list_gemini(
    http: httpx.AsyncClient,
    provider: str,
    base_url: str,
    key: str | None,
    timeout: float,
    now: datetime,
) -> list[Discovered]:
    # The OpenAI-compat route (…/v1beta/openai) only lists ids; the native list has the limits.
    root = _root(base_url, "gemini").removesuffix("/openai")
    headers = {"x-goog-api-key": key} if key else {}
    found: list[Discovered] = []
    token: str | None = None
    seen_tokens: set[str] = set()
    for _ in range(_MAX_PAGES):
        params: dict[str, str | int] = {"pageSize": 1000}
        if token:
            params["pageToken"] = token
        payload = await _request_json(
            http,
            "GET",
            f"{root}/models",
            provider=provider,
            secret=key,
            timeout=timeout,
            headers=headers,
            params=params,
        )
        found.extend(_collect(_items(payload, provider, "models"), lambda i: _parse_gemini(i, now)))
        token = _text(payload.get("nextPageToken")) if isinstance(payload, dict) else None
        if not token or token in seen_tokens:
            break
        seen_tokens.add(token)
    return _dedupe(found)


def _ollama_host(base_url: str) -> str:
    host = _root(base_url, "ollama").removesuffix("/v1")
    return host if host.startswith(("http://", "https://")) else f"http://{host}"


async def _list_ollama(
    http: httpx.AsyncClient,
    provider: str,
    base_url: str,
    key: str | None,
    timeout: float,
    now: datetime,
) -> list[Discovered]:
    host = _ollama_host(base_url)
    headers = _bearer(key)
    tags = await _request_json(
        http,
        "GET",
        f"{host}/api/tags",
        provider=provider,
        secret=key,
        timeout=timeout,
        headers=headers,
    )
    entries: list[tuple[str, dict[str, Any]]] = []
    for entry in _items(tags, provider, "models"):
        name = _text(entry.get("model") or entry.get("name")) if isinstance(entry, dict) else None
        if name is not None:
            entries.append((name, entry))
    gate = asyncio.Semaphore(_OLLAMA_CONCURRENCY)

    async def show(name: str) -> dict[str, Any] | None:
        async with gate:
            try:
                reply = await _request_json(
                    http,
                    "POST",
                    f"{host}/api/show",
                    provider=provider,
                    secret=key,
                    timeout=timeout,
                    headers=headers,
                    body={"model": name},
                )
            except LLMError:
                return None  # one broken model must not hide the rest
        return reply if isinstance(reply, dict) else None

    tasks = [asyncio.create_task(show(name)) for name, _ in entries]
    try:
        # A hung server cannot stall the whole refresh: whatever is unanswered after `timeout`
        # falls back to what /api/tags alone says.
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    found: list[Discovered] = []
    seen: set[str] = set()
    for (name, summary), task in zip(entries, tasks, strict=True):
        done_ok = task.done() and not task.cancelled() and task.exception() is None
        model = _parse_ollama(name, summary, task.result() if done_ok else None)
        if model is not None and model.id not in seen:
            seen.add(model.id)
            found.append(model)
    return found


_LISTERS: dict[str, _Lister] = {
    "anthropic": _list_anthropic,
    "openai": _list_openai,
    "responses": _list_openai,
    "openai-compat": _list_openai,
    "openrouter": _list_openrouter,
    "gemini": _list_gemini,
    "deepseek": _list_deepseek,
    "sarvam": _list_sarvam,
    "ollama": _list_ollama,
}


async def list_models(
    kind: str,
    provider: str,
    base_url: str,
    api_key: str | None,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 15.0,
) -> list[Discovered]:
    """List the chat models a provider serves.

    ``kind`` selects the endpoint/parser (``anthropic``, ``openai``/``responses``/
    ``openai-compat``, ``openrouter``, ``gemini``, ``deepseek``, ``sarvam``, ``ollama``).
    ``timeout`` bounds each request; the whole call is bounded by three times that. Every
    failure is an ``LLMError``; the API key is never part of its message.
    """
    lister = _LISTERS.get(kind.lower())
    if lister is None:
        raise LLMError(
            f"no model-list endpoint is known for kind {kind!r}",
            ErrorCode.INVALID_REQUEST,
            provider=provider,
        )
    http = client or make_client(timeout)
    try:
        async with asyncio.timeout(timeout * 3):
            return await lister(http, provider, base_url, api_key, timeout, _utcnow())
    except TimeoutError as error:
        raise LLMError(
            f"listing models took longer than {timeout * 3:g}s",
            ErrorCode.TIMEOUT,
            provider=provider,
        ) from error
    finally:
        if client is None:
            await http.aclose()


# --------------------------------------------------------------------------- cache


def cache_path() -> Path:
    return kalash_home() / "models-cache.json"


def _encode(model: Discovered) -> dict[str, Any]:
    data = asdict(model)
    data["efforts"] = list(model.efforts)
    return data


def _decode(raw: Any) -> Discovered | None:
    if not isinstance(raw, dict):
        return None
    model_id = _text(raw.get("id"))
    if model_id is None:
        return None
    price = None
    stored = raw.get("price")
    if isinstance(stored, dict):
        inp, out = _money(stored.get("input"), 1), _money(stored.get("output"), 1)
        if inp is not None and out is not None:
            price = Price(
                inp, out, _money(stored.get("cache_read"), 1), _money(stored.get("cache_write"), 1)
            )
    return Discovered(
        id=model_id,
        display_name=_text(raw.get("display_name")),
        context=_tokens(raw.get("context")),
        max_output=_tokens(raw.get("max_output")),
        vision=_flag(raw.get("vision")),
        tools=_flag(raw.get("tools")),
        reasoning=_flag(raw.get("reasoning")),
        price=price,
        efforts=_efforts(raw.get("efforts")),
        retired=raw.get("retired") is True,
        note=raw["note"] if isinstance(raw.get("note"), str) else "",
    )


def _trusted(path: Path) -> bool:
    """A cache we may believe: a regular file nobody else can write, owned by this user."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        return False
    return not (os.name == "posix" and (info.st_mode & 0o022 or info.st_uid != os.getuid()))


def _read_providers(path: Path) -> dict[str, dict[str, Any]]:
    """Well-formed per-provider entries of the cache file; ``{}`` if absent or untrusted."""
    if not _trusted(path):
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    providers = raw["providers"]
    if raw.get("version") != _CACHE_VERSION or not isinstance(providers, dict):
        return {}
    return {
        pid: entry
        for pid, entry in providers.items()
        if isinstance(pid, str)
        and isinstance(entry, dict)
        and isinstance(entry.get("models"), list)
        and isinstance(entry.get("fetched_at"), int | float)
        and not isinstance(entry.get("fetched_at"), bool)
    }


def load_cache(max_age_s: float = 86400) -> dict[str, list[Discovered]]:
    """Cached model lists younger than ``max_age_s``; stale, corrupt or untrusted data is
    ignored. Never raises."""
    try:
        entries = _read_providers(cache_path())
    except Exception:
        return {}
    now = _wall()
    loaded: dict[str, list[Discovered]] = {}
    for provider, entry in entries.items():
        try:
            age = now - float(entry["fetched_at"])
            if age > max_age_s or age < -_FUTURE_SKEW_S:
                continue
            decoded = [_decode(raw) for raw in entry["models"]]
        except Exception:
            continue
        loaded[provider] = [m for m in decoded if m is not None]
    return loaded


def _save_many(batch: Mapping[str, list[Discovered]]) -> None:
    path = cache_path()
    try:
        providers = _read_providers(path)
    except Exception:
        providers = {}  # corrupt or untrusted: start over rather than refuse to cache
    stamp = _wall()
    for provider, models in batch.items():
        providers[provider] = {"fetched_at": stamp, "models": [_encode(m) for m in models]}
    payload = {"version": _CACHE_VERSION, "providers": providers}
    atomic_private_write(path, json.dumps(payload, indent=1, sort_keys=True).encode("utf-8"))


def save_cache(provider: str, models: list[Discovered]) -> None:
    """Record ``models`` for ``provider`` (atomic, mode 0600); other providers are kept."""
    _save_many({provider: models})


# --------------------------------------------------------------------------- merge


def _live_int(value: int | None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _overlay(info: ModelInfo, live: Discovered) -> ModelInfo:
    """Curated fields win; live data supplies limits and vision, and fills what is blank."""
    changes: dict[str, Any] = {}
    if (context := _live_int(live.context)) is not None:
        changes["context"] = context
    if (max_output := _live_int(live.max_output)) is not None:
        changes["max_output"] = max_output
    if live.vision is not None:
        changes["vision"] = live.vision
    if info.price is None and live.price is not None:
        changes["price"] = live.price
    if not info.efforts and live.efforts:
        changes["efforts"] = live.efforts
    if not info.note and live.note:
        changes["note"] = live.note
    return replace(info, **changes) if changes else info


def _new_info(live: Discovered, default_context: int, default_output: int) -> ModelInfo:
    """A model we know only from discovery: conservative, no assumptions about wire quirks."""
    context = _live_int(live.context) or default_context
    max_output = _live_int(live.max_output) or min(default_output, context)
    extra: dict[str, object] = {"discovered": True}
    if live.display_name:
        extra["display_name"] = live.display_name
    if live.reasoning is not None:
        extra["reasoning"] = live.reasoning
    if live.efforts:
        extra["efforts"] = list(live.efforts)
    return ModelInfo(
        live.id,
        context,
        max_output,
        price=live.price,
        vision=bool(live.vision),
        tools=live.tools is not False,
        note=live.note,
        extra=extra,
    )


def merge(
    static: dict[str, ModelInfo],
    discovered: list[Discovered],
    *,
    default_context: int,
    default_output: int,
) -> dict[str, ModelInfo]:
    """Fold live discovery into a static catalog (neither input is modified).

    Curated entries keep pricing, efforts, thinking style and sampling flags but take live
    context/max_output/vision when reported and positive; blank price/efforts/note are filled.
    New ids are appended after the curated ones, alphabetically. Retired ids are dropped unless
    that would leave nothing.
    """
    live: dict[str, Discovered] = {}
    for model in discovered:
        live.setdefault(model.id, model)
    merged: dict[str, ModelInfo] = {}
    retired: set[str] = set()
    for model_id, info in static.items():
        found = live.get(model_id)
        merged[model_id] = info if found is None else _overlay(info, found)
        if found is not None and found.retired:
            retired.add(model_id)
    for model_id in sorted((i for i in live if i not in static), key=lambda i: (i.lower(), i)):
        merged[model_id] = _new_info(live[model_id], default_context, default_output)
        if live[model_id].retired:
            retired.add(model_id)
    kept = {k: v for k, v in merged.items() if k not in retired}
    return kept or merged


# --------------------------------------------------------------------------- refresh

_PROVIDER_KINDS = {
    "openrouter": "openrouter",
    "sarvam": "sarvam",
    "ollama": "ollama",
    "google": "gemini",
}
_SPEC_KINDS = {
    "anthropic": "anthropic",
    "deepseek": "deepseek",
    "gemini": "gemini",
    "responses": "openai",
    "openai": "openai",
}


def kind_for(spec: ProviderSpec) -> str | None:
    """The discovery kind for a provider, or ``None`` if its kind has no models endpoint.

    Providers with their own list format are matched by id first. ``google`` is listed through
    the native Gemini endpoint (the OpenAI-compat list carries ids only) even while its
    ``ProviderSpec.kind`` is ``openai``."""
    return _PROVIDER_KINDS.get(spec.id) or _SPEC_KINDS.get(str(getattr(spec, "kind", "")))


def _describe(error: LLMError) -> str:
    status = f" (HTTP {error.status})" if error.status else ""
    return f"{error.code.value}{status}: {error.message}"


async def refresh(
    provider_specs: Mapping[str, ProviderSpec],
    *,
    only: str | None = None,
    client: httpx.AsyncClient | None = None,
    timeout: float = 15.0,
) -> tuple[dict[str, list[Discovered]], dict[str, str]]:
    """List models for every provider with a usable key (Ollama needs none), concurrently.

    Returns ``(results, errors)`` keyed by provider id. A provider that fails only appears in
    ``errors``; the others are unaffected. Non-empty successes are written to the cache. Without
    ``only``, providers lacking a key are skipped silently; with it they are reported.
    """
    from kalash.llm.providers import ProviderError, api_key_for, base_url_for

    results: dict[str, list[Discovered]] = {}
    errors: dict[str, str] = {}
    if only is not None and only not in provider_specs:
        errors[only] = f"unknown provider {only!r}"
    jobs: list[tuple[str, str, str, str | None]] = []
    for provider, spec in provider_specs.items():
        if only is not None and provider != only:
            continue
        kind = kind_for(spec)
        if kind is None:
            if only is not None:
                errors[provider] = f"no model-list endpoint for provider kind {spec.kind!r}"
            continue
        try:
            key = api_key_for(spec)
        except ProviderError as error:
            errors[provider] = str(error)
            continue
        if getattr(spec, "requires_key", True) and not key:
            if only is not None:
                errors[provider] = f"no API key for {spec.name}"
            continue
        jobs.append((provider, kind, base_url_for(spec, ""), key))

    async def one(http: httpx.AsyncClient, job: tuple[str, str, str, str | None]) -> None:
        provider, kind, base, key = job
        try:
            results[provider] = await list_models(
                kind, provider, base, key, client=http, timeout=timeout
            )
        except LLMError as error:
            errors[provider] = _describe(error)
        except Exception as error:  # a bug in one parser must not take the others down
            errors[provider] = _scrub(f"unexpected {type(error).__name__}: {error}", key)

    if jobs:
        http = client or make_client(timeout)
        try:
            await asyncio.gather(*(one(http, job) for job in jobs))
        finally:
            if client is None:
                await http.aclose()
    saved = {p: m for p, m in results.items() if m}
    if saved:
        try:
            _save_many(saved)
        except OSError as error:
            log.warning("could not write the model cache: %s", error)
    return results, errors
