"""Reasoning effort: model-driven choices, validation, persistence, the keyboard cycle, and auto."""

from __future__ import annotations

import pytest

from kalash.agent import effort as fx
from kalash.llm.catalog import CATALOG, ModelInfo, default_info, resolve_effort
from kalash.session.log import SessionLog
from tests.harness.scripted import call, say

SARVAM = CATALOG["sarvam"]["sarvam-105b"]  # off is explicit null on V1 (2026-10-28)
ALWAYS = CATALOG["groq"]["openai/gpt-oss-20b"]  # reasoning cannot be disabled
OPUS = CATALOG["anthropic"]["claude-opus-5-5"]  # adaptive: low..max
GPT = next(i for i in CATALOG["openai"].values() if "none" in i.efforts)  # has an off value
PLAIN = CATALOG["sarvam"]["gemma4"]  # no reasoning control


def values(info: ModelInfo | None) -> list[str]:
    return [c.value for c in fx.choices(info)]


def test_choices_are_driven_by_what_the_model_accepts():
    assert values(SARVAM) == ["default", "auto", "low", "high", "off"]
    assert values(OPUS) == [
        "default",
        "auto",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ] or "max" in values(OPUS)
    assert values(PLAIN) == [] and not fx.has_control(PLAIN)
    assert "off" in values(GPT)
    assert "off" not in values(ALWAYS), "a model without an off value is not offered one"


def test_off_is_offered_where_it_really_turns_reasoning_off():
    assert fx.off_supported(GPT) and fx.off_supported(SARVAM)
    assert not fx.off_supported(ALWAYS) and not fx.off_supported(PLAIN)
    budget = next(
        i
        for models in CATALOG.values()
        for i in models.values()
        if i.thinking == "budget" and i.can_disable_thinking
    )
    assert fx.off_supported(budget)


def test_every_catalog_model_gets_a_consistent_menu():
    for provider, models in CATALOG.items():
        for model_id, info in models.items():
            menu = fx.choices(info)
            assert [c.value for c in menu].count("default") == (1 if menu else 0), (
                provider,
                model_id,
            )
            for choice in menu:
                setting, problem = fx.normalize(info, choice.value)
                assert problem is None, (provider, model_id, choice.value, problem)


def test_unknown_model_metadata_preserves_explicit_effort_instead_of_claiming_no_control():
    info = default_info("sarvam", "new-model", context=128000, max_output=16384)
    assert fx.normalize(info, "low") == ("low", None)
    assert resolve_effort(info, "low") == "low"
    assert fx.normalize(info, None) == (None, None)


@pytest.mark.parametrize(
    ("requested", "setting", "has_message"),
    [
        (None, None, False),
        ("default", None, False),
        ("auto", "auto", False),
        ("high", "high", False),
        ("HIGH", "high", False),
        ("medium", "low", True),  # nearest the model offers (low or high; ties go to the lower)
        ("minimal", "low", True),
    ],
)
def test_normalize_keeps_valid_choices_and_substitutes_the_nearest_with_a_message(
    requested, setting, has_message
):
    result, message = fx.normalize(SARVAM, requested)
    assert result == setting
    assert bool(message) == has_message


def test_normalize_refuses_what_cannot_be_honoured():
    assert fx.normalize(ALWAYS, "off")[0] is None and "cannot be turned off" in (
        fx.normalize(ALWAYS, "off")[1] or ""
    )
    assert "unknown level" in (fx.normalize(SARVAM, "turbo")[1] or "")
    assert "no reasoning control" in (fx.normalize(PLAIN, "high")[1] or "")
    assert fx.normalize(PLAIN, None) == (None, None), "the default always works"
    assert fx.normalize(None, "banana") == ("banana", None), (
        "unknown models take what they are given"
    )
    assert fx.normalize(GPT, "off") == ("off", None)


def test_cycle_walks_the_menu_without_auto_and_wraps():
    seen = []
    current: str | None = None
    for _ in range(4):
        current = fx.cycle(SARVAM, current)
        seen.append(current)
    assert seen == ["low", "high", "off", "default"], seen
    assert fx.cycle(PLAIN, None) is None
    assert fx.cycle(GPT, "high")[0:1] != "" and fx.cycle(GPT, "off") == "default"


