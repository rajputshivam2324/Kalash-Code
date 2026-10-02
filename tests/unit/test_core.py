"""Tests for the core layer."""

from decimal import Decimal

import pytest

from kalash.core.budget import BudgetState, Pricing, Usage, estimate_tokens
from kalash.core.errors import KalashError, ModelError, ToolError
from kalash.core.events import Event, EventBus, EventType
from kalash.core.ids import generate_id, timestamp_from_id

# --- ID generation ---


class TestIDs:
    def test_generate_id_no_prefix(self):
        uid = generate_id()
        assert len(uid) == 26  # ULID is 26 chars in Crockford Base32

    def test_generate_id_with_prefix(self):
        uid = generate_id("mem_")
        assert uid.startswith("mem_")
        assert len(uid) == 30  # 4 prefix + 26 ULID

    def test_ids_are_unique(self):
        ids = {generate_id() for _ in range(100)}
        assert len(ids) == 100

    def test_ids_are_sortable_by_time(self):
        import time

        id1 = generate_id()
        time.sleep(0.002)
        id2 = generate_id()
        assert id1 < id2  # lexicographic order = chronological

    def test_timestamp_extraction(self):
        import time

        before = time.time() - 0.01  # small tolerance
        uid = generate_id()
        after = time.time() + 0.01
        ts = timestamp_from_id(uid)
        assert before <= ts <= after


# --- Errors ---


class TestErrors:
    def test_base_error(self):
        err = KalashError("something went wrong")
        assert str(err) == "something went wrong"
        assert err.code == "KALASH_INTERNAL"
        assert err.recoverable is False

    def test_tool_error_code(self):
        err = ToolError("bad args", code="KALASH_TOOL_INVALID_ARGS")
        assert err.code == "KALASH_TOOL_INVALID_ARGS"

    def test_model_error(self):
        err = ModelError("all failed", recoverable=False)
        assert err.code == "KALASH_MODEL_ERROR"
        assert not err.recoverable


# --- Budget ---


class TestBudget:
    def test_usage_total_tokens(self):
        usage = Usage(input_tokens=100, output_tokens=50, cache_write_tokens=10)
        assert usage.total_tokens == 160

    def test_budget_state_ceiling_check(self):
        state = BudgetState(max_tokens=1000, tokens_used=999)
        assert state.check_ceiling() is None
        state.tokens_used = 1000
        assert state.check_ceiling() == "tokens"

    def test_budget_state_warning(self):
        state = BudgetState(max_tokens=100, tokens_used=76)
        assert state.check_warning(0.75) == "tokens"

    def test_budget_record_usage(self):
        state = BudgetState(max_tokens=100_000)
        pricing = Pricing(
            input_per_mtok=Decimal("3.00"),
            output_per_mtok=Decimal("15.00"),
        )
        usage = Usage(input_tokens=1000, output_tokens=500)
        state.record_usage(usage, pricing)
        assert state.tokens_used == 1500
        assert state.cost_used > Decimal("0")

    def test_estimate_tokens_code(self):
        text = "x" * 320
        tokens = estimate_tokens(text, mode="code")
        # 320 / 3.2 * 1.05 = 105
        assert tokens == 105

    def test_estimate_tokens_prose(self):
        text = "x" * 400
        tokens = estimate_tokens(text, mode="prose")
        # 400 / 4.0 * 1.05 = 105
        assert tokens == 105

    def test_reserve_for_child(self):
        state = BudgetState(max_tokens=10000)
        assert state.reserve_for_child(5000, Decimal("1.00"))
        assert state.tokens_reserved == 5000
        assert state.remaining_tokens() == 5000

    def test_reserve_insufficient(self):
        state = BudgetState(max_tokens=100, tokens_used=90)
        assert not state.reserve_for_child(50, Decimal("1.00"))


# --- Events ---


class TestEvents:
    @pytest.mark.asyncio
    async def test_event_emit_and_receive(self):
        bus = EventBus()
        received = []

        async def handler(event: Event):
            received.append(event)

        bus.on(EventType.TURN_START, handler)
        await bus.emit(Event(type=EventType.TURN_START, data={"seq": 1}))
        assert len(received) == 1
        assert received[0].data["seq"] == 1

    @pytest.mark.asyncio
    async def test_event_no_handler(self):
        bus = EventBus()
        # Should not raise
        await bus.emit(Event(type=EventType.SESSION_START))

    @pytest.mark.asyncio
    async def test_event_handler_error_swallowed(self):
        bus = EventBus()

        async def bad_handler(event: Event):
            raise RuntimeError("oops")

        bus.on(EventType.TURN_START, bad_handler)
        # Should not propagate
        await bus.emit(Event(type=EventType.TURN_START))
