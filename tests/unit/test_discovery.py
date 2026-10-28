"""Live model discovery: per-provider wire parsing, error mapping, cache and catalog merge.

Payloads are written from each provider's documented models-list response (see the module
docstring of ``kalash.llm.discovery``); everything runs over ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from kalash.llm import discovery
from kalash.llm.catalog import ModelInfo, Price
from kalash.llm.discovery import (
    Discovered,
    cache_path,
    kind_for,
    list_models,
    load_cache,
    merge,
    refresh,
    save_cache,
)
from kalash.llm.errors import ErrorCode, LLMError
from kalash.llm.providers import PROVIDERS, ProviderSpec
from tests.harness.wire import Recorder, json_error

PAST = "2000-01-01"
FUTURE = "2099-12-31"


@pytest.fixture
async def http():
    """Build mock-transport clients; all are closed at teardown."""
    made: list[httpx.AsyncClient] = []

    def make(handler: Callable[[httpx.Request], Any]) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        made.append(client)
        return client

    yield make
    for client in made:
        await client.aclose()


def ok(payload: Any) -> httpx.Response:
    return httpx.Response(200, json=payload)


async def run(
    http, kind: str, payload: Any, *, key: str | None = "k", base: str = "https://x.test"
):
    """List models from one canned 200 response; returns (models, recorder)."""
    recorder = Recorder(ok(payload))
    models = await list_models(kind, "p", base, key, client=http(recorder))
    return models, recorder


def by_id(models: list[Discovered]) -> dict[str, Discovered]:
    return {m.id: m for m in models}


# ----------------------------------------------------------------------------- anthropic


def _claude(model_id: str, **kw: Any) -> dict[str, Any]:
    return {"type": "model", "id": model_id, "display_name": model_id.title(), **kw}


async def test_anthropic_reads_limits_and_capabilities_and_sends_auth_headers(http):
    caps = {
        "image_input": {"supported": True},
        "thinking": {"supported": True, "types": {"adaptive": {"supported": True}}},
        "effort": {
            "supported": True,
            "low": {"supported": True},
            "medium": {"supported": True},
            "high": {"supported": True},
            "max": {"supported": True},
            "xhigh": None,
        },
    }
    payload = {
        "data": [
            _claude(
                "claude-opus-5-5",
                max_input_tokens=1_000_000,
                max_tokens=128_000,
                capabilities=caps,
                lifecycle="active",
                retires_at=None,
            )
        ],
        "first_id": "claude-opus-5-5",
        "last_id": "claude-opus-5-5",
        "has_more": False,
    }
    models, recorder = await run(
        http, "anthropic", payload, key="sk-ant-1", base="https://api.anthropic.com"
    )
    (model,) = models
    assert model == Discovered(
        "claude-opus-5-5",
        display_name="Claude-Opus-5-5",
        context=1_000_000,
        max_output=128_000,
        vision=True,
        reasoning=True,
        efforts=("low", "medium", "high", "max"),
    )
    request = recorder.requests[0]
    assert request.method == "GET" and request.url.path == "/v1/models"
    assert request.headers["x-api-key"] == "sk-ant-1"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.url.params["limit"] == "1000"
    assert "authorization" not in request.headers


async def test_anthropic_follows_has_more_with_after_id(http):
    first = {"data": [_claude("a"), _claude("b")], "has_more": True, "last_id": "b"}
    second = {"data": [_claude("c")], "has_more": False, "last_id": "c"}
    recorder = Recorder(ok(first), ok(second))
    models = await list_models("anthropic", "p", "https://x.test", "k", client=http(recorder))
    assert [m.id for m in models] == ["a", "b", "c"]
    assert len(recorder.requests) == 2
    assert "after_id" not in recorder.requests[0].url.params
    assert recorder.requests[1].url.params["after_id"] == "b"


async def test_anthropic_pagination_cannot_loop_forever(http):
    page = {"data": [_claude("a")], "has_more": True, "last_id": "a"}
    recorder = Recorder(*[ok(page)] * 5)
    models = await list_models("anthropic", "p", "https://x.test", "k", client=http(recorder))
    assert [m.id for m in models] == ["a"]
    assert len(recorder.requests) == 2  # the repeated cursor is detected on the second page


@pytest.mark.parametrize(
    ("fields", "retired", "note"),
    [
        ({"lifecycle": "retired", "retires_at": PAST}, True, "retired 2000-01-01"),
        ({"lifecycle": "retired"}, True, "retired"),
        ({"lifecycle": "deprecated"}, True, "deprecated"),
        ({"lifecycle": "deprecated", "retires_at": FUTURE}, True, "deprecated; retires 2099-12-31"),
        ({"lifecycle": "active", "retires_at": "2000-06-01T00:00:00Z"}, True, "retired 2000-06-01"),
        ({"lifecycle": "active", "retires_at": FUTURE}, False, "retires 2099-12-31"),
        ({"lifecycle": "active", "retires_at": None}, False, ""),
        ({}, False, ""),
    ],
)
async def test_anthropic_retirement_detection(http, fields, retired, note):
    models, _ = await run(http, "anthropic", {"data": [_claude("m", **fields)]})
    assert (models[0].retired, models[0].note) == (retired, note)


async def test_anthropic_zero_placeholders_and_null_capabilities_are_not_reported(http):
    payload = {"data": [_claude("m", max_input_tokens=0, max_tokens=0, capabilities=None)]}
    (model,), _ = await run(http, "anthropic", payload)
    assert (model.context, model.max_output, model.vision, model.reasoning) == (
        None,
        None,
        None,
        None,
    )
    assert model.efforts == ()


async def test_anthropic_effort_unsupported_yields_no_levels(http):
    caps = {
        "effort": {"supported": False, "low": {"supported": True}},
        "image_input": {"supported": False},
    }
    (model,), _ = await run(http, "anthropic", {"data": [_claude("m", capabilities=caps)]})
    assert model.efforts == () and model.vision is False


async def test_anthropic_base_url_ending_in_v1_is_not_doubled(http):
    _, recorder = await run(http, "anthropic", {"data": []}, base="https://proxy.test/v1/")
    assert recorder.requests[0].url.path == "/v1/models"


# ----------------------------------------------------------------------------- openai & compat


async def test_openai_list_is_ids_only_with_bearer_auth(http):
    payload = {
        "object": "list",
        "data": [
            {"id": "gpt-6-sol", "object": "model", "created": 1686935002, "owned_by": "openai"},
            {
                "id": "gpt-6-luna",
                "object": "model",
                "created": 1,
                "owned_by": "openai",
                "shutdown_date": None,
            },
        ],
    }
    models, recorder = await run(
        http, "openai", payload, key="sk-1", base="https://api.openai.com/v1"
    )
    assert models == [Discovered("gpt-6-sol"), Discovered("gpt-6-luna")]
    request = recorder.requests[0]
    assert str(request.url) == "https://api.openai.com/v1/models"
    assert request.headers["authorization"] == "Bearer sk-1"


@pytest.mark.parametrize("kind", ["openai", "responses", "openai-compat"])
async def test_every_openai_style_kind_lists_from_base_models(http, kind):
    models, recorder = await run(http, kind, {"data": [{"id": "m"}]}, base="https://x.test/v1/")
    assert [m.id for m in models] == ["m"]
    assert recorder.requests[0].url.path == "/v1/models"


async def test_openai_shutdown_date_marks_retired_only_once_it_has_passed(http, monkeypatch):
    monkeypatch.setattr(discovery, "_utcnow", lambda: datetime(2026, 10, 23, 9, 0, tzinfo=UTC))
    payload = {
        "data": [
            {"id": "o1", "shutdown_date": "2026-10-23"},  # today: already gone
            {"id": "gpt-5", "shutdown_date": "2026-12-11"},
            {"id": "gpt-6-sol", "shutdown_date": None},
            {"id": "o3-mini", "shutdown_date": "2026-10-22"},
        ]
    }
    models, _ = await run(http, "openai", payload)
    found = by_id(models)
    assert found["o1"].retired and found["o3-mini"].retired
    assert not found["gpt-5"].retired and found["gpt-5"].note == "retires 2026-12-11"
    assert not found["gpt-6-sol"].retired and found["gpt-6-sol"].note == ""


async def test_openai_list_drops_plainly_non_chat_ids(http):
    ids = [
        "gpt-6-sol",
        "whisper-1",
        "tts-1-hd",
        "text-embedding-3-small",
        "dall-e-3",
        "omni-moderation-latest",
        "gpt-4o-mini-transcribe",
        "gpt-image-1",
        "sora-2",
        "o3",
        "playai-tts",
        "mistral-embed",
        "codestral-latest",
    ]
    models, _ = await run(http, "openai", {"data": [{"id": i} for i in ids]})
    assert [m.id for m in models] == ["gpt-6-sol", "o3", "codestral-latest"]


async def test_groq_reads_context_window_max_completion_tokens_and_active(http):
    payload = {
        "object": "list",
        "data": [
            {
                "id": "openai/gpt-oss-120b",
                "object": "model",
                "created": 1693721698,
                "owned_by": "OpenAI",
                "active": True,
                "context_window": 131072,
                "public_apps": None,
                "max_completion_tokens": 65536,
            },
            {"id": "old-model", "object": "model", "active": False, "context_window": 8192},
            {"id": "whisper-large-v3", "object": "model", "active": True, "context_window": 448},
        ],
    }
    models, _ = await run(http, "openai", payload)
    found = by_id(models)
    assert set(found) == {"openai/gpt-oss-120b", "old-model"}
    assert (found["openai/gpt-oss-120b"].context, found["openai/gpt-oss-120b"].max_output) == (
        131072,
        65536,
    )
    assert not found["openai/gpt-oss-120b"].retired
    assert found["old-model"].retired and found["old-model"].note == "inactive"


async def test_together_bare_array_with_types_and_per_mtok_pricing(http):
    payload = [
        {
            "id": "zai-org/GLM-5.3",
            "object": "model",
            "type": "chat",
            "display_name": "GLM-5.3",
            "context_length": 1048575,
            "pricing": {"input": 1.4, "output": 4.4, "hourly": 0},
        },
        {"id": "BAAI/bge-large", "type": "embedding", "context_length": 512},
        {"id": "black-forest-labs/FLUX.2-pro", "type": "image"},
        {"id": "mixedbread-ai/rerank", "type": "rerank"},
    ]
    models, _ = await run(http, "openai-compat", payload)
    (model,) = models
    assert model.id == "zai-org/GLM-5.3" and model.display_name == "GLM-5.3"
    assert model.context == 1048575
    assert model.price == Price(1.4, 4.4)


async def test_fireworks_flags_in_snake_and_camel_case(http):
    payload = {
        "object": "list",
        "data": [
            {
                "id": "accounts/fireworks/models/kimi-k3",
                "object": "model",
                "owned_by": "fireworks",
                "kind": "HF_BASE_MODEL",
                "supports_chat": True,
                "supports_tools": True,
                "supports_image_input": True,
                "context_length": 1_040_000,
            },
            {
                "id": "accounts/fireworks/models/nomic-embed",
                "kind": "EMBEDDING_MODEL",
                "supports_chat": False,
            },
            {"id": "accounts/fireworks/models/completion-only", "supports_chat": False},
            {
                "name": "accounts/fireworks/models/glm-5p3",
                "contextLength": 1_040_000,
                "supportsTools": False,
                "supportsImageInput": False,
                "deprecationDate": {"year": 2000, "month": 1, "day": 2},
            },
        ],
    }
    models, _ = await run(http, "openai-compat", payload)
    found = by_id(models)
    assert set(found) == {"accounts/fireworks/models/kimi-k3", "accounts/fireworks/models/glm-5p3"}
    kimi = found["accounts/fireworks/models/kimi-k3"]
    assert (kimi.context, kimi.tools, kimi.vision, kimi.retired) == (1_040_000, True, True, False)
    glm = found["accounts/fireworks/models/glm-5p3"]
    assert (glm.context, glm.tools, glm.vision, glm.retired) == (1_040_000, False, False, True)
    assert glm.note == "retired 2000-01-02"


async def test_cerebras_list_has_only_ids(http):
    payload = {
        "object": "list",
        "data": [
            {"id": "gpt-oss-120b", "object": "model", "created": 0, "owned_by": "Cerebras"},
            {"id": "qwen-3.8-27b", "object": "model", "created": 0, "owned_by": "Cerebras"},
        ],
    }
    models, _ = await run(http, "openai-compat", payload)
    assert models == [Discovered("gpt-oss-120b"), Discovered("qwen-3.8-27b")]


async def test_mistral_capabilities_context_and_deprecation(http):
    payload = {
        "object": "list",
        "data": [
            {
                "id": "mistral-large-latest",
                "object": "model",
                "name": "mistral-large-3",
                "max_context_length": 262144,
                "type": "base",
                "deprecation": None,
                "capabilities": {
                    "completion_chat": True,
                    "function_calling": True,
                    "vision": True,
                    "reasoning": False,
                },
            },
            {
                "id": "mistral-ocr-latest",
                "max_context_length": 32768,
                "capabilities": {"completion_chat": False, "ocr": True},
            },
            {
                "id": "open-mistral-7b",
                "max_context_length": 32768,
                "deprecation": "2000-03-30",
                "capabilities": {"completion_chat": True, "function_calling": False},
            },
        ],
    }
    models, _ = await run(http, "openai-compat", payload)
    found = by_id(models)
    assert set(found) == {"mistral-large-latest", "open-mistral-7b"}
    large = found["mistral-large-latest"]
    assert (large.context, large.tools, large.vision, large.reasoning) == (
        262144,
        True,
        True,
        False,
    )
    assert large.display_name == "mistral-large-3" and not large.retired
    old = found["open-mistral-7b"]
    assert old.retired and old.tools is False and old.note == "retired 2000-03-30"


async def test_models_whose_output_modalities_lack_text_are_not_chat_models(http):
    payload = {
        "data": [
            {"id": "paint", "output_modalities": ["image"]},
            {"id": "mixed", "output_modalities": ["text", "image"]},
            {"id": "nested", "architecture": {"output_modalities": ["audio"]}},
        ]
    }
    models, _ = await run(http, "openai-compat", payload)
    assert [m.id for m in models] == ["mixed"]


async def test_garbage_entries_missing_ids_and_duplicates_are_skipped(http):
    payload = {
        "data": [
            "junk",
            7,
            None,
            {"object": "model"},
            {"id": ""},
            {"id": "a"},
            {"id": "a", "context_window": 5},
            {"id": "b"},
        ]
    }
    models, _ = await run(http, "openai", payload)
    assert models == [Discovered("a"), Discovered("b")]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (131072, 131072),
        (131072.0, 131072),
        ("131072", 131072),
        (0, None),
        (-5, None),
        (True, None),
        (1.5, None),
        ("1e6", None),
        (10**12, None),
        ("9" * 5000, None),
        (None, None),
    ],
)
async def test_implausible_token_counts_are_treated_as_not_reported(http, raw, expected):
    (model,), _ = await run(http, "openai", {"data": [{"id": "m", "context_window": raw}]})
    assert model.context == expected


# ----------------------------------------------------------------------------- openrouter


def _or_item(model_id: str, **kw: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": model_id,
        "name": f"Vendor: {model_id}",
        "context_length": 262144,
        "architecture": {
            "modality": "text->text",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
        },
        "pricing": {"prompt": "0.0000003", "completion": "0.0000012"},
        "top_provider": {"context_length": 262144, "max_completion_tokens": 131072},
        "supported_parameters": ["max_tokens", "temperature", "tools"],
        "expiration_date": None,
    }
    item.update(kw)
    return item


async def test_openrouter_converts_per_token_price_strings_to_per_mtok(http):
    item = _or_item(
        "minimax/minimax-m3.1-flash",
        pricing={
            "prompt": "0.00000015",
            "completion": "0.0000006",
            "input_cache_read": "0.000000015",
            "input_cache_write": "0.00000019",
            "web_search": "0.01",
        },
    )
    (model,), recorder = await run(
        http, "openrouter", {"data": [item]}, base="https://openrouter.ai/api/v1"
    )
    assert model.price == Price(0.15, 0.6, 0.015, 0.19)  # exact: Decimal, not binary-float scaling
    assert recorder.requests[0].url.path == "/api/v1/models"
    assert (model.context, model.max_output) == (262144, 131072)
    assert model.display_name == "Vendor: minimax/minimax-m3.1-flash"


async def test_openrouter_real_catalog_entry_fetched_2026_10_23(http):
    # Verbatim shape of a live GET https://openrouter.ai/api/v1/models row (description trimmed).
    item = {
        "id": "minimax/minimax-m3.1-flash",
        "canonical_slug": "minimax/minimax-m3.1-flash-20261019",
        "hugging_face_id": None,
        "name": "MiniMax: MiniMax M3.1 Flash",
        "created": 1792379569,
        "description": "MiniMax-M3.1-Flash is a multimodal model from MiniMax.",
        "context_length": 1000000,
        "architecture": {
            "modality": "text+image+video->text",
            "input_modalities": ["text", "image", "video"],
            "output_modalities": ["text"],
            "tokenizer": "Other",
            "instruct_type": None,
        },
        "pricing": {
            "prompt": "0.00000015",
            "completion": "0.0000006",
            "input_cache_read": "0.000000015",
        },
        "top_provider": {
            "context_length": 1000000,
            "max_completion_tokens": 524288,
            "is_moderated": False,
        },
        "per_request_limits": None,
        "supported_parameters": ["include_reasoning", "max_tokens", "reasoning", "tools"],
        "default_parameters": {},
        "supported_voices": None,
        "knowledge_cutoff": None,
        "expiration_date": None,
        "links": {"details": "/api/v1/models/minimax/minimax-m3.1-flash-20261019/endpoints"},
        "reasoning": {"mandatory": True},
    }
    (model,), _ = await run(http, "openrouter", {"data": [item]})
    assert model == Discovered(
        "minimax/minimax-m3.1-flash",
        display_name="MiniMax: MiniMax M3.1 Flash",
        context=1_000_000,
        max_output=524_288,
        vision=True,
        tools=True,
        reasoning=True,
        price=Price(0.15, 0.6, 0.015),
    )


async def test_openrouter_free_variable_and_malformed_prices(http):
    payload = {
        "data": [
            _or_item("free:free", pricing={"prompt": "0", "completion": "0"}),
            _or_item("router/auto", pricing={"prompt": "-1", "completion": "-1"}),
            _or_item("odd/one", pricing={"prompt": "n/a", "completion": "0.000001"}),
            _or_item("no/pricing", pricing=None),
        ]
    }
    models, _ = await run(http, "openrouter", payload)
    found = by_id(models)
    assert found["free:free"].price == Price(0.0, 0.0)
    assert found["router/auto"].price is None
    assert found["odd/one"].price is None
    assert found["no/pricing"].price is None


async def test_openrouter_reads_modalities_tools_reasoning_and_efforts(http):
    payload = {
        "data": [
            _or_item(
                "vendor/vision-reasoner",
                architecture={"input_modalities": ["text", "image"], "output_modalities": ["text"]},
                supported_parameters=["include_reasoning", "reasoning", "tools"],
                reasoning={
                    "mandatory": False,
                    "default_effort": "high",
                    "supported_efforts": ["max", "xhigh", "high", "medium", "low", "none"],
                },
            ),
            _or_item("vendor/plain", supported_parameters=["temperature"]),
            _or_item("vendor/include-only", supported_parameters=["include_reasoning", "tools"]),
        ]
    }
    models, _ = await run(http, "openrouter", payload)
    found = by_id(models)
    smart = found["vendor/vision-reasoner"]
    assert (smart.vision, smart.tools, smart.reasoning) == (True, True, True)
    assert smart.efforts == ("none", "low", "medium", "high", "xhigh", "max")
    plain = found["vendor/plain"]
    assert (plain.vision, plain.tools, plain.reasoning, plain.efforts) == (False, False, False, ())
    assert found["vendor/include-only"].reasoning is True


async def test_openrouter_expiration_date_and_non_text_outputs(http):
    payload = {
        "data": [
            _or_item("gone/model", expiration_date=PAST),
            _or_item("soon/model", expiration_date=FUTURE),
            _or_item(
                "paint/image",
                architecture={"input_modalities": ["text"], "output_modalities": ["image"]},
            ),
            _or_item(
                "voice/embed-ish", id="speak/embeddings-v1"
            ),  # kept: OpenRouter lists text->text only
        ]
    }
    models, _ = await run(http, "openrouter", payload)
    found = by_id(models)
    assert found["gone/model"].retired and found["gone/model"].note == "retired 2000-01-01"
    assert not found["soon/model"].retired and found["soon/model"].note == "retires 2099-12-31"
    assert "paint/image" not in found
    assert "speak/embeddings-v1" in found  # structured data decides, not id guessing


async def test_openrouter_works_without_a_key_because_the_catalog_is_public(http):
    _, recorder = await run(http, "openrouter", {"data": []}, key=None)
    assert "authorization" not in recorder.requests[0].headers


# ----------------------------------------------------------------------------- gemini


def _gem(name: str, methods: list[str] | None = None, **kw: Any) -> dict[str, Any]:
    return {
        "name": name,
        "baseModelId": name.removeprefix("models/"),
        "version": "001",
        "displayName": name.removeprefix("models/").title(),
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 65536,
        "supportedGenerationMethods": ["generateContent", "countTokens"]
        if methods is None
        else methods,
        **kw,
    }


async def test_gemini_strips_models_prefix_reads_limits_and_uses_goog_header(http):
    payload = {
        "models": [_gem("models/gemini-3.8-flash", thinking=True), _gem("models/gemini-2.5-pro")]
    }
    models, recorder = await run(
        http,
        "gemini",
        payload,
        key="AIza-secret",
        base="https://generativelanguage.googleapis.com/v1beta",
    )
    found = by_id(models)
    assert set(found) == {"gemini-3.8-flash", "gemini-2.5-pro"}
    flash = found["gemini-3.8-flash"]
    assert (flash.context, flash.max_output, flash.reasoning) == (1048576, 65536, True)
    assert flash.display_name == "Gemini-3.8-Flash"
    assert found["gemini-2.5-pro"].reasoning is None
    request = recorder.requests[0]
    assert request.url.path == "/v1beta/models"
    assert request.url.params["pageSize"] == "1000"
    assert request.headers["x-goog-api-key"] == "AIza-secret"
    assert "key" not in request.url.params and "authorization" not in request.headers


async def test_gemini_follows_next_page_token(http):
    recorder = Recorder(
        ok({"models": [_gem("models/a")], "nextPageToken": "tok2"}),
        ok({"models": [_gem("models/b")]}),
    )
    models = await list_models(
        "gemini", "google", "https://x.test/v1beta", "k", client=http(recorder)
    )
    assert [m.id for m in models] == ["a", "b"]
    assert "pageToken" not in recorder.requests[0].url.params
    assert recorder.requests[1].url.params["pageToken"] == "tok2"


async def test_gemini_keeps_only_generate_content_chat_models(http):
    names = {
        "models/gemini-3.6-flash": ["generateContent"],
        "models/gemini-embedding-001": ["embedContent"],
        "models/text-embedding-004": ["generateContent"],
        "models/aqa": ["generateAnswer"],
        "models/aqa-v2": ["generateContent"],
        "models/imagen-4.0-generate-001": ["generateContent"],
        "models/veo-3.1-generate-preview": ["generateContent"],
        "models/gemini-2.5-flash-preview-tts": ["generateContent"],
        "models/legacy-completion": ["generateText"],
        "models/no-methods-field": None,
    }
    items = [_gem(name, methods) for name, methods in names.items() if methods is not None]
    items.append({"name": "models/no-methods-field", "inputTokenLimit": 5})
    models, _ = await run(http, "gemini", {"models": items})
    assert [m.id for m in models] == ["gemini-3.6-flash"]


async def test_gemini_lists_natively_even_when_given_the_openai_compat_base(http):
    _, recorder = await run(
        http,
        "gemini",
        {"models": []},
        base="https://generativelanguage.googleapis.com/v1beta/openai",
    )
    assert recorder.requests[0].url.path == "/v1beta/models"


# ----------------------------------------------------------------------------- deepseek & sarvam


async def test_deepseek_lists_from_the_openai_root_not_the_anthropic_route(http):
    payload = {
        "object": "list",
        "data": [
            {
                "id": "deepseek-flash",
                "object": "model",
                "owned_by": "deepseek",
                "name": "DeepSeek V4.1 Flash",
                "context_window": 1_000_000,
                "max_output_tokens": 393_216,
                "input_modalities": ["text", "image"],
                "output_modalities": ["text"],
                "effort": {
                    "supported_levels": ["max", "low", "high", "medium"],
                    "default_level": "high",
                },
            },
            {
                "id": "deepseek-v4-pro",
                "object": "model",
                "owned_by": "deepseek",
                "context_window": 1_000_000,
                "max_output_tokens": 393_216,
                "input_modalities": ["text"],
            },
        ],
    }
    models, recorder = await run(
        http, "deepseek", payload, key="ds-key", base="https://api.deepseek.com/anthropic"
    )
    assert str(recorder.requests[0].url) == "https://api.deepseek.com/models"
    assert recorder.requests[0].headers["authorization"] == "Bearer ds-key"
    found = by_id(models)
    flash, pro = found["deepseek-flash"], found["deepseek-v4-pro"]
    assert (flash.context, flash.max_output, flash.vision, flash.reasoning) == (
        1_000_000,
        393_216,
        True,
        True,
    )
    assert flash.efforts == ("low", "medium", "high", "max")
    assert flash.display_name == "DeepSeek V4.1 Flash"
    assert (pro.vision, pro.efforts, pro.reasoning) == (False, (), None)


async def test_deepseek_base_without_anthropic_suffix_is_used_as_is(http):
    _, recorder = await run(http, "deepseek", {"data": []}, base="https://proxy.test/ds/")
    assert str(recorder.requests[0].url) == "https://proxy.test/ds/models"


@pytest.mark.parametrize(
    "base", ["https://api.sarvam.ai/v1", "https://api.sarvam.ai/v2/", "https://api.sarvam.ai"]
)
async def test_sarvam_always_lists_from_v2_models_with_the_subscription_key(http, base):
    payload = {
        "object": "list",
        "data": [
            {"id": "sarvam-105b", "object": "model", "created": 1, "owned_by": "sarvam"},
            {"id": "deepseekv4.1-flash", "object": "model", "created": 1, "owned_by": "sarvam"},
        ],
    }
    models, recorder = await run(http, "sarvam", payload, key="sv-key", base=base)
    assert [m.id for m in models] == ["sarvam-105b", "deepseekv4.1-flash"]
    request = recorder.requests[0]
    assert str(request.url) == "https://api.sarvam.ai/v2/models"
    assert request.headers["api-subscription-key"] == "sv-key"


# ----------------------------------------------------------------------------- ollama


def _ollama_server(
    shows: dict[str, Any], tags: list[dict[str, Any]], seen: list[httpx.Request] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path == "/api/tags":
            return ok({"models": tags})
        assert request.url.path == "/api/show" and request.method == "POST"
        reply = shows[json.loads(request.content)["model"]]
        return reply if isinstance(reply, httpx.Response) else ok(reply)

    return handler


def _tag(name: str) -> dict[str, Any]:
    return {"name": name, "model": name, "size": 1, "digest": "abc", "details": {"family": "x"}}


async def test_ollama_reads_capabilities_and_context_from_the_architecture_key(http):
    shows = {
        "gemma4:8b": {
            "capabilities": ["completion", "thinking", "vision", "tools"],
            "thinking": {"values": [False, True], "default": True},
            "model_info": {
                "general.architecture": "gemma4",
                "gemma4.context_length": 131072,
                "gemma4.vision.patch_size": 16,
            },
        },
        "gpt-oss:20b": {
            "capabilities": ["completion", "thinking", "tools"],
            "thinking": {"values": ["low", "medium", "high"], "default": "medium"},
            "model_info": {"general.architecture": "gptoss", "gptoss.context_length": 131072},
        },
        "tiny:1b": {
            "capabilities": ["completion"],
            "model_info": {"general.architecture": "llama", "llama.context_length": 4096},
        },
    }
    seen: list[httpx.Request] = []
    client = http(_ollama_server(shows, [_tag(n) for n in shows], seen))
    models = await list_models("ollama", "ollama", "http://localhost:11434", None, client=client)
    found = by_id(models)
    assert (found["gemma4:8b"].context, found["gemma4:8b"].vision, found["gemma4:8b"].tools) == (
        131072,
        True,
        True,
    )
    assert found["gemma4:8b"].reasoning is True and found["gemma4:8b"].efforts == ()
    assert found["gpt-oss:20b"].efforts == ("low", "medium", "high")
    assert found["gpt-oss:20b"].vision is False and found["gpt-oss:20b"].context == 131072
    assert (found["tiny:1b"].tools, found["tiny:1b"].reasoning, found["tiny:1b"].context) == (
        False,
        False,
        4096,
    )
    assert all("authorization" not in r.headers for r in seen)
    assert json.loads(seen[1].content) == {"model": "gemma4:8b"}  # the documented request field


async def test_ollama_context_falls_back_to_any_prefixed_context_length_key(http):
    shows = {
        "m": {
            "capabilities": ["completion"],
            "model_info": {"weird.context_length": 8192, "a.b.context_length": 1},
        }
    }
    (model,) = await list_models(
        "ollama", "ollama", "http://h", None, client=http(_ollama_server(shows, [_tag("m")]))
    )
    assert model.context == 8192


async def test_ollama_base_url_v1_suffix_and_missing_scheme_are_normalised(http):
    seen: list[httpx.Request] = []
    client = http(_ollama_server({}, [], seen))
    await list_models("ollama", "ollama", "gpu-box:11434/v1", None, client=client)
    assert str(seen[0].url) == "http://gpu-box:11434/api/tags"


async def test_ollama_skips_embedding_models(http):
    shows = {
        "nomic-embed-text": {"capabilities": ["embedding"], "model_info": {}},
        "qwen3.8:27b": {"capabilities": ["completion", "tools"], "model_info": {}},
    }
    models = await list_models(
        "ollama",
        "ollama",
        "http://h",
        None,
        client=http(_ollama_server(shows, [_tag(n) for n in shows])),
    )
    assert [m.id for m in models] == ["qwen3.8:27b"]


async def test_ollama_one_failing_model_does_not_hide_the_others(http):
    shows = {
        "good": {
            "capabilities": ["completion", "tools"],
            "model_info": {"general.architecture": "a", "a.context_length": 9000},
        },
        "broken": httpx.Response(500, json={"error": "model failed to load"}),
        "garbled": httpx.Response(200, content=b"not json"),
    }
    models = await list_models(
        "ollama",
        "ollama",
        "http://h",
        None,
        client=http(_ollama_server(shows, [_tag(n) for n in shows])),
    )
    found = by_id(models)
    assert set(found) == {"good", "broken", "garbled"}
    assert found["good"].context == 9000 and found["good"].tools is True
    assert found["broken"] == Discovered("broken") and found["garbled"] == Discovered("garbled")


async def test_ollama_falls_back_to_capabilities_in_tags_when_show_fails(http):
    tags = [{**_tag("cloud-model"), "capabilities": ["completion", "tools", "vision"]}]
    shows = {"cloud-model": httpx.Response(404, json={"error": "not found"})}
    (model,) = await list_models(
        "ollama", "ollama", "http://h", None, client=http(_ollama_server(shows, tags))
    )
    assert (model.tools, model.vision, model.reasoning, model.context) == (True, True, False, None)


async def test_ollama_show_concurrency_is_bounded_to_eight(http):
    active = peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        if request.url.path == "/api/tags":
            return ok({"models": [_tag(f"m{i}") for i in range(30)]})
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return ok({"capabilities": ["completion"]})

    models = await list_models("ollama", "ollama", "http://h", None, client=http(handler))
    assert len(models) == 30
    assert 1 < peak <= 8


async def test_ollama_total_time_is_bounded_when_the_server_hangs(http):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return ok({"models": [_tag("a"), _tag("b")]})
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    started = time.monotonic()
    models = await list_models(
        "ollama", "ollama", "http://h", None, client=http(handler), timeout=0.2
    )
    assert time.monotonic() - started < 3
    assert [m.id for m in models] == ["a", "b"]  # tags-only fallback, nothing lost


async def test_ollama_with_no_models_returns_an_empty_list(http):
    assert (
        await list_models("ollama", "ollama", "http://h", None, client=http(_ollama_server({}, [])))
        == []
    )


# ----------------------------------------------------------------------------- errors

ALL_KINDS = [
    "anthropic",
    "openai",
    "responses",
    "openai-compat",
    "openrouter",
    "gemini",
    "deepseek",
    "sarvam",
    "ollama",
]


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, ErrorCode.AUTH),
        (403, ErrorCode.AUTH),
        (500, ErrorCode.SERVER),
        (503, ErrorCode.SERVER),
        (404, ErrorCode.INVALID_REQUEST),
    ],
)
async def test_http_status_maps_to_an_error_code(http, status, code):
    client = http(Recorder(json_error(status, "nope")))
    with pytest.raises(LLMError) as info:
        await list_models("openai", "groq", "https://x.test", "k", client=client)
    assert (info.value.code, info.value.status, info.value.provider) == (code, status, "groq")
    assert not isinstance(info.value, httpx.HTTPError)


async def test_rate_limit_carries_retry_after(http):
    client = http(Recorder(json_error(429, "slow down", {"retry-after": "7"})))
    with pytest.raises(LLMError) as info:
        await list_models("anthropic", "anthropic", "https://x.test", "k", client=client)
    assert (
        info.value.code is ErrorCode.RATE_LIMIT
        and info.value.retry_after == 7.0
        and info.value.retryable
    )


@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_every_kind_rejects_invalid_json(http, kind):
    client = http(Recorder(httpx.Response(200, content=b"<html>gateway</html>")))
    with pytest.raises(LLMError) as info:
        await list_models(kind, "p", "https://x.test", "k", client=client)
    assert info.value.code is ErrorCode.MALFORMED_RESPONSE


@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize("payload", [{"unexpected": 1}, "text", 42, None])
async def test_every_kind_rejects_the_wrong_json_shape(http, kind, payload):
    client = http(Recorder(ok(payload)))
    with pytest.raises(LLMError) as info:
        await list_models(kind, "p", "https://x.test", "k", client=client)
    assert info.value.code is ErrorCode.MALFORMED_RESPONSE


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (httpx.ConnectError("connection refused"), ErrorCode.TRANSPORT),
        (httpx.RemoteProtocolError("server disconnected"), ErrorCode.TRANSPORT),
        (httpx.ReadTimeout("slow"), ErrorCode.TIMEOUT),
        (httpx.ConnectTimeout("slow"), ErrorCode.TIMEOUT),
    ],
)
async def test_transport_failures_never_leak_raw_httpx_exceptions(http, raised, code):
    def handler(request: httpx.Request) -> httpx.Response:
        raise raised

    with pytest.raises(LLMError) as info:
        await list_models("gemini", "google", "https://x.test", "k", client=http(handler))
    assert info.value.code is code and info.value.provider == "google"


async def test_a_whole_call_that_hangs_times_out_as_an_llm_error(http):
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    with pytest.raises(LLMError) as info:
        await list_models("openai", "p", "https://x.test", "k", client=http(handler), timeout=0.05)
    assert info.value.code is ErrorCode.TIMEOUT


async def test_unknown_kind_is_an_llm_error_not_a_crash():
    with pytest.raises(LLMError) as info:
        await list_models("telepathy", "p", "https://x.test", "k")
    assert info.value.code is ErrorCode.INVALID_REQUEST


async def test_an_invalid_base_url_is_an_llm_error(http):
    with pytest.raises(LLMError) as info:
        await list_models("openai", "p", "not a url \x00", "k", client=http(Recorder()))
    assert info.value.code in (ErrorCode.INVALID_REQUEST, ErrorCode.TRANSPORT)


SECRET = "sk-live-SECRET-0123456789"


@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize("failure", ["status", "transport"])
async def test_the_api_key_never_appears_in_an_error(http, kind, failure):
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "transport":
            # what h11 does for a key with a stray newline: it quotes the header value
            raise httpx.LocalProtocolError(f"Illegal header value {SECRET!r}", request=request)
        return json_error(401, f"Incorrect API key provided: {SECRET}. Find it at the dashboard.")

    with pytest.raises(LLMError) as info:
        await list_models(kind, "p", "https://x.test", SECRET, client=http(handler))
    error = info.value
    text = " | ".join([str(error), error.message, repr(error), *map(str, error.args)])
    assert SECRET not in text and "***" in error.message  # scrubbed, not merely omitted
    # a traceback prints the whole chain, so the chain must be clean too
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__


async def test_a_client_created_internally_is_closed(monkeypatch):
    created: list[httpx.AsyncClient] = []

    def fake_make_client(idle_timeout: float = 300.0, transport: Any = None) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(Recorder(ok({"data": [{"id": "m"}]})))
        )
        created.append(client)
        return client

    monkeypatch.setattr(discovery, "make_client", fake_make_client)
    assert [m.id for m in await list_models("openai", "p", "https://x.test", "k")] == ["m"]
    assert created[0].is_closed


async def test_a_caller_supplied_client_is_left_open(http):
    client = http(Recorder(ok({"data": []})))
    await list_models("openai", "p", "https://x.test", "k", client=client)
    assert not client.is_closed


# ----------------------------------------------------------------------------- cache


def sample() -> list[Discovered]:
    return [
        Discovered(
            "m1",
            display_name="Model One",
            context=200_000,
            max_output=64_000,
            vision=True,
            tools=True,
            reasoning=True,
            price=Price(0.15, 0.6, 0.015, 0.19),
            efforts=("low", "high"),
            retired=False,
            note="retires 2099-01-01",
        ),
        Discovered("m2", retired=True),
    ]


def test_cache_lives_in_the_kalash_home():
    assert cache_path().name == "models-cache.json"
    assert cache_path().parent == discovery.kalash_home()


def test_cache_roundtrip_preserves_every_field():
    save_cache("openrouter", sample())
    assert load_cache() == {"openrouter": sample()}


def test_missing_cache_loads_as_empty():
    assert load_cache() == {}


def test_cache_file_is_private_and_atomic():
    save_cache("a", sample())
    assert stat.S_IMODE(cache_path().stat().st_mode) == 0o600
    assert [p.name for p in cache_path().parent.iterdir() if p.name.endswith(".tmp")] == []
    save_cache("a", sample())  # overwriting keeps the mode
    assert stat.S_IMODE(cache_path().stat().st_mode) == 0o600


def test_saving_one_provider_keeps_the_others_and_replaces_its_own():
    save_cache("a", [Discovered("a1")])
    save_cache("b", [Discovered("b1")])
    save_cache("a", [Discovered("a2")])
    assert load_cache() == {"a": [Discovered("a2")], "b": [Discovered("b1")]}


def test_stale_providers_are_ignored_but_fresh_ones_kept(monkeypatch):
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(discovery, "_wall", lambda: clock["now"])
    save_cache("old", [Discovered("o")])
    clock["now"] += 90_000  # older than a day
    save_cache("new", [Discovered("n")])
    assert load_cache() == {"new": [Discovered("n")]}
    assert load_cache(max_age_s=10 * 86400).keys() == {"old", "new"}
    clock["now"] += 1
    assert load_cache(max_age_s=0) == {}


def test_stale_entries_survive_a_save_of_another_provider_until_they_are_refreshed(monkeypatch):
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(discovery, "_wall", lambda: clock["now"])
    save_cache("old", [Discovered("o")])
    clock["now"] += 90_000
    save_cache("new", [Discovered("n")])
    assert json.loads(cache_path().read_text())["providers"].keys() == {"old", "new"}


def test_a_timestamp_from_the_future_is_not_trusted(monkeypatch):
    save_cache("a", [Discovered("a1")])
    monkeypatch.setattr(discovery, "_wall", lambda: time.time() - 10_000)
    assert load_cache() == {}


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{",
        b"\xff\xfe\x00",
        b"null",
        b"[]",
        b'"x"',
        b"{}",
        b'{"version": 1}',
        b'{"version": 1, "providers": []}',
        b'{"version": 99, "providers": {}}',
        b'{"version": 1, "providers": {"a": 5}}',
    ],
)
def test_corrupt_cache_files_load_as_empty_and_never_raise(content):
    cache_path().parent.mkdir(parents=True, exist_ok=True)
    cache_path().write_bytes(content)
    cache_path().chmod(0o600)
    assert load_cache() == {}


def test_a_cache_path_that_is_a_directory_never_raises():
    cache_path().mkdir(parents=True)
    assert load_cache() == {}


def test_one_bad_provider_entry_does_not_hide_the_good_ones():
    save_cache("good", [Discovered("g")])
    raw = json.loads(cache_path().read_text())
    raw["providers"]["bad"] = {"fetched_at": "yesterday", "models": []}
    raw["providers"]["worse"] = {"fetched_at": time.time(), "models": "nope"}
    cache_path().write_text(json.dumps(raw))
    assert load_cache() == {"good": [Discovered("g")]}


def test_invalid_model_records_are_dropped_and_values_are_revalidated():
    save_cache("p", [Discovered("ok")])
    raw = json.loads(cache_path().read_text())
    raw["providers"]["p"]["models"] += [
        "junk",
        {"no": "id"},
        {
            "id": "huge",
            "context": 10**15,
            "max_output": -1,
            "vision": "yes",
            "price": {"input": "x", "output": 1},
        },
    ]
    cache_path().write_text(json.dumps(raw))
    assert load_cache() == {"p": [Discovered("ok"), Discovered("huge")]}


def test_a_group_or_world_writable_cache_is_not_believed():
    save_cache("a", [Discovered("a1")])
    cache_path().chmod(0o664)
    assert load_cache() == {}
    cache_path().chmod(0o600)
    assert load_cache() == {"a": [Discovered("a1")]}


def test_a_symlinked_cache_is_not_followed(tmp_path):
    save_cache("a", [Discovered("a1")])
    real = tmp_path / "elsewhere.json"
    real.write_bytes(cache_path().read_bytes())
    cache_path().unlink()
    cache_path().symlink_to(real)
    assert load_cache() == {}


def test_saving_over_a_corrupt_or_untrusted_cache_recovers():
    cache_path().parent.mkdir(parents=True, exist_ok=True)
    cache_path().write_text("{ not json")
    save_cache("a", [Discovered("a1")])
    assert load_cache() == {"a": [Discovered("a1")]}
    cache_path().chmod(0o666)  # untrusted contents are not carried over
    save_cache("b", [Discovered("b1")])
    assert load_cache() == {"b": [Discovered("b1")]}
    assert stat.S_IMODE(cache_path().stat().st_mode) == 0o600


# ----------------------------------------------------------------------------- merge


def _static() -> dict[str, ModelInfo]:
    return {
        "zeta": ModelInfo(
            "zeta",
            100_000,
            8_000,
            Price(1, 2, 0.1),
            vision=False,
            thinking="adaptive",
            efforts=("low", "high"),
            default_effort="low",
            sampling=False,
            can_disable_thinking=False,
            note="curated",
            replay_field="reasoning",
        ),
        "alpha": ModelInfo("alpha", 50_000, 4_000),
    }


def test_merge_keeps_curated_fields_and_takes_live_limits_and_vision():
    live = [
        Discovered(
            "zeta",
            context=1_000_000,
            max_output=128_000,
            vision=True,
            price=Price(9, 9),
            efforts=("max",),
            reasoning=False,
            tools=False,
            note="x",
        )
    ]
    merged = merge(_static(), live, default_context=1, default_output=1)
    zeta = merged["zeta"]
    assert (zeta.context, zeta.max_output, zeta.vision) == (1_000_000, 128_000, True)
    assert zeta.price == Price(1, 2, 0.1)
    assert zeta.efforts == ("low", "high") and zeta.default_effort == "low"
    assert (zeta.thinking, zeta.sampling, zeta.can_disable_thinking) == ("adaptive", False, False)
    assert (zeta.tools, zeta.note, zeta.replay_field) == (True, "curated", "reasoning")


def test_merge_ignores_zero_and_missing_live_values():
    live = [
        Discovered("zeta", context=0, max_output=None, vision=None),
        Discovered("alpha", context=-5, max_output=0),
    ]
    merged = merge(_static(), live, default_context=1, default_output=1)
    assert merged["zeta"] == _static()["zeta"] and merged["alpha"] == _static()["alpha"]


def test_merge_a_live_false_vision_flag_is_a_real_answer():
    static = {"m": ModelInfo("m", 10, 5, vision=True)}
    assert (
        merge(static, [Discovered("m", vision=False)], default_context=1, default_output=1)[
            "m"
        ].vision
        is False
    )


def test_merge_fills_only_blank_price_efforts_and_note_of_a_static_entry():
    live = [
        Discovered("alpha", price=Price(3, 4), efforts=("low", "medium"), note="retires 2099-01-01")
    ]
    alpha = merge(_static(), live, default_context=1, default_output=1)["alpha"]
    assert (alpha.price, alpha.efforts, alpha.note) == (
        Price(3, 4),
        ("low", "medium"),
        "retires 2099-01-01",
    )


def test_merge_appends_new_ids_alphabetically_after_the_curated_order():
    live = [
        Discovered("m-b"),
        Discovered("Z-new"),
        Discovered("alpha"),
        Discovered("a-new"),
        Discovered("zeta"),
    ]
    merged = merge(_static(), live, default_context=1, default_output=1)
    assert list(merged) == ["zeta", "alpha", "a-new", "m-b", "Z-new"]


def test_merge_gives_new_ids_conservative_defaults_and_discovered_facts():
    live = [
        Discovered("plain"),
        Discovered(
            "rich",
            display_name="Rich",
            context=200_000,
            max_output=50_000,
            vision=True,
            price=Price(1, 2),
            reasoning=True,
            efforts=("low", "high"),
            note="n",
        ),
        Discovered("no-tools", tools=False),
        Discovered("tiny", context=4_096),
    ]
    merged = merge({}, live, default_context=128_000, default_output=16_000)
    plain, rich = merged["plain"], merged["rich"]
    assert (plain.context, plain.max_output, plain.vision, plain.tools, plain.price) == (
        128_000,
        16_000,
        False,
        True,
        None,
    )
    assert (plain.thinking, plain.efforts, plain.sampling) == ("none", (), True)
    assert (rich.context, rich.max_output, rich.vision, rich.price, rich.note) == (
        200_000,
        50_000,
        True,
        Price(1, 2),
        "n",
    )
    assert (
        rich.thinking == "none" and rich.efforts == ()
    )  # wire style is unknown: no behaviour change
    assert rich.extra == {
        "discovered": True,
        "display_name": "Rich",
        "reasoning": True,
        "efforts": ["low", "high"],
    }
    assert merged["no-tools"].tools is False
    assert merged["tiny"].max_output == 4_096  # a default output is never larger than the window


def test_merge_drops_retired_ids_whether_curated_or_new():
    live = [
        Discovered("alpha", retired=True),
        Discovered("ghost", retired=True),
        Discovered("fresh"),
    ]
    merged = merge(_static(), live, default_context=1, default_output=1)
    assert list(merged) == ["zeta", "fresh"]


def test_merge_keeps_a_retired_id_that_is_the_only_entry():
    static = {"only": ModelInfo("only", 10, 5)}
    assert list(
        merge(static, [Discovered("only", retired=True)], default_context=1, default_output=1)
    ) == ["only"]
    assert list(
        merge({}, [Discovered("solo", retired=True)], default_context=1, default_output=1)
    ) == ["solo"]
    both = merge(
        _static(),
        [Discovered("zeta", retired=True), Discovered("alpha", retired=True)],
        default_context=1,
        default_output=1,
    )
    assert list(both) == ["zeta", "alpha"]  # never return an empty catalog because of retirements


def test_merge_leaves_static_ids_unknown_to_discovery_alone_and_mutates_nothing():
    static = _static()
    before = dict(static)
    merged = merge(static, [Discovered("other")], default_context=1, default_output=1)
    assert merged["zeta"] is static["zeta"] and merged["alpha"] is static["alpha"]
    assert static == before and merged is not static


def test_merge_uses_the_first_record_for_a_duplicated_id():
    live = [Discovered("alpha", context=111), Discovered("alpha", context=222)]
    assert merge(_static(), live, default_context=1, default_output=1)["alpha"].context == 111


# ----------------------------------------------------------------------------- refresh


def test_provider_kinds_map_to_discovery_kinds():
    expected = {
        "anthropic": "anthropic",
        "openai": "openai",
        "deepseek": "deepseek",
        "google": "gemini",
        "sarvam": "sarvam",
        "openrouter": "openrouter",
        "ollama": "ollama",
        "groq": "openai",
        "together": "openai",
        "fireworks": "openai",
        "cerebras": "openai",
    }
    assert {pid: kind_for(PROVIDERS[pid]) for pid in expected} == expected
    # kinds that the spec may gain later are handled by string comparison
    assert kind_for(ProviderSpec("x", "X", "gemini", "https://x")) == "gemini"  # type: ignore[arg-type]
    assert kind_for(ProviderSpec("x", "X", "responses", "https://x")) == "openai"  # type: ignore[arg-type]
    assert kind_for(ProviderSpec("x", "X", "bedrock", "https://x")) is None  # type: ignore[arg-type]


def _router(routes: dict[str, Callable[[httpx.Request], httpx.Response]]):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return routes[request.url.host](request)

    return handler, seen


async def test_refresh_lists_every_keyed_provider_and_saves_the_results(http, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
    monkeypatch.setenv("OPENAI_API_KEY", "oai-key")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    handler, seen = _router(
        {
            "api.anthropic.com": lambda r: ok(
                {"data": [_claude("claude-opus-5-5", max_input_tokens=1_000_000)]}
            ),
            "api.openai.com": lambda r: ok({"data": [{"id": "gpt-6-sol"}]}),
            "generativelanguage.googleapis.com": lambda r: ok(
                {"models": [_gem("models/gemini-4-argon")]}
            ),
            "localhost": lambda r: ok({"models": []}),
        }
    )
    results, errors = await refresh(PROVIDERS, client=http(handler))
    assert errors == {}
    assert set(results) == {"anthropic", "openai", "google", "ollama"}
    assert results["anthropic"][0].context == 1_000_000
    assert results["google"][0].id == "gemini-4-argon"
    hosts = [r.url.host for r in seen]
    assert "api.groq.com" not in hosts and "openrouter.ai" not in hosts  # no key, so skipped
    by_host = {r.url.host: r for r in seen}
    assert by_host["api.anthropic.com"].headers["x-api-key"] == "ant-key"
    assert by_host["api.openai.com"].headers["authorization"] == "Bearer oai-key"
    assert by_host["generativelanguage.googleapis.com"].headers["x-goog-api-key"] == "gem-key"
    assert by_host["generativelanguage.googleapis.com"].url.path == "/v1beta/models"
    cached = load_cache()
    assert set(cached) == {"anthropic", "openai", "google"}  # the empty ollama list is not cached
    assert cached["openai"] == [Discovered("gpt-6-sol")]


async def test_one_failing_provider_never_fails_the_others(http, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
    monkeypatch.setenv("OPENAI_API_KEY", "oai-key")
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    handler, _ = _router(
        {
            "api.anthropic.com": lambda r: json_error(401, "invalid x-api-key"),
            "api.openai.com": lambda r: ok({"data": [{"id": "gpt-6-sol"}]}),
            "api.groq.com": lambda r: httpx.Response(200, content=b"<html>"),
            "api.deepseek.com": lambda r: json_error(500, "boom"),
            "localhost": lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")),
        }
    )
    results, errors = await refresh(PROVIDERS, client=http(handler))
    assert set(results) == {"openai"}
    assert errors["anthropic"].startswith("AUTH (HTTP 401): invalid x-api-key")
    assert errors["groq"].startswith("MALFORMED_RESPONSE")
    assert errors["deepseek"] == "SERVER (HTTP 500): boom"
    assert errors["ollama"].startswith("TRANSPORT")
    assert set(load_cache()) == {"openai"}  # failures are not cached


async def test_refresh_keeps_previously_cached_providers_when_one_fails(http, monkeypatch):
    save_cache("anthropic", [Discovered("claude-opus-5-5", context=1_000_000)])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
    handler, _ = _router({"api.anthropic.com": lambda r: json_error(503, "down")})
    results, errors = await refresh({"anthropic": PROVIDERS["anthropic"]}, client=http(handler))
    assert results == {} and errors["anthropic"].startswith("SERVER")
    assert load_cache()["anthropic"][0].context == 1_000_000


async def test_refresh_without_a_key_is_silent_unless_the_provider_was_asked_for(http):
    handler, seen = _router({})
    specs = {"anthropic": PROVIDERS["anthropic"], "openai": PROVIDERS["openai"]}
    assert await refresh(specs, client=http(handler)) == ({}, {})
    assert seen == []
    results, errors = await refresh(specs, only="openai", client=http(handler))
    assert results == {} and "no API key" in errors["openai"] and "anthropic" not in errors
    assert (await refresh(specs, only="nope", client=http(handler)))[1] == {
        "nope": "unknown provider 'nope'"
    }


async def test_refresh_reports_an_unsupported_provider_kind_only_when_asked(http):
    odd = ProviderSpec("odd", "Odd", "bedrock", "https://odd.test", ("ODD_KEY",))  # type: ignore[arg-type]
    handler, seen = _router({})
    assert await refresh({"odd": odd}, client=http(handler)) == ({}, {})
    _, errors = await refresh({"odd": odd}, only="odd", client=http(handler))
    assert "no model-list endpoint" in errors["odd"] and seen == []


async def test_refresh_only_lists_the_requested_provider(http, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("OPENAI_API_KEY", "b")
    handler, seen = _router({"api.openai.com": lambda r: ok({"data": [{"id": "gpt-6-sol"}]})})
    results, errors = await refresh(
        {"anthropic": PROVIDERS["anthropic"], "openai": PROVIDERS["openai"]},
        only="openai",
        client=http(handler),
    )
    assert set(results) == {"openai"} and errors == {}
    assert {r.url.host for r in seen} == {"api.openai.com"}


async def test_refresh_uses_a_stored_credential_and_a_base_url_override(http):
    from kalash.credentials import save_credential

    save_credential("openai", "stored-key")
    save_credential("openai:base_url", "https://proxy.test/v1")
    handler, seen = _router({"proxy.test": lambda r: ok({"data": [{"id": "x"}]})})
    results, _ = await refresh({"openai": PROVIDERS["openai"]}, client=http(handler))
    assert results["openai"] == [Discovered("x")]
    assert (
        seen[0].headers["authorization"] == "Bearer stored-key" and seen[0].url.path == "/v1/models"
    )


async def test_refresh_ollama_needs_no_key_and_honours_ollama_host(http, monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "gpu-box:11434")
    seen: list[httpx.Request] = []
    handler = _ollama_server({"m": {"capabilities": ["completion"]}}, [_tag("m")], seen)
    results, errors = await refresh({"ollama": PROVIDERS["ollama"]}, client=http(handler))
    assert errors == {} and [m.id for m in results["ollama"]] == ["m"]
    assert seen[0].url.host == "gpu-box" and "authorization" not in seen[0].headers


async def test_refresh_reaches_all_providers_concurrently(http, monkeypatch):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY", "TOGETHER_API_KEY"):
        monkeypatch.setenv(env, "k")
    active = peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return ok({"data": [{"id": "m"}]})

    specs = {pid: PROVIDERS[pid] for pid in ("anthropic", "openai", "groq", "together")}
    results, errors = await refresh(specs, client=http(handler))
    assert len(results) == 4 and errors == {}
    assert peak == 4


async def test_a_bug_in_one_parser_is_reported_not_raised(http, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-secret-key")
    monkeypatch.setenv("OPENAI_API_KEY", "oai-key")

    async def broken(*args: Any, **kwargs: Any) -> list[Discovered]:
        raise KeyError("ant-secret-key exploded")

    monkeypatch.setitem(discovery._LISTERS, "anthropic", broken)
    handler, _ = _router({"api.openai.com": lambda r: ok({"data": [{"id": "gpt-6-sol"}]})})
    specs = {"anthropic": PROVIDERS["anthropic"], "openai": PROVIDERS["openai"]}
    results, errors = await refresh(specs, client=http(handler))
    assert set(results) == {"openai"}
    assert (
        errors["anthropic"].startswith("unexpected KeyError")
        and "ant-secret-key" not in errors["anthropic"]
    )


async def test_a_cache_write_failure_does_not_lose_the_results(http, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "oai-key")

    def explode(batch: Any) -> None:
        raise PermissionError("read-only home")

    monkeypatch.setattr(discovery, "_save_many", explode)
    handler, _ = _router({"api.openai.com": lambda r: ok({"data": [{"id": "gpt-6-sol"}]})})
    results, errors = await refresh({"openai": PROVIDERS["openai"]}, client=http(handler))
    assert results == {"openai": [Discovered("gpt-6-sol")]} and errors == {}


async def test_the_api_key_never_appears_in_refresh_errors(http, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    handler, _ = _router(
        {
            "api.anthropic.com": lambda r: json_error(401, f"bad key {SECRET}"),
            "api.openai.com": lambda r: json_error(403, f"Incorrect API key provided: {SECRET}"),
        }
    )
    _, errors = await refresh(
        {"anthropic": PROVIDERS["anthropic"], "openai": PROVIDERS["openai"]}, client=http(handler)
    )
    assert set(errors) == {"anthropic", "openai"}
    assert all(SECRET not in message for message in errors.values())


async def test_refresh_creates_and_closes_its_own_client_when_none_is_given(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    created: list[httpx.AsyncClient] = []

    def fake_make_client(idle_timeout: float = 300.0, transport: Any = None) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: ok({"data": [{"id": "m"}]}))
        )
        created.append(client)
        return client

    monkeypatch.setattr(discovery, "make_client", fake_make_client)
    results, _ = await refresh({"openai": PROVIDERS["openai"]})
    assert results["openai"] == [Discovered("m")]
    assert len(created) == 1 and created[0].is_closed


def test_the_cache_file_is_never_world_readable_after_a_refresh_save():
    save_cache("p", [Discovered("m")])
    mode = os.stat(cache_path()).st_mode
    assert mode & 0o077 == 0
