"""Offline probes for the 2026-10-04 review. No real credentials or providers."""

import asyncio
import hashlib
import json
import os
import tempfile
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

from tests.unit.test_agent_loop import make_host, make_loop

from kalash.core.budget import BudgetState, Pricing
from kalash.models.gateway import ModelGateway, RetryConfig
from kalash.models.normalize import (
    BlockDelta,
    BlockStart,
    Message,
    MessageStop,
    Role,
    StopReason,
    StreamError,
    TextBlock,
)
from kalash.models.providers.anthropic import _normalize_usage
from kalash.models.providers.openai import OpenAIProvider
from kalash.permissions.classify import classify_tool_call
from kalash.permissions.policy import DenyRule
from kalash.runtime.stream import StreamHandler
from kalash.sdk import KalashSession
from kalash.tools.base import ToolContext
from kalash.tools.fs.edit import MultiEditParams, MultiEditTool
from kalash.tools.fs.write import WriteParams, WriteTool


async def main(root):
    results = {}
    os.environ["KALASH_HOME"] = str(root / "home")

    # A session grant returns before explicit deny rules are evaluated.
    host = make_host(root)
    args = {"path": "ordinary.txt", "content": "probe"}
    risk = classify_tool_call("write", args, cwd=root, writable_roots=(root,))
    host._grants.add(host._signature(risk, args))
    host.policy.deny_rules.append(DenyRule("write", "explicit prohibition"))
    results["grant_before_deny"] = {
        "gate_refused": await host._gate(risk, args),
        "effect_executed": False,
    }

    # Record grant scope; commit and publication currently have different risk keys.
    commands = ["git commit -m probe", "git push origin topic"]
    signatures = []
    for command in commands:
        arg = {"command": command}
        r = classify_tool_call("shell", arg, cwd=root, writable_roots=(root,))
        signatures.append(
            {
                "signature": host._signature(r, arg),
                "confirmations": [x.value for x in r.confirmation_classes],
            }
        )
    results["grant_scope"] = signatures

    # Mutate between digest validation and replacement using the supported hook seam.
    path = root / "race.txt"
    path.write_text("original")

    class Hooks:
        async def dispatch(self, payload):
            if payload.event.value == "PreFileEdit":
                path.write_text("concurrent change")

    context = ToolContext(
        session_id="audit",
        run_id="audit",
        cwd=root,
        writable_roots=(root,),
        capabilities=frozenset({"fs.write"}),
        hooks=Hooks(),
    )
    result = await WriteTool().execute(
        WriteParams(
            path=str(path), content="replacement", digest=hashlib.sha256(b"original").hexdigest()
        ),
        context,
    )
    results["write_after_intervening_change"] = {"ok": result.ok, "final": path.read_text()}

    # Prevalidating all replacements against the original does not validate the sequence.
    path = root / "multi.txt"
    path.write_text("A B")
    context = replace(context, hooks=None)
    result = await MultiEditTool().execute(
        MultiEditParams(
            path=str(path),
            digest=hashlib.sha256(b"A B").hexdigest(),
            edits=[{"old_str": "A", "new_str": "B"}, {"old_str": "B", "new_str": "C"}],
        ),
        context,
    )
    results["overlapping_multi_edit"] = {"ok": result.ok, "final": path.read_text()}

    # Documented usage partitions: 100 uncached + 1000 cache read + 200 cache write.
    usage = _normalize_usage(
        {
            "input_tokens": 100,
            "output_tokens": 10,
            "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 200,
        }
    )
    budget = BudgetState()
    budget.record_usage(
        usage,
        Pricing(
            input_per_mtok=Decimal("3"),
            output_per_mtok=Decimal("15"),
            cache_read_per_mtok=Decimal("0.3"),
            cache_write_per_mtok=Decimal("3.75"),
        ),
    )
    results["cached_usage_complete"] = {
        "input_tokens": usage.input_tokens,
        "total_tokens": usage.total_tokens,
        "cost": str(budget.cost_used),
        "expected_total_tokens": 1310,
        "expected_cost": "0.001500",
    }

    # Text emitted to the UI is discarded from history on a provider error; no usage tail.
    gateway = __import__("tests.unit.test_agent_loop", fromlist=["FakeGateway"]).FakeGateway(
        [
            [
                BlockStart(index=0, block_type="text"),
                BlockDelta(index=0, delta="visible partial"),
                StreamError(error="transport lost", code="transport"),
            ]
        ]
    )
    loop = make_loop(gateway, make_host(root))
    seen = []
    result = await loop.run(
        user_message=Message(Role.USER, [TextBlock("prompt")]),
        system_identity="audit",
        on_text_delta=seen.append,
    )
    results["failed_response"] = {
        "visible": seen,
        "final": result.final_response,
        "assistant_history_entries": sum(m.role == Role.ASSISTANT for m in loop.conversation),
        "charged_tokens": loop.budget.tokens_used,
    }

    # The stream fallback branch is unreachable for async generator startup failures.
    class Provider:
        def __init__(self, name, failing=False):
            self.name, self.failing, self.calls = name, failing, 0

        async def stream(self, *_args, **_kwargs):
            self.calls += 1
            if self.failing:
                raise ValueError("invalid provider configuration")
            yield MessageStop(StopReason.END_TURN)

    primary, fallback = Provider("primary", True), Provider("fallback")
    gateway = ModelGateway(primary, fallbacks=[fallback], retry_config=RetryConfig(max_attempts=1))
    failure = None
    try:
        async for _event in gateway.stream([]):
            pass
    except Exception as exc:
        failure = type(exc).__name__
    results["stream_fallback"] = {
        "primary_calls": primary.calls,
        "fallback_calls": fallback.calls,
        "error": failure,
    }

    # Tool identity arriving after the first delta is silently ignored.
    class WireStream:
        def __aiter__(self):
            async def chunks():
                for identity, name, arg, reason in (
                    (None, None, "{", None),
                    ("call-complete", "read", '"path":"ordinary.txt"}', "tool_calls"),
                ):
                    yield NS(
                        usage=None,
                        choices=[
                            NS(
                                finish_reason=reason,
                                delta=NS(
                                    content=None,
                                    tool_calls=[
                                        NS(
                                            index=0,
                                            id=identity,
                                            function=NS(name=name, arguments=arg),
                                        )
                                    ],
                                ),
                            )
                        ],
                    )

            return chunks()

        async def close(self):
            pass

    class Completions:
        async def create(self, **kwargs):
            return WireStream()

    provider = OpenAIProvider(api_key="local-fixture")
    provider._client = NS(chat=NS(completions=Completions()))
    handler = StreamHandler()
    try:
        async for event in provider.stream([Message(Role.USER, [TextBlock("probe")])]):
            handler.feed(event)
    finally:
        provider._client = None
    result = handler.result()
    results["late_tool_identity"] = {
        "id": result.tool_calls[0].id,
        "name": result.tool_calls[0].name,
        "error": result.error.code if result.error else None,
    }

    # SDK buffers until completion and model= changes metadata without replacing the route.
    emitted, release = asyncio.Event(), asyncio.Event()

    class Agent:
        loop = NS(model_id="actual-model", max_iterations=50)
        budget = NS(cost_used=Decimal(0))
        session_id = "audit"
        routed_model = "actual-model"

        async def send(self, prompt, on_text_delta=None):
            on_text_delta("first chunk")
            emitted.set()
            await release.wait()
            return NS(error=None, final_response="first chunk", total_tokens=1)

    agent = Agent()
    session = KalashSession(agent, NS())
    stream = session.stream("probe", model="requested-model")
    first = asyncio.create_task(anext(stream))
    await emitted.wait()
    await asyncio.sleep(0)
    results["sdk_stream"] = {
        "yielded_before_completion": first.done(),
        "reported_model": agent.loop.model_id,
        "routed_model": agent.routed_model,
    }
    release.set()
    await first
    await stream.aclose()

    # Distinct Gemini calls share IDs when they have the same function name.
    from kalash.models.providers.google import GeminiProvider

    class GoogleModels:
        async def generate_content_stream(self, **kwargs):
            async def chunks():
                yield NS(
                    usage_metadata=None,
                    candidates=[
                        NS(
                            finish_reason="stop",
                            content=NS(
                                parts=[
                                    NS(
                                        text=None,
                                        thought_signature=None,
                                        function_call=NS(name="read", args={"path": value}),
                                    )
                                    for value in ("one", "two")
                                ]
                            ),
                        )
                    ],
                )

            return chunks()

    provider = GeminiProvider(api_key="local-fixture")
    provider._client = NS(aio=NS(models=GoogleModels()))
    handler = StreamHandler()
    async for event in provider.stream([Message(Role.USER, [TextBlock("probe")])]):
        handler.feed(event)
    results["google_call_ids"] = [call.id for call in handler.result().tool_calls]

    from kalash.models.providers.anthropic import AnthropicProvider

    class AnthropicStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        def __aiter__(self):
            async def chunks():
                yield NS(type="content_block_start", content_block=NS(type="thinking"))
                yield NS(
                    type="content_block_delta", delta=NS(type="thinking_delta", thinking="probe")
                )
                yield NS(
                    type="content_block_delta",
                    delta=NS(type="signature_delta", signature="signed-fixture"),
                )
                yield NS(type="content_block_stop")
                yield NS(type="message_delta", delta=NS(stop_reason="end_turn"), usage=None)

            return chunks()

    provider = AnthropicProvider(api_key="local-fixture")
    provider._client = NS(messages=NS(stream=lambda **kwargs: AnthropicStream()))
    handler = StreamHandler()
    async for event in provider.stream([Message(Role.USER, [TextBlock("probe")])]):
        handler.feed(event)
    results["thinking_signature"] = {
        "received": "signed-fixture",
        "retained": handler.result().content[0].signature,
    }

    from kalash.core.config import _apply_env, _build_config

    env_key = "KALASH_PERMISSIONS__NETWORK"
    os.environ[env_key] = "false"
    raw = {}
    try:
        _apply_env(raw)
        config = _build_config(raw, root)
        results["environment_boolean"] = {
            "value": config.permissions.network,
            "truthy": bool(config.permissions.network),
        }
    finally:
        os.environ.pop(env_key)

    from kalash.core.config import BudgetConfig, KalashConfig, MemoryConfig
    from kalash.models.normalize import ModelCapabilities, UsageUpdate
    from kalash.runtime.agent import build_agent

    class DurableProvider:
        name = "fixture/model"
        capabilities = ModelCapabilities(context_window=32768, max_output_tokens=1024)
        pricing = Pricing(input_per_mtok=Decimal(0), output_per_mtok=Decimal(0))

        async def stream(self, *_args, **_kwargs):
            yield BlockStart(index=0, block_type="text")
            yield BlockDelta(index=0, delta="done")
            from kalash.models.normalize import BlockStop

            yield BlockStop(index=0)
            yield UsageUpdate(input_tokens=100, output_tokens=10)
            yield MessageStop(StopReason.END_TURN)

        async def close(self):
            pass

    first_workspace, next_workspace = root / "first", root / "next"
    first_workspace.mkdir()
    next_workspace.mkdir()
    config = KalashConfig(
        memory=MemoryConfig(enabled=False), budget=BudgetConfig(max_tokens=100000)
    )
    agent, reason = build_agent(
        cwd=first_workspace,
        provider=DurableProvider(),
        config=config,
        extensions=False,
        interactive=False,
    )
    if agent is None:
        raise RuntimeError(reason)
    first_result = await agent.send("probe")
    if agent.budget.tokens_used != 110:
        raise RuntimeError(f"resume fixture failed before capture: {first_result}")
    await agent.loop.session_repo.append_turn(
        session_id=agent.session_id,
        role="system",
        content="durable-summary-fixture",
        content_type="compaction_summary",
    )
    resumed, reason = build_agent(
        cwd=next_workspace,
        provider=DurableProvider(),
        config=config,
        extensions=False,
        interactive=False,
        resume=True,
        session_id=agent.session_id,
    )
    if resumed is None:
        raise RuntimeError(reason)
    await resumed.prepare()
    results["resume"] = {
        "original_workspace": agent.cwd.name,
        "resumed_workspace": resumed.cwd.name,
        "prior_tokens_used": agent.budget.tokens_used,
        "resumed_tokens_used": resumed.budget.tokens_used,
        "retained_summary": resumed.loop._compacted_summary,
        "summary_in_history": any(
            "durable-summary-fixture" in getattr(block, "text", "")
            for message in resumed.history
            for block in message.content
        ),
    }
    await agent.close()
    await resumed.close()

    from kalash.scheduler.daemon import SchedulerDaemon
    from kalash.scheduler.manager import SchedulerManager

    manager = SchedulerManager()
    job = manager.add(name="audit", schedule="* * * * *", prompt="offline probe")
    manager._engine.execute_write_sync(
        "UPDATE schedules SET next_run_at = ? WHERE id = ?", ("2000-01-01T00:00:00+00:00", job.id)
    )
    daemon = SchedulerDaemon(manager._engine)
    try:
        await daemon._process_due_schedules()
        due_error = None
    except Exception as error:
        due_error = f"{type(error).__name__}: {error}"
    triggered = manager.trigger(job.id)
    sessions = manager._engine.execute_read(
        "SELECT id FROM sessions WHERE id = ?", (triggered.session_id,)
    )
    runs = manager._engine.execute_read(
        "SELECT status FROM schedule_runs WHERE schedule_id = ?", (job.id,)
    )
    results["scheduler_public_path"] = {
        "due_error": due_error,
        "trigger_created_session": bool(sessions),
        "run_status": runs[0]["status"],
        "model_called": False,
    }

    return results


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="kalash-review-") as directory:
        print(json.dumps(asyncio.run(asyncio.wait_for(main(Path(directory)), 10)), indent=2))
