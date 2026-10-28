"""Use repeated real usage observations when text tokenizes denser than the fallback."""

from kalash.agent.context import fixed_tokens, message_tokens
from kalash.llm.types import Message, TextBlock, Usage, user_text
from tests.harness.scripted import say


async def test_summary_chunks_use_observed_token_density(make_harness):
    window = 8192

    def must_fit(request):
        if "compaction engine" not in request.messages[-1].text:
            return
        # The provider tokenizer in this trace uses 2.4x the fallback message estimate.
        actual = fixed_tokens(request.system, request.tools) + sum(
            message_tokens(m) * (2.4 if "a91d71bf3d0267e4" in m.text else 1.0)
            for m in request.messages
        )
        assert actual + request.max_tokens <= window

    h = await make_harness([say("checkpoint", on_start=must_fit) for _ in range(20)], window=window)
    for i in range(3):
        h.session.append_message(user_text(f"batch {i}: " + "a91d71bf3d0267e4" * 400))
        history = h.session.messages()
        used = fixed_tokens(h.agent.system_prompt, h.agent.tool_specs()) + int(
            2.4 * sum(message_tokens(m) for m in history)
        )
        h.session.append_message(
            Message("assistant", [TextBlock("ACK")], origin=h.agent.model_label),
            usage=Usage(used, 3).to_dict(),
        )
    result = await h.turn("continue with the preserved task")

    assert result.reason == "completed"
    summaries = [e for e in h.session.events if e.type == "compaction_request"]
    assert len(summaries) >= 2 and all(e.data["token_ratio"] > 2 for e in summaries)
