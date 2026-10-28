"""Compaction must honor the reasoning control selected for the active model."""

from kalash.llm.catalog import ModelInfo
from tests.contract.test_compaction import BIG, WINDOW, big_reply
from tests.harness.scripted import say


async def test_compaction_passes_the_selected_effort_to_the_provider(make_harness):
    def inspect(request):
        assert request.reasoning_effort == "low"

    h = await make_harness(
        [big_reply(), say("checkpoint", on_start=inspect), say("continued")], window=WINDOW
    )
    h.agent.info = ModelInfo(
        "model", WINDOW, 4096, thinking="effort", efforts=("low", "high"), default_effort="high"
    )
    h.agent.set_effort("low")
    await h.turn(BIG)
    result = await h.turn("continue")
    assert result.reason == "completed"
    request = next(e for e in h.session.events if e.type == "compaction_request")
    assert request.data["reasoning_effort"] == "low"