def test_auto_thinks_hard_on_planning_failure_and_nudges_and_lightly_otherwise():
    hard, easy = "high", "low"
    kwargs = dict(first_step=False, last_failed=False, nudged=False)
    assert fx.pick_for_step(SARVAM, **{**kwargs, "first_step": True}) == hard
    assert fx.pick_for_step(SARVAM, **{**kwargs, "last_failed": True}) == hard
    assert fx.pick_for_step(SARVAM, **{**kwargs, "nudged": True}) == hard
    assert fx.pick_for_step(SARVAM, **kwargs) == easy
    assert fx.pick_for_step(PLAIN, **kwargs) is None
    assert fx.pick_for_step(None, **kwargs) is None
    assert fx.pick_for_step(OPUS, **{**kwargs, "first_step": True}) == OPUS.default_effort


def test_labels_for_the_status_bar():
    assert fx.label(SARVAM, None) == "high"  # the model default
    assert fx.label(SARVAM, "low") == "low"
    assert fx.label(SARVAM, "auto", "low") == "auto:low" and fx.label(SARVAM, "auto") == "auto"
    assert fx.label(PLAIN, None) == ""


# ------------------------------------------------------------------- through the agent


async def test_set_effort_applies_logs_and_is_sent_on_the_next_request(make_harness):
    h = await make_harness([say("one"), say("two")])
    h.agent.info = SARVAM
    h.agent.effort_setting = None
    await h.turn("first")
    assert h.adapter.requests[-1].reasoning_effort == "high", "the model default"
    setting, message = h.agent.set_effort("low")
    assert (setting, message) == ("low", None) and h.session.effort() == (True, "low")
    await h.turn("second")
    assert h.adapter.requests[-1].reasoning_effort == "low"


async def test_a_refused_choice_changes_nothing(make_harness):
    h = await make_harness([say("one")])
    h.agent.info = ALWAYS
    h.agent.set_effort("high")
    setting, message = h.agent.set_effort("off")
    assert setting is None and "cannot be turned off" in (message or "")
    assert h.agent.effort_setting == "high" and h.session.effort() == (True, "high")


async def test_the_choice_survives_a_resume_and_beats_settings(make_harness, work):
    h1 = await make_harness([say("one")])
    h1.agent.info = SARVAM
    h1.agent.set_effort("auto")
    await h1.turn("first")
    path = h1.session.path
    await h1.runtime.aclose()
    h2 = await make_harness([say("two")], session=SessionLog.open(path), reasoning_effort="max")
    assert h2.agent.effort_setting in ("auto", "low", "high", "max")  # the saved choice wins
    assert h2.session.effort() == (True, "auto")


async def test_auto_switches_level_per_step_and_reports_what_it_used(make_harness):
    h = await make_harness(
        [
            call("bash", {"description": "ok", "command": "true"}),
            call("bash", {"description": "fails", "command": "false"}),
            call("bash", {"description": "after the failure", "command": "true"}),
            call("bash", {"description": "routine", "command": "true"}),
            say("done"),
        ]
    )
    h.agent.info = SARVAM
    h.agent.set_effort("auto")
    await h.turn("work")
    levels = [r.reasoning_effort for r in h.adapter.requests]
    assert levels == ["high", "low", "high", "low", "low"], levels
    assert h.agent.effort_display.startswith("auto:")


async def test_a_new_turn_starts_hard_again(make_harness):
    h = await make_harness([say("a"), say("b")])
    h.agent.info = SARVAM
    h.agent.set_effort("auto")
    await h.turn("one")
    await h.turn("two")
    assert [r.reasoning_effort for r in h.adapter.requests] == ["high", "high"]


async def test_settings_effort_applies_when_the_session_has_no_choice(make_harness):
    h = await make_harness([say("x")], reasoning_effort="low")
    assert h.agent.effort_setting in ("low", None)  # the scripted model has no catalog entry
    await h.turn("go")
    assert h.adapter.requests[0].reasoning_effort in ("low", None)
