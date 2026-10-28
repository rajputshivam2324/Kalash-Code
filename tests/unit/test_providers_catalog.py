"""Provider registry, model catalog invariants, and how the catalog steers each adapter."""

from __future__ import annotations

import json

import pytest

from kalash.llm import catalog
from kalash.llm.anthropic import AnthropicAdapter
from kalash.llm.catalog import CATALOG, RETIRED, lookup, resolve_effort
from kalash.llm.gemini import GeminiAdapter
from kalash.llm.openai_chat import OpenAIChatAdapter
from kalash.llm.openai_responses import OpenAIResponsesAdapter
from kalash.llm.providers import (
    PROVIDERS,
    ProviderError,
    all_specs,
    build_adapter,
    catalog_for,
    known_models,
    model_problem,
    parse_model,
)
from kalash.llm.types import Message, TextBlock, ToolCallBlock
from tests.harness.wire import (
    Recorder,
    collect,
    openai_adapter,
    respond,
    simple_request,
    sse_bytes,
)

# ----------------------------------------------------------------------------- invariants


def test_every_catalog_provider_is_a_real_provider_and_every_model_is_sane():
    assert set(CATALOG) <= set(PROVIDERS)
    for provider, models in CATALOG.items():
        assert models, provider
        for model_id, info in models.items():
            where = f"{provider}/{model_id}"
            assert info.id == model_id, where
            assert 0 < info.max_output <= info.context, where
            assert info.context >= 32_000, where
            if info.efforts:
                assert info.thinking != "none", where
            if info.default_effort and info.efforts:
                assert info.default_effort in info.efforts, where
            if info.price:
                assert info.price.input > 0 and info.price.output > 0, where
            for tier in info.tiers:
                assert tier.above > 0 and tier.price.input >= info.price.input, where
            assert not catalog.retired(provider, model_id), f"{where} is catalogued and retired"


def test_retired_table_only_names_known_providers():
    assert set(RETIRED) <= set(PROVIDERS)


def test_curated_ids_follow_the_providers_current_documentation():
    # Spot checks that guard against silently reintroducing retired or wrong ids.
    assert "claude-opus-5-5" in CATALOG["anthropic"]
    assert "claude-sonnet-4-5" not in CATALOG["anthropic"]
    assert "deepseek-flash" in CATALOG["deepseek"] and "deepseek-chat" not in CATALOG["deepseek"]
    assert "deepseekv4.1-flash" in CATALOG["sarvam"] and "deepseekv4-flash" not in CATALOG["sarvam"]
    assert "gpt-5.6-sol" in CATALOG["openai"] and "gpt-4o" not in CATALOG["openai"]


@pytest.mark.parametrize(
    ("provider", "model", "level"),
    [
        ("anthropic", "claude-opus-4-1", "error"),
        ("deepseek", "deepseek-chat", "error"),
        ("openai", "o3-mini", "error"),
        ("sarvam", "deepseekv4-flash", "error"),
        ("anthropic", "claude-sonnet-4-5", "warning"),
        ("google", "gemini-3.7-flash", "warning"),
        ("anthropic", "claude-opus-5-5", None),
    ],
)
def test_retired_models_are_refused_and_retiring_ones_warn(provider, model, level):
    problem = model_problem(provider, model)
    assert (problem[0] if problem else None) == level


def test_a_retired_model_cannot_be_built_even_with_a_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    specs = all_specs()
    with pytest.raises(ProviderError, match="discontinued"):
        build_adapter(parse_model("deepseek/deepseek-chat", specs), specs)


# ----------------------------------------------------------------------------- effort mapping


def test_efforts_map_to_the_nearest_supported_value_and_never_invent_controls():
    argon = lookup("google", "gemini-4-argon")
    assert [resolve_effort(argon, e) for e in ("minimal", "low", "max", "off")] == [
        "low",
        "low",
        "high",
        None,
    ]
    opus = lookup("anthropic", "claude-opus-5-5")
    assert resolve_effort(opus, "minimal") == "low" and resolve_effort(opus, "xhigh") == "xhigh"
    sol = lookup("openai", "gpt-6-sol")
    assert resolve_effort(sol, "off") == "none"
    astra = lookup("openai", "gpt-6-astra")
    assert resolve_effort(astra, "off") is None, "Astra cannot disable reasoning"
    assert resolve_effort(lookup("sarvam", "gemma4"), "high") is None, "no reasoning control"
    assert resolve_effort(None, "High") == "high" and resolve_effort(argon, None) is None


