"""All tools remain usable when verbose schemas crowd a small context window."""

from kalash.agent.context import fixed_tokens, message_tokens
from tests.harness.scripted import say


async def test_four_k_context_can_start_with_all_tools_and_valid_schemas(make_harness):
    h = await make_harness([say("ready")], window=4096, max_output_tokens=512)
    result = await h.turn("hello")

    assert result.reason == "completed" and result.text == "ready"
    request = h.adapter.requests[0]
    assert {t.name for t in request.tools} == set(h.agent.registry.names())
    assert (
        fixed_tokens(request.system, request.tools)
        + sum(message_tokens(m) for m in request.messages)
        + request.max_tokens
        <= 4096
    )
    schema_event = next(e for e in h.session.events if e.type == "tool_schema")
    assert schema_event.data["profile"] == "compact"
    assert [t["name"] for t in schema_event.data["tools"]] == [t.name for t in request.tools]
    for compact, full in zip(request.tools, h.agent.registry.specs(), strict=True):
        assert compact.parameters.get("required") == full.parameters.get("required")
        assert set(compact.parameters.get("properties", {})) == set(
            full.parameters.get("properties", {})
        )
        assert compact.parameters["additionalProperties"] is False
    memory = next(t for t in request.tools if t.name == "memory")
    assert memory.parameters["properties"]["scope"]["enum"] == ["session", "project"]
    assert memory.parameters["properties"]["limit"]["minimum"] == 128
    assert memory.parameters["properties"]["limit"]["maximum"] == 20_000
