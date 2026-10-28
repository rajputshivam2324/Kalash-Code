"""The summarizer must fit the same physical context window as ordinary requests."""

from kalash.agent.context import fixed_tokens, message_tokens
from kalash.llm.errors import ErrorCode
from kalash.llm.types import Message, TextBlock, ToolCallBlock, Usage, tool_result, user_text
from tests.contract.test_turn_contracts import assert_paired
from tests.harness.scripted import fail, say


async def test_long_history_is_summarized_in_fitting_chunks(make_harness):
    window = 10_000

    def must_fit(request):
        used = fixed_tokens(request.system, request.tools) + sum(
            message_tokens(m) for m in request.messages
        )
        assert used + (request.max_tokens or 0) <= window, f"summary request overflow: {used}"
        assert_paired(request.messages)

    h = await make_harness(
        [say("Consolidated checkpoint.", on_start=must_fit) for _ in range(30)], window=window
    )
    for index in range(24):
        h.session.append_message(user_text(f"request {index}: " + "detail " * 900))
    original_users = sum(e.type == "user" for e in h.session.events)
    result = await h.turn("continue implementing and verify the tests")

    assert result.reason == "completed"
    summaries = [r for r in h.adapter.requests if "compaction engine" in r.messages[-1].text]
    assert len(summaries) >= 3
    assert sum(e.type == "compaction_request" for e in h.session.events) == len(summaries)
    assert sum(e.type == "user" for e in h.session.events) == original_users + 1
    assert "continue implementing and verify the tests" in h.adapter.requests[-1].messages[0].text


async def test_a_large_tool_exchange_stays_paired_while_its_summary_copy_is_reduced(make_harness):
    window = 10_000

    def must_fit(request):
        assert_paired(request.messages)
        assert (
            fixed_tokens(request.system, request.tools)
            + sum(message_tokens(m) for m in request.messages)
            + (request.max_tokens or 0)
            <= window
        )

    h = await make_harness(
        [say("Checkpoint.", on_start=must_fit) for _ in range(20)], window=window
    )
    h.session.append_message(user_text("Build the application without editing its verifier."))
    large = "archived-file-body:" + "x" * 100_000
    call = ToolCallBlock("large-write", "write", '{"file_path":"app.py","content":"' + large + '"}')
    h.session.append_message(Message("assistant", [call]))
    h.session.append_message(tool_result(call, "Created app.py."))
    result = await h.turn("continue")

    assert result.reason == "completed"
    assert_paired(h.session.messages())
    assert any(
        large in e.data.get("message", {}).get("content", [{}])[0].get("arguments", "")
        for e in h.session.events
        if e.type == "assistant"
    )
    assert any(
        "archive" in str(e.data).lower() for e in h.session.events if e.type == "compaction_request"
    )


async def test_later_chunk_failure_preserves_original_history_and_all_completed_usage(make_harness):
    h = await make_harness(
        [say("Checkpoint one.", usage=Usage(500, 100)), fail(ErrorCode.AUTH)],
        window=10_000,
    )
    for index in range(24):
        h.session.append_message(user_text(f"original-{index}: " + "detail " * 900))
    result = await h.turn("continue")

    assert result.reason == "error" and result.error_code == "AUTH"
    assert result.usage.total == h.session.usage_total().total == 600
    assert not any(e.type == "compaction" for e in h.session.events)
    assert any("original-0:" in m.text for m in h.session.messages())
    assert len([e for e in h.session.events if e.type == "compaction_request"]) == 2


async def test_current_intent_and_task_list_are_pinned_even_when_summary_omits_them(make_harness):
    h = await make_harness([say("unhelpful summary") for _ in range(20)], window=10_000)
    h.session.append_message(user_text("Build the API. Do not change the independent verifier."))
    h.session.append("todo", todos=[{"content": "Verify API startup", "status": "in_progress"}])
    for _ in range(6):
        h.session.append_message(Message("assistant", [TextBlock("history " * 900)]))
    result = await h.turn("Correction: keep the existing database schema too.")

    assert result.reason == "completed"
    pinned = h.adapter.requests[-1].messages[0].text
    assert "Do not change the independent verifier." in pinned
    assert "keep the existing database schema" in pinned
    assert "Verify API startup" in pinned


async def test_summary_tools_are_quoted_history_rather_than_callable_actions(make_harness):
    def inspect(request):
        if "compaction engine" not in request.messages[-1].text:
            return
        assert not request.tools
        assert not any(m.tool_calls or m.role == "tool" for m in request.messages)

    h = await make_harness([say("checkpoint", on_start=inspect) for _ in range(20)], window=10000)
    h.session.append_message(user_text("Remember the project test command."))
    call = ToolCallBlock(
        "remember", "memory", '{"action":"remember","key":"tests","text":"pytest"}'
    )
    h.session.append_message(Message("assistant", [call]))
    h.session.append_message(tool_result(call, "Remembered tests."))
    for _ in range(8):
        h.session.append_message(user_text("data " * 1000))

    result = await h.turn("continue")

    assert result.reason == "completed", result.error
    summaries = [e for e in h.session.events if e.type == "compaction_request"]
    quoted = "".join(str(e.data) for e in summaries)
    assert "Remembered tests." in quoted and '"action":"remember"' in quoted
    assert not any(e.type == "memory" for e in h.session.events)
