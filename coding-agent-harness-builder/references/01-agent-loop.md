# 01 — Agent Loop, Messages, Scheduling, Stop Conditions

## Contents
1. State machine
2. The loop (concrete)
3. Streaming and cancellation
4. Tool-call scheduling
5. Completion gate
6. Loop and stall detection
7. Budgets and limits
8. Failure taxonomy → recovery
9. Retry policy
10. Final report contract

---

## 1. State machine

Make states explicit and emit an event on every transition.

```text
CREATED → PREPARING_CONTEXT → AWAITING_MODEL → STREAMING
   → DECIDING ─┬─ tool calls → EXECUTING_TOOLS → (append results) → PREPARING_CONTEXT
               ├─ no tool calls → COMPLETION_GATE ─┬─ pass → FINALIZING → COMPLETED
               │                                    └─ fail → (inject gate feedback) → PREPARING_CONTEXT
               ├─ ask_user tool → PAUSED_FOR_USER → (resume) → PREPARING_CONTEXT
               └─ invalid output → REPAIRING → AWAITING_MODEL | FAILED
Any state → CANCELLING → CANCELLED
Any state → FAILED (classified)   Any state → LIMIT_REACHED → FINALIZING
```

Terminal states: `COMPLETED`, `FAILED`, `CANCELLED`, `LIMIT_REACHED` (finalize with an honest partial report). `PAUSED_*` is resumable, not terminal.

Encode legal transitions as a table and assert on every transition; illegal transitions are `RuntimeInvariantViolation` (a bug).

## 2. The loop (concrete)

```python
async def run(self, run: Run) -> RunResult:
    s, ev = run.state, run.events
    await ev.emit("RunStarted", {...config_fingerprint, policy_version, model, sandbox_image...})
    try:
        while True:
            if (stop := self.limits.check(s)):            # tokens, cost, iterations, tool calls, wall clock
                return await self.finalize(run, outcome="limit", reason=stop)
            run.cancel.raise_if_set()

            self.transition(s, "PREPARING_CONTEXT")
            prompt = await self.context.build(s, self.model.caps)   # may compact; emits ContextCompacted
            await ev.emit("ModelRequested", prompt.summary())       # sizes, prefix hash, not full text
            resp = await self.call_model(run, prompt)               # retries, streaming, normalization
            s.append(resp.message); s.usage.add(resp.usage)
            await ev.emit("ModelResponded", resp.summary(), blobs=[resp.raw_ref])
            await self.checkpoint(run)

            calls = [b for b in resp.message.blocks if isinstance(b, ToolUse)]

            if resp.finish == "max_tokens" and not calls:
                s.append(self.reminders.continue_truncated()); continue

            if not calls:
                gate = await self.completion_gate(run)
                if gate.passed:
                    return await self.finalize(run, outcome="completed")
                s.append(gate.feedback_message()); continue         # model must address gate failures

            results = await self.execute_calls(run, calls)           # ALWAYS one ToolResult per ToolUse
            s.append(Message("user", tuple(results), meta={"origin": "tool_results"}))
            await self.checkpoint(run)

            if (pause := self.pending_user_question(results)):
                return await self.pause(run, pause)
    except Cancelled:
        return await self.finalize(run, outcome="cancelled")
    except HarnessError as e:                                        # classified (§8)
        return await self.handle_failure(run, e)
    # no bare except: anything else is UnknownRuntimeError, emitted, re-raised after state is persisted
```

Rules:
- `limits.check` runs *before* every model call and *before* every tool batch.
- A tool batch is atomic w.r.t. history: either all results are appended or (on crash) recovery fabricates error results for unfinished calls (see `06-state-events-replay.md` §5).
- The model is told remaining budget via a short system reminder every N iterations or when <20% remains. Enforcement never depends on it.

## 3. Streaming and cancellation

Always stream (better UX, earlier cancellation, first-token latency metric).

