"""Work continues independently of retired spending and step settings."""

from pathlib import Path

from kalash.agent.events import Notice
from kalash.config import Settings, load_settings
from kalash.llm.types import Usage
from tests.harness.scripted import call, say


async def test_legacy_caps_do_not_stop_work_or_request_an_early_wrap_up(make_harness, work: Path):
    (work / "input.txt").write_text("keep working")
    h = await make_harness(
        [call("read", {"file_path": "input.txt"}, usage=Usage(1000, 50)) for _ in range(205)]
        + [say("completed and verified")],
        limits={"max_steps_per_turn": 1, "max_session_tokens": 1, "max_session_cost_usd": 0.01},
        pricing={"scripted/model": {"input_per_mtok": 100, "output_per_mtok": 100}},
    )
    result = await h.turn("finish the work")

    assert result.reason == "completed" and result.steps == 206
    assert result.tool_calls == 205
    assert h.session.usage_total().total > 200_000 and h.session.cost_total() > 1
    assert not any("budget" in m.text.lower() for m in h.session.messages() if m.role == "tool")
    assert not any("cost cap" in e.text for e in h.of(Notice))
    h.adapter.assert_exhausted()


def test_retired_limits_are_absent_from_effective_settings(work: Path):
    cfg, _ = load_settings(work, {"limits": {"max_steps_per_turn": 1}})
    assert "limits" not in cfg.model_dump()
    direct = Settings.model_validate({"budget": {"max_tokens": 1}})
    assert "budget" not in direct.model_dump()


async def test_explicit_per_response_output_target_is_respected(make_harness):
    h = await make_harness([say("ok")], max_output_tokens=256)
    await h.turn("hello")
    assert h.adapter.requests[0].max_tokens == 256
