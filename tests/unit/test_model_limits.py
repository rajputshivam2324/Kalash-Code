"""Tests built from real provider failures.

Every error string below was copied from an actual failed run against Groq. They
existed because the loop sent a hardcoded ``max_tokens=8192`` and the full
15-tool schema block to every model without asking what the model accepted, and
because the test suite used a fake provider that accepted anything.

The fixed prefix measured 4,993 tokens (1,105 prompt + 3,888 schemas). Adding an
8,192-token reservation produced an 11k-13k request against an 8,000
tokens-per-minute ceiling, so *every* prompt failed — including "hi".
"""

import json

import pytest

from kalash.core.budget import estimate_tokens
from kalash.models.diagnose import diagnose, explain
from kalash.models.limits import (
    DEFAULT_MAX_OUTPUT,
    MIN_USABLE_OUTPUT,
    TARGET_OUTPUT_TOKENS,
    ConstraintCache,
    describe,
    effective_limits,
    fit_request_size,
    input_budget,
    lookup,
    resolve_output_tokens,
    supports_tools,
    tpm_allowance,
)
from kalash.runtime.prompt import build_system_prompt
from kalash.tools.builtins import default_registry
from kalash.tools.schema import (
    MINIMAL_TOOLS,
    minify_schema,
    select_profile,
    tool_schema,
)

# Verbatim from the failing runs.
ERR_MAX_OUTPUT = (
    "Error code: 400 - {'error': {'message': \"'max_completion_tokens' must be "
    "less than or equal to 4096, the maximum value for 'max_completion_tokens' "
    "is less than the 'context_window' for this model\", 'type': "
    "'invalid_request_error', 'param': 'max_completion_tokens'}}"
)
ERR_TPM = (
    "Error code: 413 - {'error': {'message': 'Request too large for model "
    "`openai/gpt-oss-20b` in organization `org_01k` service tier `on_demand` on "
    "tokens per minute (TPM): Limit 8000, Requested 10692, please reduce your "
    "message size and try again.', 'type': 'tokens', 'code': "
    "'rate_limit_exceeded'}}"
)
ERR_NO_TOOLS = (
    "Error code: 400 - {'error': {'message': \"'tool calling' is not supported "
    "with this model\", 'type': 'invalid_request_error', 'param': 'tool calling'}}"
)


class TestDiagnoseRealErrors:
    def test_max_output_error_yields_the_exact_cap(self):
        result = diagnose(ERR_MAX_OUTPUT, "openai/gpt-oss-20b")
        assert result.kind == "max_output"
        assert result.adjust_max_output == 4096
        assert result.retryable
        assert "4,096" in result.summary

    def test_tpm_error_reports_the_overage(self):
        result = diagnose(ERR_TPM, "openai/gpt-oss-20b")
        assert result.kind == "tpm"
        assert "8,000" in result.summary
        assert "10,692" in result.summary
        assert "2,692 over" in result.summary
        assert result.reduce_input

    def test_no_tools_error_tells_the_user_to_switch(self):
        result = diagnose(ERR_NO_TOOLS, "groq/compound")
        assert result.kind == "no_tools"
        assert "does not support tool calling" in result.summary
        assert "/models" in result.remedy

    @pytest.mark.parametrize("raw", [ERR_MAX_OUTPUT, ERR_TPM, ERR_NO_TOOLS])
    def test_explanation_hides_the_json_wrapper(self, raw):
        text = explain(raw, "m")
        assert "{'error'" not in text
        assert "org_01k" not in text
        assert len(text) < 260

    def test_auth_and_context_errors(self):
        assert diagnose("401 unauthorized: invalid api key").kind == "auth"
        assert diagnose("maximum context length exceeded").kind == "context"

    def test_unknown_error_keeps_the_provider_sentence(self):
        raw = "Error code: 500 - {'error': {'message': 'upstream exploded'}}"
        assert diagnose(raw).summary == "upstream exploded"

    def test_empty_error_does_not_crash(self):
        assert diagnose("").summary

    def test_invalid_tool_call_is_recoverable(self):
        raw = (
            "Tool call validation failed: attempted to call tool 'print_tree' "
            "which was not in request.tools"
        )
        result = diagnose(raw, "openai/gpt-oss-safeguard-20b")
        assert result.kind == "invalid_tool"
        assert result.retryable
        assert "print_tree" in result.summary
        assert "print_tree" in result.remedy