```python
async def call_model(self, run, prompt):
    attempt = 0
    while True:
        try:
            acc = StreamAccumulator()
            async with run.cancel.scope() as scope:                 # cancels HTTP on cancel
                async for ev in self.model.stream(prompt.messages, prompt.tools, prompt.cfg, run.cancel):
                    acc.add(ev)
                    await run.ui.emit(ev)                           # text deltas, tool-call previews
            return acc.finish()                                     # validates tool args JSON, ids unique
        except ModelError as e:
            cls = classify_model_error(e)
            await run.events.emit("ModelFailed", {"class": cls.name, "attempt": attempt, "retryable": cls.retryable})
            if not cls.retryable or attempt >= self.retry.max_attempts - 1:
                raise
            await sleep(self.retry.delay(attempt, retry_after=e.retry_after))
            attempt += 1
```

Cancellation contract:
- `CancelToken` is checked at: before model call, on each stream chunk, before/after each tool, inside long tools (poll every ≤250 ms).
- On cancel mid-stream: discard the partial assistant message *unless* it contains complete tool calls you will honor (simplest: discard).
- On cancel mid-tool: SIGTERM process group, wait 2 s, SIGKILL; return `ToolResult(is_error=True, "cancelled by user")`; persist; leave history valid so `resume` works.
- Double Ctrl-C = hard cancel (skip graceful phase).

## 4. Tool-call scheduling

Providers may return several tool calls per turn. Execute with these rules:

```python
async def execute_calls(self, run, calls):
    results: dict[str, ToolResult] = {}
    batches = partition(calls)   # consecutive read_only calls form one concurrent batch;
                                 # each non-read-only call is its own serial batch, in model order
    for batch in batches:
        if len(batch) == 1:
            results[batch[0].id] = await self.execute_one(run, batch[0])
        else:
            sem = asyncio.Semaphore(self.cfg.max_parallel_tools)
            async def guarded(c):
                async with sem: return await self.execute_one(run, c)
            for c, r in zip(batch, await asyncio.gather(*(guarded(c) for c in batch))):
                results[c.id] = r
    return [results[c.id] for c in calls]       # preserve the model's order
```

`execute_one` pipeline (each stage emits an event):

```text
1. parse/validate args against JSON Schema  → invalid → ToolResult(error: schema message + example)
2. tool.describe_effect(args)               → pure Effect
3. PreToolUse hooks                         → may deny or rewrite args
4. policy.evaluate(effect)                  → allow | deny | ask | sanitize
5. if ask: approval flow (UI/API) with timeout → deny on timeout
6. sandbox.run(tool, args, limits)          → ToolOutput (stdout/stderr/exit/duration/changed_files)
7. truncate + redact                        → ToolResult, full output → artifact
8. PostToolUse hooks                        → may append feedback (e.g. lint output)
9. file-state cache update (mtime/hash) for read-before-edit tracking
```

Concurrency hazards: two concurrent reads are fine; any write serializes against everything touching the same path. Keep a per-path async lock for mutating tools even in serial mode (hooks and subagents can interleave).

## 5. Completion gate

The loop ends only if the gate passes. Gate = list of deterministic checks configured per task/profile:

```yaml
completion_gate:
  require_no_pending_todos: true
  commands:                       # run by the HARNESS in sandbox, not by the model
    - {name: tests, cmd: "pytest -x -q", must_pass: true, timeout_s: 600, only_if_changed: ["**/*.py"]}
    - {name: lint,  cmd: "ruff check .", must_pass: false}
  forbidden_paths_unchanged: [".github/**", "pyproject.toml"]
  max_gate_retries: 3
```

On failure, inject a user-role message: failing command, exit code, last 80 lines, and instruction "Fix the failures. Do not weaken tests." After `max_gate_retries`, finalize as `LIMIT_REACHED` with the failing gate in the report. Never loop forever on a gate.

For non-coding profiles the gate can be: citations present for each claim, JSON schema validates, verifier call agrees.

## 6. Loop and stall detection

```python
class LoopDetector:
    def observe(self, call: ToolUse, result: ToolResult):
        key = (call.name, canonical_json(call.args), hash_result(result))
        self.window.append(key)                    # last 20
        if self.window.count(key) >= 3: return "repeat"
        if self.is_alternating(self.window):       # A,B,A,B,A,B
            return "oscillation"
        if self.no_progress_steps >= 12:           # no file changed, no new file read, no test status change
            return "stall"
```

Escalation ladder: (1) inject a reminder naming the repeated action and demanding a different approach; (2) force a plan-mode turn (tools disabled, ask for a revised plan); (3) escalate to stronger model if routing is configured; (4) terminate with `LIMIT_REACHED` and a report of what was tried. Emit `LoopDetected` with the pattern at each rung.

