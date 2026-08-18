"""Phased iterations: fixed per-provider token budget, rollup, final synthesis."""

from __future__ import annotations

import pytest

from kalash.core.budget import BudgetState
from kalash.core.events import EventBus
from kalash.models.limits import IterationBudget, iteration_budget, tpm_allowance
from kalash.models.normalize import Message, Role, TextBlock, ToolResultBlock
from kalash.runtime.context import ContextAssembler
from kalash.runtime.loop import AgentLoop, TerminationReason
from kalash.runtime.prompt import build_system_prompt


class TestIterationBudget:
    def test_groq_model_gets_phased_fixed_envelope(self):
        budget = iteration_budget("openai/gpt-oss-20b", provider_id="groq")
        assert budget.phased is True
        allowance = tpm_allowance("openai/gpt-oss-20b", provider_id="groq")
        assert allowance is not None
        assert budget.request_allowance == allowance
        assert budget.max_output_tokens >= 1024
        assert budget.max_prompt_tokens + budget.max_output_tokens <= allowance

    def test_anthropic_model_is_not_phased(self):
        budget = iteration_budget(
            "anthropic/claude-sonnet-4-5", provider_id="anthropic"
        )
        assert budget.phased is False
        assert budget.max_output_tokens >= 8192

    def test_unknown_model_on_groq_inherits_phased_defaults(self):
        budget = iteration_budget("future-checkpoint", provider_id="groq")
        assert budget.phased is True
        assert budget.request_allowance == int(8000 * 0.9)


class TestRollup:
    @pytest.fixture
    def loop(self, tmp_path):
        bus = EventBus()
        budget = BudgetState(max_tokens=100_000)
        agent_loop = AgentLoop(
            gateway=object(),  # unused in these tests
            tool_registry=object(),
            session_repo=None,
            event_bus=bus,
            budget=budget,
            assembler=ContextAssembler(
                budget=budget, context_window=131_072, event_bus=bus
            ),
            system_prompt=build_system_prompt(),
            model_id="openai/gpt-oss-20b",
            provider_id="groq",
        )
        agent_loop._iteration_budget = iteration_budget(
            "openai/gpt-oss-20b", provider_id="groq"
        )
        task = Message(role=Role.USER, content=[TextBlock(text="build the app")])
        agent_loop._task_message = task
        agent_loop._conversation = [task]
        for i in range(8):
            agent_loop._conversation.append(
                Message(
                    role=Role.ASSISTANT,
                    content=[TextBlock(text=f"step {i} " + ("x" * 400))],
                )
            )
            agent_loop._conversation.append(
                Message(
                    role=Role.USER,
                    content=[
                        ToolResultBlock(
                            tool_use_id=f"t{i}",
                            content="ok " * 300,
                        )
                    ],
                )
            )
        return agent_loop

    def test_should_rollup_when_carry_exceeded(self, loop):
        assert loop._should_rollup() is True

    def test_rollup_keeps_task_and_active_exchange(self, loop):
        before = len(loop._conversation)
        loop._rollup_for_next_iteration()
        assert len(loop._conversation) == 3
        assert loop._conversation[0] is loop._task_message
        assert loop._compacted_summary
        assert before > len(loop._conversation)


@pytest.mark.asyncio
async def test_synthesis_produces_final_text(tmp_path):
    from kalash.models.normalize import (
        BlockDelta,
        BlockStart,
        BlockStop,
        MessageStart,
        MessageStop,
        StopReason,
        UsageUpdate,
    )

    class _SynthProvider:
        name = "openai/gpt-oss-20b"

        async def stream(self, messages, *, system=None, tools=None, max_tokens=None, **kw):
            yield MessageStart(id="s", model=self.name)
            yield BlockStart(index=0, block_type="text")
            yield BlockDelta(index=0, delta="Final synthesized answer.")
            yield BlockStop(index=0)
            yield UsageUpdate(input_tokens=100, output_tokens=20)
            yield MessageStop(StopReason.END_TURN)

    class _Gateway:
        primary = _SynthProvider()

        async def stream(self, messages, **kwargs):
            async for event in self.primary.stream(messages, **kwargs):
                yield event

    bus = EventBus()
    budget = BudgetState(max_tokens=100_000)
    loop = AgentLoop(
        gateway=_Gateway(),
        tool_registry=object(),
        session_repo=None,
        event_bus=bus,
        budget=budget,
        assembler=ContextAssembler(
            budget=budget, context_window=131_072, event_bus=bus
        ),
        system_prompt=build_system_prompt(),
        model_id="openai/gpt-oss-20b",
        provider_id="groq",
    )
    loop._iteration_budget = iteration_budget("openai/gpt-oss-20b", provider_id="groq")
    loop._task_message = Message(
        role=Role.USER, content=[TextBlock(text="explain the codebase")]
    )
    loop._run_memory_blocks = ["<plan>read src/</plan>"]
    loop._compacted_summary = "Read main.py and found entrypoint."
    loop._response_parts = ["partial note"]

    text = await loop._synthesize_final_answer(on_text_delta=None)
    assert text == "Final synthesized answer."