class TestModelLimits:
    def test_groq_safeguard_model_is_throughput_bound(self):
        limits = lookup("openai/gpt-oss-safeguard-20b")
        assert limits.max_output_tokens == 4096
        assert limits.tokens_per_minute == 8000
        assert limits.source == "groq"

    def test_provider_prefix_is_stripped_for_lookup(self):
        assert lookup("groq/gpt-oss-20b").tokens_per_minute == 8000
        assert lookup("openai/gpt-oss-safeguard-20b").tokens_per_minute == 8000

    def test_fit_request_size_flags_input_shrink(self):
        output, needs = fit_request_size("openai/gpt-oss-20b", 7500, 4096)
        assert output == MIN_USABLE_OUTPUT
        assert needs is True

    def test_fit_request_size_fits_when_under_allowance(self):
        output, needs = fit_request_size("openai/gpt-oss-20b", 2000, 4096)
        assert needs is False
        assert output <= 4096

    def test_unknown_model_inherits_groq_provider_default(self):
        limits = effective_limits("brand-new-model-v9", provider_id="groq")
        assert limits.tokens_per_minute == 8000
        assert limits.source == "groq-default"

    def test_unknown_model_on_anthropic_has_no_tpm(self):
        limits = effective_limits("claude-future-9", provider_id="anthropic")
        assert limits.tokens_per_minute is None

    def test_observed_tpm_overrides_table(self):
        ConstraintCache.reset()
        ConstraintCache.record_tpm("openai/gpt-oss-20b", 12_000)
        limits = effective_limits("openai/gpt-oss-20b")
        assert limits.tokens_per_minute == 12_000
        assert limits.source == "observed-tpm"
        ConstraintCache.reset()

    def test_tpm_diagnosis_carries_observed_limit(self):
        result = diagnose(ERR_TPM, "openai/gpt-oss-20b")
        assert result.tpm_limit == 8000
        assert result.requested_total == 10_692

    def test_longest_pattern_wins(self):
        # gpt-oss-120b must not fall through to a shorter generic entry.
        assert lookup("openai/gpt-oss-120b").source == "groq"
        assert lookup("claude-3-5-sonnet-20241022").max_output_tokens == 8192

    def test_unknown_model_gets_conservative_defaults(self):
        limits = lookup("some-model-nobody-has-heard-of")
        assert limits.max_output_tokens == DEFAULT_MAX_OUTPUT
        assert limits.supports_tools is True

    def test_compound_models_reject_tools(self):
        assert supports_tools("groq/compound") is False
        assert supports_tools("anthropic/claude-sonnet-4-5") is True

    def test_output_is_clamped_to_the_model_cap(self):
        # The bug: 8192 requested against a 4096 cap.
        assert resolve_output_tokens("openai/gpt-oss-20b", 8192, 0) <= 4096

    def test_output_shrinks_as_the_prompt_grows_under_tpm(self):
        roomy = resolve_output_tokens("openai/gpt-oss-20b", 8192, 500)
        tight = resolve_output_tokens("openai/gpt-oss-20b", 8192, 5000)
        assert tight < roomy
        assert tight >= MIN_USABLE_OUTPUT

    def test_output_never_drops_below_usable(self):
        assert (
            resolve_output_tokens("openai/gpt-oss-20b", 8192, 100_000)
            == MIN_USABLE_OUTPUT
        )

    def test_unbounded_provider_is_not_throttled(self):
        assert resolve_output_tokens("anthropic/claude-sonnet-4-5", 8192, 5000) == 8192

    def test_input_budget_reflects_throughput_not_window(self):
        # 131k context but 8k/minute: the window is not the binding constraint.
        assert lookup("openai/gpt-oss-20b").context_window == 131_072
        assert input_budget("openai/gpt-oss-20b") < 10_000
        assert input_budget("anthropic/claude-sonnet-4-5") > 100_000

    def test_describe_is_human_readable(self):
        text = describe("openai/gpt-oss-20b")
        assert "TPM" in text
        assert "8,000" in text