## 7. Budgets and limits

```python
@dataclass(frozen=True)
class Budget:
    max_iterations: int = 60
    max_tool_calls: int = 200
    max_input_tokens_total: int = 2_000_000    # cumulative billed input incl. cache reads
    max_output_tokens_total: int = 200_000
    max_cost_usd: float = 5.0
    max_wall_seconds: int = 1800
    max_subagent_depth: int = 2
    max_parallel_tools: int = 4
    max_tool_result_bytes: int = 30_000
    max_consecutive_tool_errors: int = 6
```

- Cost: compute from `Usage` × price table in config; separate cache-read, cache-write, input, output, reasoning. Reconcile weekly against provider invoices; alert at >2% drift.
- Check **before** an expensive step using worst-case estimate (`prompt_tokens * in_price + max_output * out_price`) so one call cannot blow far past the cap.
- Wall-clock is a deadline, not a counter: pass `deadline` into every tool and model call.
- Subagent budgets are carved from the parent's remaining budget.

## 8. Failure taxonomy → recovery

| Class | Examples | Retry? | Recovery |
|---|---|---|---|
| `TransientModelError` | 429, 5xx, timeout, connection reset, `overloaded` | yes, backoff+jitter, honor `Retry-After` | after N: fall back to secondary model if configured; else checkpoint + FAILED(resumable) |
| `PermanentModelError` | 400 schema, auth, unknown model | no | FAILED; `doctor` hint |
| `ContextOverflow` | provider says prompt too long | no (same prompt) | force compaction level+1, rebuild, retry once |
| `MalformedModelOutput` | invalid tool JSON, unknown tool name, duplicate ids | no (re-ask) | return `ToolResult` errors listing valid tools/schema; after 3 consecutive → repair prompt → fail |
| `ToolTimeout` | deadline exceeded | model decides | kill group, return partial output + "timed out after Ns; consider running in background / narrowing" |
| `ToolPermissionDenied` | policy deny | no | tell model why and what is allowed; do **not** suggest workarounds that evade policy |
| `SandboxFailure` | bwrap missing, container died, OOM | once if infra | recreate sandbox from snapshot; if repeated → FAILED(infra) |
| `BudgetExceeded` | any limit | no | finalize with report |
| `PolicyViolation` | attempted escape, canary touched | no | terminate run, security event, alert |
| `UserCancellation` | Ctrl-C, API cancel | no | CANCELLED |
| `RuntimeInvariantViolation` | illegal transition, orphan result | no | persist state, FAILED, page owner |
| `UnknownRuntimeError` | anything else | no | persist, emit stack, re-raise |

## 9. Retry policy

```python
@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base: float = 1.0
    cap: float = 30.0
    jitter: Literal["full", "equal"] = "full"
    retry_after_cap: float = 120.0

    def delay(self, attempt, retry_after=None):
        exp = min(self.cap, self.base * 2 ** attempt)
        d = random.uniform(0, exp) if self.jitter == "full" else exp / 2 + random.uniform(0, exp / 2)
        return min(max(d, retry_after or 0), self.retry_after_cap)
```

Add a **circuit breaker per provider** (open after k failures in window, half-open probe) and a global concurrency limiter / token-bucket per API key so parallel runs cannot self-DDoS the provider.

Streaming retries: if the stream died after partial content, discard and retry from scratch (don't try to splice) unless the provider supports resumption.

## 10. Final report contract

Generated by the harness from state, then optionally polished by the model; the structured part is authoritative.

```json
{
  "outcome": "completed|limit|failed|cancelled",
  "summary": "…",
  "changes": [{"path": "src/x.py", "kind": "modified", "lines_added": 12, "lines_removed": 3}],
  "verification": [{"cmd": "pytest -q", "exit": 0, "at_event": 412}],
  "unresolved": ["…"],
  "budget": {"iterations": 23, "tool_calls": 61, "cost_usd": 0.84, "wall_s": 312},
  "artifacts": ["patches/final.diff", "logs/run.jsonl"]
}
```

Rule: "tests pass" may only be claimed in the report if a verification entry with exit 0 exists *after the last file mutation*. The harness checks this; the model's prose is not trusted.