# ----------------------------------------------------------------------------- adapter wiring


@pytest.mark.parametrize(
    ("spec_id", "model", "cls", "needle"),
    [
        ("anthropic", "claude-opus-5-5", AnthropicAdapter, "api.anthropic.com"),
        ("openai", "gpt-6.1-sol", OpenAIResponsesAdapter, "api.openai.com/v1"),
        ("xai", "grok-4.7", OpenAIResponsesAdapter, "api.x.ai/v1"),
        ("google", "gemini-4-argon", GeminiAdapter, "generativelanguage.googleapis.com/v1beta"),
        ("deepseek", "deepseek-flash", AnthropicAdapter, "api.deepseek.com/anthropic"),
        ("minimax", "MiniMax-M3", AnthropicAdapter, "api.minimax.io/anthropic"),
        ("moonshot", "kimi-k3", OpenAIChatAdapter, "api.moonshot.ai/v1"),
        ("zai", "glm-5.3", OpenAIChatAdapter, "api.z.ai/api/paas/v4"),
        ("openrouter", "anthropic/claude-opus-5", OpenAIChatAdapter, "openrouter.ai/api/v1"),
        ("groq", "openai/gpt-oss-120b", OpenAIChatAdapter, "api.groq.com/openai/v1"),
        ("together", "zai-org/GLM-5.3", OpenAIChatAdapter, "api.together.ai/v1"),
        ("fireworks", "accounts/fireworks/models/kimi-k3", OpenAIChatAdapter, "fireworks.ai"),
        ("cerebras", "gpt-oss-120b", OpenAIChatAdapter, "api.cerebras.ai/v1"),
    ],
)
def test_each_builtin_provider_builds_the_right_adapter(monkeypatch, spec_id, model, cls, needle):
    for name in PROVIDERS[spec_id].env_keys:
        monkeypatch.setenv(name, "test-key")
    specs = all_specs()
    adapter = build_adapter(parse_model(f"{spec_id}/{model}", specs), specs)
    assert isinstance(adapter, cls) and needle in adapter.base_url
    assert adapter.context_window == lookup(spec_id, model).context


def test_ollama_needs_no_key_and_honours_ollama_host(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "10.0.0.5:11434")
    specs = all_specs()
    adapter = build_adapter(parse_model("ollama/qwen3.8:27b", specs), specs)
    assert adapter.base_url == "http://10.0.0.5:11434/v1" and adapter.api_key is None


def test_sarvam_routes_auth_and_usage_differ_between_v1_and_v2(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "sv-key")
    specs = all_specs()
    v1 = build_adapter(parse_model("sarvam/sarvam-105b", specs), specs)
    v2 = build_adapter(parse_model("sarvam/glm5.3", specs), specs)
    assert v1.base_url.endswith("/v1") and v2.base_url.endswith("/v2")
    for adapter in (v1, v2):
        assert adapter.dialect.auth == "none"
        assert adapter.dialect.extra_headers == {"api-subscription-key": "sv-key"}
        assert "authorization" not in {k.lower() for k in adapter._headers()}
    assert v1.info.stream_usage is False and v2.info.stream_usage is True
    assert (v1.context_window, v2.context_window) == (128_000, 1_048_576)


def test_live_listed_glm_flash_uses_the_sarvam_v2_endpoint(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "sv-key")
    specs = all_specs()
    adapter = build_adapter(parse_model("sarvam/glm5.3-flash", specs), specs)
    assert adapter.base_url == "https://api.sarvam.ai/v2"
    assert adapter._headers()["api-subscription-key"] == "sv-key"


def test_sarvam_glm_flash_is_selectable_without_a_discovery_cache(monkeypatch):
    monkeypatch.setattr("kalash.llm.discovery.load_cache", dict)
    spec = PROVIDERS["sarvam"]
    assert {"glm5.3", "glm5.3-flash"} <= set(spec.models)
    assert {"glm5.3", "glm5.3-flash"} <= set(known_models(spec))
    info = catalog_for(spec)["glm5.3-flash"]
    assert info.price is None and info.extra["unverified_metadata"] is True
    assert info.context == 1048576 and info.vision and info.tools
    assert resolve_effort(info, "low") == "low"


