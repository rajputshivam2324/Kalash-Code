"""A model that goes silent after its last tool result is asked once, in words, for a summary."""

from __future__ import annotations

from kalash.agent.events import Notice
from kalash.llm.errors import ErrorCode
from tests.harness.scripted import call, fail, say


async def test_an_empty_reply_after_a_tool_result_is_nudged_once_and_the_turn_completes(
    make_harness,
):
    h = await make_harness(
        [
            call("bash", {"description": "stop the job", "command": "echo stopped"}),
            fail(ErrorCode.EMPTY_RESPONSE, "model returned an empty response"),
            fail(ErrorCode.EMPTY_RESPONSE, "model returned an empty response"),  # the one retry
            say("I stopped the job and confirmed it exited."),
        ]
    )
    result = await h.turn("stop the job")
    assert result.reason == "completed" and "confirmed it exited" in result.text
    asked = h.adapter.requests[-1].messages
    assert asked[-1].role == "user" and "your last reply was empty" in asked[-1].text
    assert asked[-1].visible_text == "", "the nudge is harness text, never shown as the user's"
    assert any("empty reply" in n.text for n in h.of(Notice))
    h.adapter.assert_exhausted()


async def test_a_second_empty_reply_is_reported_as_the_error_it_is(make_harness):
    h = await make_harness(
        [
            call("bash", {"description": "x", "command": "true"}),
            *[fail(ErrorCode.EMPTY_RESPONSE)] * 4,  # (ask + one retry) before and after the nudge
        ]
    )
    result = await h.turn("go")
    assert result.reason == "error" and result.error_code == "EMPTY_RESPONSE"
    nudges = [m for m in h.session.messages() if "your last reply was empty" in m.text]
    assert len(nudges) == 1, "asked once, not in a loop"


async def test_an_empty_first_reply_is_not_nudged_because_there_is_nothing_to_summarise(
    make_harness,
):
    h = await make_harness([fail(ErrorCode.EMPTY_RESPONSE)] * 2)
    result = await h.turn("hello")
    assert result.reason == "error" and result.error_code == "EMPTY_RESPONSE"
    assert not [m for m in h.session.messages() if "your last reply was empty" in m.text]