class TestSchemaMinification:
    def test_titles_and_defaults_are_dropped(self):
        raw = {
            "properties": {
                "path": {"title": "Path", "type": "string", "default": ""},
            },
            "additionalProperties": False,
        }
        out = minify_schema(raw)
        assert "title" not in out["properties"]["path"]
        assert "default" not in out["properties"]["path"]
        assert "additionalProperties" not in out

    def test_semantics_survive(self):
        raw = {
            "properties": {
                "mode": {"title": "Mode", "type": "string", "enum": ["a", "b"]},
                "items": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["mode"],
            "type": "object",
        }
        out = minify_schema(raw)
        assert out["required"] == ["mode"]
        assert out["properties"]["mode"]["enum"] == ["a", "b"]
        assert out["properties"]["items"]["items"]["type"] == "integer"

    def test_minification_measurably_shrinks_the_block(self):
        tools = default_registry().list_tools()
        verbose = estimate_tokens(
            json.dumps(
                [
                    {
                        "name": t.name,
                        "description": t.description,
                        "input_schema": t.params.model_json_schema(),
                    }
                    for t in tools
                ],
                separators=(",", ":"),
            )
        )
        terse = estimate_tokens(
            json.dumps([tool_schema(t) for t in tools], separators=(",", ":"))
        )
        assert terse < verbose * 0.85

    def test_schema_uses_the_key_both_providers_read(self):
        schema = tool_schema(default_registry().get("read"))
        assert "input_schema" in schema
        assert "parameters" not in schema


class TestToolProfiles:
    def test_small_model_gets_a_reduced_but_capable_set(self):
        """Reduced, not crippled.

        The earlier version asserted an exact profile name, which broke the moment
        the budget improved. What matters is that the set shrinks *and* keeps the
        capabilities a user assumes exist — the omission of `web_search` is what
        made the agent answer "I don't have a web search tool".
        """
        from kalash.runtime.agent import _fit_prompt

        model = "openai/gpt-oss-20b"
        tools = default_registry().list_tools()
        prompt = estimate_tokens(_fit_prompt(model, "build", None), mode="prose")

        selected, profile = select_profile(
            tools,
            budget_tokens=input_budget(model),
            prompt_tokens=prompt,
            reserve_output=TARGET_OUTPUT_TOKENS,
        )
        names = {t.name for t in selected}

        assert len(selected) < len(tools), "a small model should get a reduced set"
        assert {"read", "write", "edit", "shell", "search", "todo"} <= names
        assert "web_search" in names, "web search must survive budget pressure"
        assert "fetch" in names
        assert profile

    def test_large_model_gets_everything(self):
        tools = default_registry().list_tools()
        selected, profile = select_profile(
            tools,
            budget_tokens=input_budget("anthropic/claude-sonnet-4-5"),
            prompt_tokens=1105,
            reserve_output=TARGET_OUTPUT_TOKENS,
        )
        assert profile == "full"
        assert len(selected) == len(tools)

    def test_minimal_profile_can_still_do_the_job(self):
        # Read, write, edit, search, run, list is enough to build software.
        assert {"read", "write", "edit", "shell", "search"} <= set(MINIMAL_TOOLS)

    def test_output_reservation_is_honoured(self):
        """Without this the tool block ate the whole allowance."""
        tools = default_registry().list_tools()
        greedy, _ = select_profile(
            tools, budget_tokens=5000, prompt_tokens=1105, reserve_output=0
        )
        polite, _ = select_profile(
            tools, budget_tokens=5000, prompt_tokens=1105, reserve_output=2048
        )
        assert len(polite) <= len(greedy)


class TestFixedFloorFitsRealModels:
    """The check that would have caught this before shipping."""

    @pytest.mark.parametrize(
        "model",
        [
            "openai/gpt-oss-20b",
            "openai/gpt-oss-120b",
            "qwen/qwen3.6-27b",
            "llama-3.1-8b-instant",
            "anthropic/claude-sonnet-4-5",
        ],
    )
    def test_a_one_word_prompt_fits(self, model):
        limits = lookup(model)
        prompt_tokens = estimate_tokens(build_system_prompt(), mode="prose")
        tools = default_registry().list_tools()

        if limits.supports_tools:
            selected, _ = select_profile(
                tools,
                budget_tokens=input_budget(model),
                prompt_tokens=prompt_tokens,
                reserve_output=TARGET_OUTPUT_TOKENS,
            )
            schema_cost = estimate_tokens(
                json.dumps([tool_schema(t) for t in selected], separators=(",", ":"))
            )
        else:
            schema_cost = 0

        # "hi" plus framing.
        request = prompt_tokens + schema_cost + 32
        output = resolve_output_tokens(model, 8192, request)
        total = request + output

        if limits.tokens_per_minute:
            assert total <= limits.tokens_per_minute, (
                f"{model}: saying 'hi' costs {total} tokens against a "
                f"{limits.tokens_per_minute} per-minute ceiling"
            )
        assert output <= limits.max_output_tokens
        assert output >= MIN_USABLE_OUTPUT


# ---------------------------------------------------------------------------
# A provider that enforces limits, like a real one does
# ---------------------------------------------------------------------------
#
# The root cause of all three shipped failures was a test double that accepted
# any request. These tests use a double that rejects oversized requests the way
# Groq does, so the loop is validated against the constraint rather than against
# an accommodating mock.

import json as _json

from kalash.core.budget import BudgetState
from kalash.core.events import EventBus
from kalash.models.normalize import (
    BlockDelta,
    BlockStart,
    BlockStop,
    MessageStart,
    MessageStop,
    Role,
    StopReason,
    StreamError,
    TextBlock,
    UsageUpdate,
)
from kalash.runtime.context import ContextAssembler
from kalash.runtime.loop import AgentLoop, TerminationReason
from kalash.runtime.scratchpad import reset_cache
from kalash.runtime.toolhost import ToolHost


class StrictProvider:
    """Rejects requests a real throughput-limited endpoint would reject."""

    context_window = 131_072

    def __init__(self, model: str, *, tpm: int, max_output: int, tools_ok: bool = True):
        self.name = model
        self._tpm = tpm
        self._max_output = max_output
        self._tools_ok = tools_ok
        self.accepted: list[dict] = []
        self.rejections: list[str] = []

    async def stream(self, messages, *, system=None, tools=None, max_tokens=None, **kw):
        requested_output = max_tokens or 8192

        if tools and not self._tools_ok:
            error = (
                "Error code: 400 - {'error': {'message': \"'tool calling' is not "
                "supported with this model\", 'type': 'invalid_request_error'}}"
            )
            self.rejections.append(error)
            yield StreamError(error=error, code="400")
            return

        if requested_output > self._max_output:
            error = (
                f"Error code: 400 - {{'error': {{'message': "
                f"\"'max_completion_tokens' must be less than or equal to "
                f"{self._max_output}\", 'type': 'invalid_request_error'}}}}"
            )
            self.rejections.append(error)
            yield StreamError(error=error, code="400")
            return

        prompt = len(str(system or "")) // 4
        if tools:
            prompt += len(_json.dumps(tools)) // 4
        for message in messages:
            for block in message.content:
                prompt += len(str(getattr(block, "text", "") or "")) // 4

        total = prompt + requested_output
        if total > self._tpm:
            error = (
                f"Error code: 413 - {{'error': {{'message': 'Request too large for "
                f"model `{self.name}` on tokens per minute (TPM): Limit {self._tpm}, "
                f"Requested {total}', 'type': 'tokens'}}}}"
            )
            self.rejections.append(error)
            yield StreamError(error=error, code="413")
            return

        self.accepted.append(
            {"system": system, "tools": tools, "max_tokens": requested_output}
        )
        yield MessageStart(id="m", model=self.name)
        yield BlockStart(index=0, block_type="text")
        yield BlockDelta(index=0, delta="ok")
        yield BlockStop(index=0)
        yield UsageUpdate(input_tokens=prompt, output_tokens=2)
        yield MessageStop(StopReason.END_TURN)


class _Gateway:
    def __init__(self, provider):
        self.primary = provider

    async def stream(self, messages, **kwargs):
        async for event in self.primary.stream(messages, **kwargs):
            yield event


def _build_loop(tmp_path, provider, model: str, *, provider_id: str = "") -> AgentLoop:
    bus = EventBus()
    host = ToolHost(
        registry=default_registry(),
        event_bus=bus,
        session_id="ses_strict",
        cwd=tmp_path,
        model_id=model,
        provider_id=provider_id,
        reserved_prompt_tokens=estimate_tokens(build_system_prompt(), mode="prose"),
    )
    budget = BudgetState(max_tokens=200_000)
    return AgentLoop(
        gateway=_Gateway(provider),
        tool_registry=host,
        session_repo=None,
        event_bus=bus,
        budget=budget,
        assembler=ContextAssembler(budget=budget, context_window=131_072, event_bus=bus),
        session_id="ses_strict",
        system_prompt=build_system_prompt(),
        compact_system_prompt=build_system_prompt(compact=True),
        model_id=model,
        provider_id=provider_id,
    )


async def _say_hi(loop):
    return await loop.run(
        user_message=Message(role=Role.USER, content=[TextBlock(text="hi")]),
        system_identity=loop.system_prompt,
    )


from kalash.models.normalize import Message  # noqa: E402


class TestAgainstStrictProvider:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALASH_HOME", str(tmp_path / "home"))
        reset_cache()
        ConstraintCache.reset()
        yield
        ConstraintCache.reset()
        reset_cache()

    @pytest.mark.asyncio
    async def test_hi_succeeds_on_an_8k_tpm_model(self, tmp_path):
        """This is the exact request that failed with 'Requested 11220'."""
        model = "openai/gpt-oss-20b"
        provider = StrictProvider(model, tpm=8000, max_output=4096)
        result = await _say_hi(_build_loop(tmp_path, provider, model))

        assert not provider.rejections, provider.rejections[:1]
        assert provider.accepted, "the request was never accepted"
        assert result.termination_reason is TerminationReason.NO_TOOL_CALLS

    @pytest.mark.asyncio
    async def test_output_reservation_respects_the_cap(self, tmp_path):
        model = "openai/gpt-oss-20b"
        provider = StrictProvider(model, tpm=8000, max_output=4096)
        await _say_hi(_build_loop(tmp_path, provider, model))
        assert provider.accepted[0]["max_tokens"] <= 4096

    @pytest.mark.asyncio
    async def test_tool_block_is_trimmed_for_a_small_model(self, tmp_path):
        model = "openai/gpt-oss-20b"
        provider = StrictProvider(model, tpm=8000, max_output=4096)
        await _say_hi(_build_loop(tmp_path, provider, model))

        tools = provider.accepted[0]["tools"]
        names = {t["name"] for t in tools}
        assert tools, "tools must still be sent"
        # The requirement is that the request is accepted while keeping the
        # capabilities users assume, not that the count is under some number.
        assert len(tools) < 15, f"expected a reduced set, got {len(tools)}"
        assert {"read", "write", "edit", "shell", "web_search"} <= names

    @pytest.mark.asyncio
    async def test_large_model_receives_the_full_set(self, tmp_path):
        model = "anthropic/claude-sonnet-4-5"
        provider = StrictProvider(model, tpm=10_000_000, max_output=64_000)
        await _say_hi(_build_loop(tmp_path, provider, model))
        assert len(provider.accepted[0]["tools"]) >= 14

    @pytest.mark.asyncio
    async def test_tools_are_withheld_from_a_model_that_rejects_them(self, tmp_path):
        model = "groq/compound"
        provider = StrictProvider(model, tpm=8000, max_output=8192, tools_ok=False)
        result = await _say_hi(_build_loop(tmp_path, provider, model))

        assert not provider.rejections, "no tools should have been sent at all"
        assert provider.accepted[0]["tools"] in (None, [])
        assert result.termination_reason is TerminationReason.NO_TOOL_CALLS

    @pytest.mark.asyncio
    async def test_over_cap_reservation_is_repaired_and_retried(self, tmp_path):
        """A provider-stated cap is repairable without asking the user."""
        model = "unknown-model-with-tight-cap"
        provider = StrictProvider(model, tpm=1_000_000, max_output=2048)
        loop = _build_loop(tmp_path, provider, model)
        loop.max_output_tokens = 8192

        result = await _say_hi(loop)

        assert provider.rejections, "the first attempt should have been rejected"
        assert provider.accepted, "the retry should have succeeded"
        assert provider.accepted[0]["max_tokens"] == 2048
        assert result.termination_reason is TerminationReason.NO_TOOL_CALLS

    @pytest.mark.asyncio
    async def test_unrepairable_failure_reports_a_readable_reason(self, tmp_path):
        model = "openai/gpt-oss-20b"
        provider = StrictProvider(model, tpm=200, max_output=4096)  # impossibly tight
        result = await _say_hi(_build_loop(tmp_path, provider, model))

        assert result.termination_reason is TerminationReason.ERROR
        assert result.error
        assert "{'error'" not in result.error
        assert "per minute" in result.error


class TestCrossProviderTpmSmoke:
    """Smoke-test the same prompt against several TPM ceilings."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALASH_HOME", str(tmp_path / "home"))
        reset_cache()
        ConstraintCache.reset()
        yield
        reset_cache()
        ConstraintCache.reset()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tpm", [8_000, 12_000, 20_000, 60_000])
    async def test_hi_succeeds_across_tpm_tiers(self, tmp_path, tpm):
        model = "totally-unknown-checkpoint"
        provider = StrictProvider(model, tpm=tpm, max_output=4096)
        result = await _say_hi(
            _build_loop(tmp_path, provider, model, provider_id="groq")
        )

        assert provider.accepted, f"TPM {tpm}: {provider.rejections[:1]}"
        assert not provider.rejections
        assert result.termination_reason is TerminationReason.NO_TOOL_CALLS
        total = provider.accepted[0]["max_tokens"] + len(
            str(provider.accepted[0]["system"] or "")
        ) // 4
        assert total <= tpm

    @pytest.mark.asyncio
    async def test_learned_tpm_from_rejection_fixes_the_retry(self, tmp_path):
        model = "mystery-model"
        provider = StrictProvider(model, tpm=8_000, max_output=4096)
        loop = _build_loop(tmp_path, provider, model, provider_id="groq")
        loop.max_output_tokens = 8192

        # Pretend we already learned a tighter ceiling from a prior error.
        ConstraintCache.record_tpm(model, 8_000)

        result = await _say_hi(loop)
        assert provider.accepted
        assert result.termination_reason is TerminationReason.NO_TOOL_CALLS


# ---------------------------------------------------------------------------
# Prompt tiering
# ---------------------------------------------------------------------------
#
# The system prompt grew from ~1.1k to ~3.9k tokens. On an 8k-per-minute
# endpoint that left no room for tools plus a usable reply, which is the same
# class of failure as the hardcoded output cap, one layer up. Tiering the prompt
# is the fix; these tests keep it honest as the prompt evolves.


class TestPromptTiers:
    def test_compact_is_materially_smaller(self):
        from kalash.runtime.prompt import build_system_prompt

        full = estimate_tokens(build_system_prompt(), mode="prose")
        compact = estimate_tokens(build_system_prompt(compact=True), mode="prose")
        assert compact < full * 0.7

    def test_compact_keeps_the_load_bearing_content(self):
        """Dropping any of these would break capability or safety."""
        from kalash.runtime.prompt import build_system_prompt

        compact = build_system_prompt(compact=True).lower()
        assert "filesystem" in compact, "must know it can act on files"
        assert "todo" in compact, "must know to plan multi-step work"
        assert "expand" in compact, "must know how to retrieve scratchpad refs"
        for marker in ("untrusted", "confirm", "approval", "wait"):
            if marker in compact:
                break
        else:
            pytest.fail("compact prompt dropped the safety contract")

    def test_plan_mode_survives_compaction(self):
        from kalash.runtime.prompt import build_system_prompt

        compact = build_system_prompt(mode="plan", compact=True)
        assert "plan" in compact.lower()

    def test_small_model_gets_the_compact_tier(self):
        from kalash.runtime.agent import _fit_prompt
        from kalash.runtime.prompt import build_system_prompt

        chosen = _fit_prompt("openai/gpt-oss-20b", "build", None)
        assert estimate_tokens(chosen, mode="prose") < estimate_tokens(
            build_system_prompt(), mode="prose"
        )

    def test_large_model_gets_the_full_tier(self):
        from kalash.runtime.agent import _fit_prompt
        from kalash.runtime.prompt import build_system_prompt

        chosen = _fit_prompt("anthropic/claude-sonnet-4-5", "build", None)
        assert chosen == build_system_prompt(
            mode="build", project_instructions=None
        )

    @pytest.mark.parametrize(
        "model",
        [
            "openai/gpt-oss-20b",
            "openai/gpt-oss-120b",
            "qwen/qwen3.6-27b",
            "llama-3.1-8b-instant",
            "anthropic/claude-sonnet-4-5",
        ],
    )
    def test_a_one_word_prompt_leaves_room_to_reply(self, model):
        """The end-to-end budget check, using the paths the product takes."""
        from kalash.runtime.agent import _fit_prompt

        limits = lookup(model)
        prompt = estimate_tokens(_fit_prompt(model, "build", None), mode="prose")
        tools = default_registry().list_tools()

        selected, _ = select_profile(
            tools,
            budget_tokens=input_budget(model),
            prompt_tokens=prompt,
            reserve_output=TARGET_OUTPUT_TOKENS,
        )
        schema_cost = estimate_tokens(
            json.dumps([tool_schema(t) for t in selected], separators=(",", ":"))
        )
        request = prompt + schema_cost + 32
        output = resolve_output_tokens(model, 8192, request)

        assert selected, f"{model} ended up with no tools"
        assert output >= MIN_USABLE_OUTPUT, f"{model} left {output} tokens to answer"
        if limits.tokens_per_minute:
            assert request + output <= limits.tokens_per_minute


# ---------------------------------------------------------------------------
# Sandbox network and correctness
# ---------------------------------------------------------------------------
#
# The reported freeze was `npm create vite` hanging for the full 120s timeout.
# Cause: the sandbox wrapper never received allow_network, so every package
# manager ran with --unshare-net and spun on DNS retries. Two further bugs sat
# behind it: a read-only /dev broke `>/dev/null`, and /etc/resolv.conf is a
# symlink into /run on systemd hosts, so DNS failed even once network was on.


class TestSandboxNetworkDecision:
    @pytest.mark.parametrize(
        "command",
        [
            "npm create vite@latest app -- --template react",
            "npm install",
            "pnpm add react",
            "pip install requests",
            "uv sync",
            "cargo build",
            "git clone https://github.com/x/y",
            "curl https://example.com",
            "cd frontend && npm ci",
            "npx tsc --noEmit && npm test",
        ],
    )
    def test_network_commands_are_granted_network(self, command):
        from kalash.permissions.classify import classify_tool_call
        from kalash.runtime.toolhost import _needs_network

        risk = classify_tool_call("shell", {"command": command})
        assert _needs_network(risk), f"{command} would hang without network"

    @pytest.mark.parametrize(
        "command", ["ls -la", "cat package.json", "rg pattern src", "wc -l x.py"]
    )
    def test_local_commands_stay_isolated(self, command):
        from kalash.permissions.classify import classify_tool_call
        from kalash.runtime.toolhost import _needs_network

        risk = classify_tool_call("shell", {"command": command})
        assert not _needs_network(risk)

    def test_fetch_and_search_tools_get_network(self):
        from kalash.permissions.classify import classify_tool_call
        from kalash.runtime.toolhost import _needs_network

        for name in ("fetch", "web_search"):
            assert _needs_network(classify_tool_call(name, {"url": "https://x"}))

    def test_unshare_net_tracks_the_decision(self, tmp_path):
        import shutil
        import sys

        if not sys.platform.startswith("linux") or shutil.which("bwrap") is None:
            pytest.skip("bwrap not available")

        from kalash.tools.base import ToolContext
        from kalash.tools.shell import _wrap_sandboxed

        for allow in (False, True):
            ctx = ToolContext(
                session_id="s",
                run_id="r",
                cwd=tmp_path,
                writable_roots=(tmp_path,),
                allow_network=allow,
            )
            argv, wrapped, _warning = _wrap_sandboxed(["/bin/sh", "-c", "true"], ctx, tmp_path)
            if not wrapped:
                pytest.skip("sandbox preflight declined on this host")
            assert ("--unshare-net" in argv) is (not allow)

    def test_dev_is_a_devtmpfs_not_a_readonly_bind(self, tmp_path):
        """A read-only /dev makes `>/dev/null` fail in most build scripts."""
        import shutil
        import sys

        if not sys.platform.startswith("linux") or shutil.which("bwrap") is None:
            pytest.skip("bwrap not available")

        from kalash.sandbox.linux import LinuxSandbox
        from kalash.sandbox.policy import SandboxMode, SandboxPolicy

        argv = LinuxSandbox(
            policy=SandboxPolicy(
                mode=SandboxMode.WORKSPACE_WRITE, workspace_root=tmp_path
            )
        ).wrap_command(["/bin/sh", "-c", "true"], cwd=str(tmp_path))

        assert "--dev" in argv
        assert not any(
            argv[i] == "--ro-bind" and argv[i + 1] == "/dev"
            for i in range(len(argv) - 1)
        )

    @pytest.mark.asyncio
    async def test_dev_null_redirect_works(self, tmp_path):
        from kalash.tools.base import ToolContext
        from kalash.tools.shell import ShellParams, ShellTool

        ctx = ToolContext(
            session_id="s", run_id="r", cwd=tmp_path, writable_roots=(tmp_path,)
        )
        env = await ShellTool().execute(
            ShellParams(command="echo hidden >/dev/null && echo visible", timeout=20),
            ctx,
        )
        assert env.ok, env.error.message if env.error else ""
        assert "visible" in env.content
        assert "/dev" not in (env.content or "")

    def test_preflight_is_cached_and_reports(self):
        from kalash.sandbox.manager import preflight_ok, preflight_report

        first = preflight_ok()
        assert preflight_ok() is first
        assert preflight_report()