def test_cached_flash_metadata_overlays_its_builtin_listing(monkeypatch):
    from kalash.llm.discovery import Discovered

    monkeypatch.setattr(
        "kalash.llm.discovery.load_cache",
        lambda: {"sarvam": [Discovered("glm5.3-flash", context=1048576, vision=True, tools=True)]},
    )
    info = catalog_for(PROVIDERS["sarvam"])["glm5.3-flash"]
    assert info.context == 1048576 and info.vision and info.tools
    assert info.price is None and resolve_effort(info, "low") == "low"


@pytest.mark.parametrize("model", ["sarvam-105b", "sarvam-105b-conversations"])
def test_sarvam_v1_off_is_explicit_null_instead_of_omitting_the_control(monkeypatch, model):
    from kalash.agent.effort import normalize

    monkeypatch.setenv("SARVAM_API_KEY", "sv-key")
    specs = all_specs()
    adapter = build_adapter(parse_model(f"sarvam/{model}", specs), specs)
    assert normalize(adapter.info, "off") == ("off", None)
    body = adapter.build_body(simple_request(reasoning_effort="off"))
    assert "reasoning_effort" in body and body["reasoning_effort"] is None


def test_unknown_models_get_provider_defaults_and_custom_providers_work(monkeypatch):
    monkeypatch.setenv("MY_KEY", "k")
    specs = all_specs(
        {
            "acme": {
                "kind": "responses",
                "base_url": "https://acme.test/v1",
                "api_key_env": "MY_KEY",
                "context_window": 64_000,
                "models": ["acme-1"],
            }
        }
    )
    adapter = build_adapter(parse_model("acme-1", specs), specs)
    assert isinstance(adapter, OpenAIResponsesAdapter) and adapter.context_window == 64_000
    with pytest.raises(ProviderError, match="kind must be one of"):
        all_specs({"bad": {"kind": "soap", "base_url": "http://x"}})


def test_builtin_provider_endpoint_override_survives(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    specs = all_specs({"openai": {"base_url": "https://proxy.test/v1"}})
    adapter = build_adapter(parse_model("openai/gpt-6-sol", specs), specs)
    assert adapter.base_url == "https://proxy.test/v1"


# ----------------------------------------------------------------------------- chat adapter


def chat_ok() -> bytes:
    return sse_bytes(
        [{"choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": "stop"}]}]
    )


async def test_openrouter_uses_the_unified_reasoning_object():
    from kalash.llm.openai_chat import OpenAIDialect

    rec = Recorder(respond(chat_ok()))
    info = lookup("openrouter", "anthropic/claude-opus-5")
    adapter = openai_adapter(
        rec, provider="openrouter", info=info, dialect=OpenAIDialect(reasoning_style="object")
    )
    await collect(adapter, simple_request(reasoning_effort="high"))
    body = rec.body()
    assert body["reasoning"] == {"effort": "high"} and "reasoning_effort" not in body


async def test_reasoning_control_is_dropped_for_models_without_one():
    rec = Recorder(respond(chat_ok()))
    adapter = openai_adapter(
        rec, provider="sarvam", model="gemma4", info=lookup("sarvam", "gemma4")
    )
    await collect(adapter, simple_request(reasoning_effort="high", temperature=0.5))
    body = rec.body()
    assert "reasoning_effort" not in body and body["temperature"] == 0.5


async def test_sarvam_deepseek_reasoning_is_disabled_through_template_kwargs():
    rec = Recorder(respond(chat_ok()), respond(chat_ok()))
    info = lookup("sarvam", "deepseekv4.1-flash")
    adapter = openai_adapter(rec, provider="sarvam", model="deepseekv4.1-flash", info=info)
    await collect(adapter, simple_request(reasoning_effort="off"))
    await collect(adapter, simple_request(reasoning_effort="max"))
    assert rec.body(0)["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert "extra_body" not in rec.body(1) and rec.body(1)["reasoning_effort"] == "max"


async def test_sampling_is_not_sent_to_models_that_reject_it():
    rec = Recorder(respond(chat_ok()))
    info = lookup("moonshot", "kimi-k3")
    adapter = openai_adapter(rec, provider="moonshot", model="kimi-k3", info=info)
    await collect(adapter, simple_request(temperature=0.7))
    assert "temperature" not in rec.body()


async def test_stream_usage_flag_follows_the_model():
    rec = Recorder(respond(chat_ok()), respond(chat_ok()))
    v1 = openai_adapter(
        rec, provider="sarvam", model="sarvam-105b", info=lookup("sarvam", "sarvam-105b")
    )
    v2 = openai_adapter(rec, provider="sarvam", model="glm5.3", info=lookup("sarvam", "glm5.3"))
    await collect(v1, simple_request())
    await collect(v2, simple_request())
    assert "stream_options" not in rec.body(0)
    assert rec.body(1)["stream_options"] == {"include_usage": True}


async def test_replay_field_comes_from_the_catalog_per_model():
    info = lookup("groq", "openai/gpt-oss-120b")
    adapter = openai_adapter(Recorder(), provider="groq", model="openai/gpt-oss-120b", info=info)
    from kalash.llm.types import ReasoningBlock

    message = Message(
        "assistant", [ReasoningBlock("think"), TextBlock("a")], origin="groq/openai/gpt-oss-120b"
    )
    wire = adapter.serialize(simple_request().__class__("m", "", [message]))
    assert wire[0]["reasoning"] == "think" and "reasoning_content" not in wire[0]


async def test_parallel_calls_without_index_or_id_stay_separate_and_fragments_join():
    rec = Recorder(
        respond(
            sse_bytes(
                [
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {"function": {"name": "read", "arguments": '{"a":'}}
                                    ]
                                },
                            }
                        ]
                    },
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"tool_calls": [{"function": {"arguments": "1}"}}]},
                            }
                        ]
                    },
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {"function": {"name": "glob", "arguments": '{"b":2}'}}
                                    ]
                                },
                            }
                        ]
                    },
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
                ]
            )
        )
    )
    events = await collect(openai_adapter(rec), simple_request())
    calls = events[-1].message.tool_calls
    assert [(c.name, c.arguments) for c in calls] == [("read", '{"a":1}'), ("glob", '{"b":2}')]
    assert len({c.id for c in calls}) == 2


def test_only_extra_content_is_copied_to_the_wire_tool_call():
    call = ToolCallBlock(
        "c1", "read", "{}", extra={"item_id": "fc_9", "extra_content": {"google": {"s": 1}}}
    )
    message = Message("assistant", [call], origin="openai/m")
    wire = openai_adapter(Recorder()).serialize(simple_request().__class__("m", "", [message]))
    tool_call = wire[0]["tool_calls"][0]
    assert "item_id" not in tool_call and tool_call["extra_content"] == {"google": {"s": 1}}
    assert json.dumps(tool_call)


# ----------------------------------------------------------------------------- errors


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"message": "x", "code": "organization_spend_limit_exceeded"}},
        {"error": {"message": "x", "details": {"error_code": "enforced_spend_limit_reached"}}},
    ],
)
async def test_spend_limit_429s_are_not_retried(body):
    import httpx

    from kalash.llm.errors import ErrorCode, LLMError

    rec = Recorder(httpx.Response(429, json=body))
    with pytest.raises(LLMError) as info:
        await collect(openai_adapter(rec), simple_request())
    assert info.value.code == ErrorCode.QUOTA and not info.value.retryable


async def test_groq_failed_generation_is_retryable_as_an_invalid_tool_call():
    import httpx

    from kalash.llm.errors import ErrorCode, LLMError

    body = {
        "error": {
            "message": "Failed to call a function.",
            "type": "invalid_request_error",
            "code": "tool_use_failed",
            "failed_generation": "<function=read>",
        }
    }
    rec = Recorder(httpx.Response(400, json=body))
    with pytest.raises(LLMError) as info:
        await collect(openai_adapter(rec, provider="groq"), simple_request())
    assert info.value.code == ErrorCode.INVALID_TOOL_CALL and info.value.retryable
