# Agent Harness — Core Loop: Comprehensive Study Guide

> **Repo:** Kalash Code (`/home/shivam/Kalash Code`)
> **Scope:** Agent Harness core loop — loop type, tool calling, verifier, determinism
> **Method:** every file below was read in full; all `file:line` references are real.
> **Audience:** you, studying as a reviewer. Starts with ELI5, ends with exact code coordinates.
> **How to use:** read §0–§2 for the exam answer, §3–§10 for the deep dive, §11–§13 for
> "base / exactly-where / connections", §14 for "what lacks", §15 for self-test Q&A.

---

## Table of contents

- [0. The 60-second answer](#0-the-60-second-answer)
- [1. Base of the current implementation (the philosophy)](#1-base-of-the-current-implementation-the-philosophy)
- [2. What loop type is this, really?](#2-what-loop-type-is-this-really)
- [3. Max steps, budgets, and every stop condition](#3-max-steps-budgets-and-every-stop-condition)
- [4. Compaction and summarization on context overflow (4 mechanisms, not 1)](#4-compaction-and-summarization-on-context-overflow-4-mechanisms-not-1)
- [5. Prompt construction (what the model actually sees)](#5-prompt-construction-what-the-model-actually-sees)
- [6. Tool schema (how tools are described to the model)](#6-tool-schema-how-tools-are-described-to-the-model)
- [7. Tool calling — correctness path end-to-end](#7-tool-calling--correctness-path-end-to-end)
- [8. Parallel calls](#8-parallel-calls)
- [9. Retries (two layers)](#9-retries-two-layers)
- [10. Timeouts and truncated-output handling](#10-timeouts-and-truncated-output-handling)
- [11. Verifier — does it run tests/lint/typecheck and self-correct?](#11-verifier--does-it-run-testslinttypecheck-and-self-correct)
- [12. Determinism — same prompt + seed = stable diff?](#12-determinism--same-prompt--seed--stable-diff)
- [13. Exactly what is implemented, at which file (inventory)](#13-exactly-what-is-implemented-at-which-file-inventory)
- [14. Connections — who calls whom (wiring map)](#14-connections--who-calls-whom-wiring-map)
- [15. What lacks (gaps, ordered by importance)](#15-what-lacks-gaps-ordered-by-importance)
- [16. Study kit — analogies, trace exercise, Q&A, glossary](#16-study-kit--analogies-trace-exercise-qa-glossary)

---

## 0. The 60-second answer

| Your question | Answer in one line | Key file:line |
|---|---|---|
| Loop type: single ReAct vs planner+executor vs sub-agents? | **Single synchronous ReAct loop** (`assemble → stream → tools → repeat`); "planning" is a prompt section + a `todo` tool, not a separate planner agent; sub-agents exist but only as a **`task` tool** the single loop calls | `src/kalash/runtime/loop.py:195-376` (`run`), `src/kalash/runtime/prompt.py:264-313` (`_PLANNING`), `src/kalash/tools/task.py:66-220` |
| Max steps? | `max_iterations=50` per `AgentLoop.run`, `max_turns=100`, `max_tool_calls=500`, `max_tokens=500_000` budget ceilings; child agents default `max_turns=25` | `src/kalash/runtime/loop.py:139`, `src/kalash/runtime/agent.py:189`, `src/kalash/core/budget.py:58-75` |
| Stop condition? | `NO_TOOL_CALLS` (model answered), `BUDGET_EXHAUSTED`, `MAX_ITERATIONS`, `USER_INTERRUPT`, `ERROR`; `STOP_HOOK` is **declared but never emitted** (dead) | `src/kalash/runtime/loop.py:70-78`, `257-368` |
| Compaction/summarization on overflow? | **Yes, 4 layered mechanisms:** (1) pressure ladder L0–L7, (2) shrink tiers 0–5, (3) `Compactor` LLM summarization at 0.82 fill, (4) phased rollup + synthesis for TPM-bound models | `src/kalash/runtime/context.py:26-40`, `src/kalash/runtime/shrink.py:15-24`, `src/kalash/runtime/compaction.py:24`, `src/kalash/runtime/loop.py:660-688,714-785` |
| Tool calling correctness? | Strong: classify → pre-hooks → policy gate → approval → dispatch with Pydantic validation → envelope (never raises) → render → defer; malformed JSON repaired in 4 layers; truncated tool calls trigger output-budget doubling | `src/kalash/runtime/toolhost.py:338-395`, `src/kalash/tools/registry.py:127-167`, `src/kalash/runtime/stream.py:33-88`, `src/kalash/runtime/loop.py:305-325` |
| Parallel calls? | **Half-parallel:** READ tools run concurrently (semaphore 5); WRITE/EXEC serialize on an async lock; providers can *emit* parallel calls, the loop *executes* reads in parallel | `src/kalash/runtime/loop.py:938-1004` |
| Retries? | **Two layers:** gateway retries transient errors (6 attempts, exp-backoff+jitter, Retry-After, 180 s budget, fallback providers, proactive TPM pacer); loop retries with shrink tiers / output-cap fix / invalid-tool re-prompt | `src/kalash/models/gateway.py:260-472`, `src/kalash/runtime/loop.py:444-560` |
| Timeout? | Per-tool timeouts exist but are **not centrally enforced**; only `shell` (120 s + SIGTERM→SIGKILL), `task` (300 s `wait_for`), `search`, `web/fetch` (httpx 30 s) actually enforce; gateway has a retry *budget* (180 s), not a per-request deadline | `src/kalash/tools/shell.py:39,481-545`, `src/kalash/tools/task.py:171-189`, `src/kalash/tools/search.py:165-173`, `src/kalash/models/gateway.py:260-268` |
| Truncated output handling? | **Four layers:** per-stream byte caps → scratchpad deferral (>6 KB → `#xN` ref) → short-history truncation (12 k chars) → truncated-tool-call output-budget retry | `src/kalash/tools/shell.py:38`, `src/kalash/runtime/toolhost.py:81-88,671-698`, `src/kalash/runtime/loop.py:851-871,305-325` |
| Verifier: tests/lint/typecheck + self-correct, or blind emit? | **Blind emit + prompt-level exhortation.** No code invokes pytest/ruff/mypy automatically; "verify" is a system-prompt section (`_VERIFICATION`) the model *may* obey via `shell`. Self-correction is emergent (tool error fed back as text), not enforced | `src/kalash/runtime/prompt.py:508-531`, `src/kalash/runtime/loop.py:338-346,1016-1063` |
| Determinism: seed/temperature/top-p? | **Not deterministic.** `temperature` exists in config + provider signatures but **no caller ever passes it** (dead knob); `top_p`/`seed` appear nowhere in `src/` | `src/kalash/core/config.py:50,241`, `src/kalash/models/providers/anthropic.py:269,309-310`, `src/kalash/runtime/loop.py:896-932` |

**ELI5 of the whole harness:** the agent is a stubborn intern in a loop. Each lap it (1) packs a
suitcase (context assembly), (2) phones the professor (LLM stream), (3) does what the professor
scribbled on the note (tool calls), (4) reads the results out loud back to the professor (tool
results appended), and repeats until the professor stops assigning chores (no tool calls), the
wallet is empty (budget), or the fire alarm rings (cancel/error). A bouncer (permission gate)
checks every chore before it runs. A librarian (compaction) summarizes old notebooks when the
suitcase overflows. Nobody grades the final work automatically (no verifier) — the intern is just
*told* "good interns test their work" in the employee handbook (system prompt).

---

## 1. Base of the current implementation (the philosophy)

Before file coordinates, internalize the five design bets the codebase makes. Everything else is
a consequence of these.

### 1.1 "The provider is a scarce, metered, fallible resource"

Most toy agents do `await client.complete(messages, max_tokens=8192, tools=all_tools)` and hope.
This codebase assumes three hard failures *will* happen (stated verbatim in
`src/kalash/models/limits.py:1-22`):

1. `max_completion_tokens must be <= 4096` — output reservation exceeds model cap.
2. `TPM Limit 8000, Requested 11220` — Groq bills `prompt + max_tokens` against tokens-per-minute,
   so prefix + reservation exceeds the per-minute budget before the user types anything.
3. `'tool calling' is not supported with this model` — tools sent to a model that rejects them.

Consequences you will see everywhere:

- `models/limits.py` — static table + observed-error cache + provider defaults; every request is
  **preflighted** (`tpm_allowance`, `fit_request_size`, `resolve_output_tokens`, `input_budget`).
- `models/gateway.py` — sliding-window TPM **pacer** sleeps *before* breaching 429
  (`SlidingWindowRatePacer.pace`, `gateway.py:147-213`), plus retry-with-backoff and provider
  fallbacks.
- `runtime/shrink.py` + `runtime/toolhost.py:262-321` + `tools/schema.py:118-169` — the tool set
  itself **shrinks** (full → standard → minimal) to fit the allowance instead of failing.
- `runtime/prompt.py:879-951` — even the system prompt has **two tiers** (full ~3.9 k tokens vs
  compact) chosen by `_fit_prompt` (`runtime/agent.py:363-397`).

### 1.2 "Nothing reaches a tool without passing the gate"

The permission system is not a wrapper; it is the only path. `ToolHost.execute`
(`runtime/toolhost.py:338-395`) is the single choke point and it always runs, in order:
`classify_tool_call` → `_run_pre_hooks` → `_gate` (plan-mode refusal → read-only-network
fast-path → policy stages → approval prompt) → `registry.dispatch`. There is no backdoor
"call the registry directly" path used by the loop — `AgentLoop` holds a `ToolHostProtocol`, not
a `ToolRegistry` (`runtime/loop.py:115-121`).

### 1.3 "Tools never raise; the model only ever sees text"

`ToolEnvelope` (`tools/base.py:113-166`) is the universal return type. `ToolRegistry.dispatch`
(`tools/registry.py:127-167`) catches unknown-tool, invalid-args, and unexpected-exception cases
and converts each to `ToolEnvelope.fail(...)`. `ToolHost._render` (`toolhost.py:645-669`) converts
the envelope to the string the model reads (`ERROR <code>: <message>` + content + remediation).
`AgentLoop._execute_tool_with_hooks` (`loop.py:1016-1063`) catches anything left and returns a
`ToolResultBlock(is_error=True)`. Net effect: **infrastructure failures become chat text** — a
deliberate choice with a real downside (discussed in §15, gap G-06).

### 1.4 "Cache stability is a correctness property, not an optimization"

- Tool order is fixed (`tools/builtins.py:37-65`, comment "must be byte-stable").
- Context slot order is fixed 0–8 (`runtime/context.py:47-58`, "changing slot order invalidates
  cache prefixes").
- Anthropic prompt caching breakpoints are set explicitly (system + whole tool block,
  `models/providers/anthropic.py:286-307`).
- `minify_schema` (`tools/schema.py:71-89`) strips `title`/`default`/`additionalProperties`
  because Pydantic's schema verbosity cost 3,888 tokens across 15 tools for zero model benefit.

### 1.5 "Durability over cleverness"

- Atomic file writes (`tempfile + fsync + os.replace`, `tools/fs.py`).
- Append-only compaction rows, never UPDATE (`runtime/compaction.py:279-294`, I-021).
- Dangling `tool_use` blocks dropped on resume so a resumed session cannot poison the provider
  (`runtime/serialize.py:190-217`).
- Every `tool_use` gets a matching `tool_result` even on cancellation
  (`runtime/loop.py:988-1003`).

---

## 2. What loop type is this, really?

### 2.1 Verdict: single ReAct loop (with prompt-planning + tool-subagents, not agent-subagents)

There are three architectures students confuse. Here is the test and the result:

| Architecture | Definition | Present? |
|---|---|---|
| **Single ReAct loop** | One loop: Reason (LLM) → Act (tools) → Observe (results) → repeat, shared conversation | **YES — this is the whole system** |
| **Planner + executor** | Separate planner agent writes a plan; separate executor agent(s) carry it out, often different models/prompts | **NO.** There is a `_PLANNING` prompt section (`prompt.py:264-313`) *telling* the single agent to use the `todo` tool first, and a `TodoTool`, but no second agent, no plan object passed between agents. "Plan mode" (`agent.py:73-82`, `toolhost.py:514-522`) is a *capability filter*, not an agent role |
| **Sub-agent swarm / supervisor** | Supervisor spawns child agents with their own loops, aggregates | **Half.** `TaskTool` (`tools/task.py`) + `run_isolated` (`agent.py:422-496`) + `SubagentRunner`/`TeamPattern` (`orchestration/subagent.py`, `orchestration/team.py`) exist, but from the loop's perspective a subagent is **just another tool call**: the parent loop emits a `task` ToolUseBlock, `ToolHost` runs it, the child's transcript never enters the parent's context (only the summary string returns, `task.py:204-220`). There is no supervisor planner, no blackboard coordination in the hot path (`orchestration/blackboard.py` exists but the loop never touches it) |

The loop's own docstring says it plainly (`loop.py:1-5`):

```python
"""The agent turn loop.
Main loop: assemble → stream → parse tool calls → execute → append → repeat.
Terminates on: no tool calls, budget exhausted, user interrupt, or Stop hook.
"""
```

And the prompt teaches the model the same loop (`prompt.py:300-313`):

```
## Execution loop (ReAct)
1. **Reason** — think about what needs to happen and why (internally).
2. **Act** — use a tool (read, search, edit, shell, etc.).
3. **Observe** — examine the tool result.
4. **Repeat** — continue until the task is complete.
Never skip the Observe step. ...
```

### 2.2 The loop, line by line (`AgentLoop.run`, `loop.py:195-376`)

This is the function to memorize. Pseudocode with exact coordinates:

```
run(user_message, system_identity, skills_catalog, kalash_md_chain,
    memory_blocks, compacted_summary, recent_turns, environment, on_text_delta)
  [loop.py:223-240]  reset per-run state: _iteration=0, _response_parts=[],
                      _retried_output_cap=False, _shrink_tier=NORMAL,
                      _iteration_budget=iteration_budget(model_id, provider_id),
                      _task_message=user_message, _conversation=[*recent_turns, user_message]
  [loop.py:242-254]  dispatch SESSION_START hook (failure only logged)
  [loop.py:257]      WHILE _running AND _iteration < max_iterations:
  [loop.py:258]        _iteration += 1
  [loop.py:261-266]  ceiling = budget.check_ceiling() → if hit: _finish(BUDGET_EXHAUSTED)
  [loop.py:268-275]  stream_result = _stream_turn(...)   # assemble + preflight + stream + retry
  [loop.py:277-283]  if stream error: _finish(ERROR, explain(error))
  [loop.py:285-293]  budget.turns_used += 1; budget.record_usage(usage)
  [loop.py:295-303]  stash text; append assistant Message; _persist_turn(assistant, usage)
  [loop.py:306-325]  IF stop==MAX_TOKENS AND tool input has {"_raw": ...} (truncated JSON):
                        max_output_tokens = min(2x, 16384); pop assistant msg; CONTINUE (retry turn)
  [loop.py:327-336]  IF no tool calls: emit TURN_COMPLETE; _finish(NO_TOOL_CALLS, final=text)
  [loop.py:338-346]  tool_results = _execute_tools(tool_calls); _had_tool_work=True
                      append USER message of ToolResultBlocks; _persist_tool_results(...)
  [loop.py:348-349]  IF _should_rollup(): _rollup_for_next_iteration()   # phased TPM models only
  [loop.py:351-352]  IF cancelled: _finish(USER_INTERRUPT)
  [loop.py:354-368]  AFTER loop: IF phased AND _had_tool_work:
                        final = _synthesize_final_answer()  # extra no-tools pass
                        IF final: _finish(MAX_ITERATIONS, final, synthesized=True)
                      ELSE _finish(MAX_ITERATIONS)
  [loop.py:370-376]  EXCEPT CancelledError → USER_INTERRUPT; EXCEPT Exception → ERROR; FINALLY _running=False
```

Three subtleties students miss:

1. **`_iteration` counts model turns, not tool calls.** One iteration can contain N tool calls
   (all executed in `_execute_tools` before the next stream). `budget.turns_used` is also
   incremented once per iteration (`loop.py:285`), and `budget.tool_calls_used` once per tool
   (`loop.py:1036`). They are different ceilings (`budget.py:87-99`).
2. **`_finish` concatenates *all* text parts** (`loop.py:398-400`: `"\n\n".join(_response_parts)`),
   so `final_response` is the whole turn's narration, not just the last message.
3. **History ownership is split:** `AgentLoop.run` is per-request and stateless across turns;
   `Agent` (`agent.py:56-167`) owns continuity — `send()` passes `recent_turns=self.history` in
   and writes `self.history = loop.conversation` out (`agent.py:136-149`). The loop's
   `conversation` property returns a copy (`loop.py:882-891`).

### 2.3 Two "gears": normal vs phased iteration

`iteration_budget()` (`models/limits.py:305-344`) puts the loop in one of two gears:

| | Normal gear (`phased=False`) | Phased gear (`phased=True`) |
|---|---|---|
| When | Model has no TPM ceiling (Anthropic, OpenAI, Gemini) | Model has a TPM ceiling (Groq 8 k, Together 6 k, …) |
| Per-iteration output | `min(model_cap, 8192)` | `min(model_cap, allowance*0.25, 2048)` — a fixed slice |
| Carry limit | `max(4096, window*0.55)` — rollup almost never fires | `max(512, prompt*0.45)` — rollup fires often |
| End of work | Last tool-using turn's text *is* the answer | Extra **synthesis pass** (`_synthesize_final_answer`, `loop.py:714-785`): a no-tools request "You completed work across several tool-using steps. Write one complete, user-facing final answer…" whose text becomes the answer with `synthesized=True` |
| Test | `test_anthropic_model_is_not_phased` | `test_groq_model_gets_phased_fixed_envelope`, `test_synthesis_produces_final_text` (`tests/unit/test_iteration_phases.py`) |

Mental model: normal gear is "one long meeting"; phased gear is "many 15-minute standups plus a
final write-up memo." Durable state between standups lives in `_compacted_summary`,
`_run_memory_blocks`, and the scratchpad/plan — the conversation itself is cut down to
`[task, last_exchange]` by `_rollup_for_next_iteration` (`loop.py:666-688`).

---

## 3. Max steps, budgets, and every stop condition

### 3.1 Every ceiling in one table

| Ceiling | Default | Where defined | Where checked | What happens |
|---|---|---|---|---|
| Loop iterations | `50` (`AgentLoop.max_iterations`) | `loop.py:139`; wired by `build_agent(..., max_iterations=50)` at `agent.py:189` | `loop.py:257` `while _running and _iteration < max_iterations` | Falls through to synthesis-or-`MAX_ITERATIONS` (`loop.py:354-368`) |
| Child max turns | `25` (`run_isolated(max_turns=25)`), `TaskParams.max_turns=50` cap 200 | `agent.py:428`, `tools/task.py:45-50` | Same `while` in child's own loop; `budget.max_turns` | Child returns `failed`/truncated; parent sees error text |
| Session budget tokens | `500_000` | `core/budget.py:62`; `agent.py:298-300` | `loop.py:261` `budget.check_ceiling()` each iteration | `_finish(BUDGET_EXHAUSTED, "budget exhausted: tokens")` |
| Session budget cost | `$2.00` Decimal | `core/budget.py:63` | Same | Same, `cost` dimension |
| Session wallclock | `3600 s` | `core/budget.py:64` | Same `check_ceiling()` — **but see gap G-03**: nothing ever increments `wallclock_used_s` in the loop path | Effectively unenforced |
| Session turns | `100` | `core/budget.py:65` | Same; incremented at `loop.py:285` | `BUDGET_EXHAUSTED (turns)` |
| Session tool calls | `500` | `core/budget.py:66` | Same; incremented at `loop.py:1036` | `BUDGET_EXHAUSTED (tool_calls)` |
| Spawn depth | `3` | `core/budget.py:67`; `toolhost.py:203`; `task.py` via `ctx` | `ToolContext.can_spawn` (`tools/base.py:102-105`), checked at `task.py:156-164`; separately `SubagentRunner.spawn` depth check (`orchestration/subagent.py:80-85`) | `KALASH_SUBAGENT_DEPTH_EXCEEDED` envelope → model reads "Do this work directly…" |
| Stream retry attempts (gateway) | `6`, budget `180 s`, backoff `1s*2^attempt` + 25% jitter, cap 30 s | `models/gateway.py:260-274` (`RetryConfig`) | `_stream_with_retry` (`gateway.py:338-392`), `_attempt_with_retry` (`gateway.py:394-472`) | Falls to next provider / `StreamError(all_failed)` |
| Stream retry attempts (loop) | `6` (`for stream_attempt in range(6)`) | `loop.py:444` | `_stream_turn` (`loop.py:444-560`) | Returns last `StreamResult` with error → `_finish(ERROR)` |
| Shrink tiers | 6 (`NORMAL … DROP_PROJECT_CONTEXT`) | `runtime/shrink.py:15-24` | `_assemble_for_tier` loop (`loop.py:602-653`) + preflight (`loop.py:460-472`) + error retry (`loop.py:535-545`) | Progressively drops tools → compact prompt → memory → project context |
| Output-cap retry | Once per run (`_retried_output_cap`) | `loop.py:151,521-533` | `_stream_turn` on `adjust_max_output` diagnosis | `max_output_tokens` replaced; `ConstraintCache.record_max_output` |
| Truncated-tool-call retry | Double until `16_384` | `loop.py:320-322` | `run` on `MAX_TOKENS + "_raw"` | Pop assistant msg, `continue` (re-stream same turn) |
| Compaction trigger | `0.82` fill | `runtime/compaction.py:24` | `_ensure_context_fits` (`loop.py:787-831`) + `_emit_context_usage` (`loop.py:410-426`) | Summarize / trim (see §4) |
| Subagent wallclock | `300 s` (`TaskParams.timeout_s`, `SubagentConfig.max_wallclock_s=300`) | `tools/task.py:39-44`, `orchestration/subagent.py:56` | `asyncio.wait_for(..., timeout=args.timeout_s)` at `task.py:171-182` | `KALASH_TOOL_TIMEOUT` envelope |
| Shell command | `120 s` default, max `1800 s` | `tools/shell.py:39,189` | `_run_with_timeout` (`shell.py:481-517`) | `KALASH_TOOL_TIMEOUT` envelope |
| Fetch | `30 s` | `tools/web.py:34` | httpx request timeout | `KALASH_TOOL_TIMEOUT` envelope (`web.py:187`) |

### 3.2 Termination reasons — the full truth table

`TerminationReason` (`loop.py:70-78`): six values, **five reachable**:

| Reason | Value | Produced where | Meaning for the caller |
|---|---|---|---|
| `NO_TOOL_CALLS` | `no_tool_calls` | `loop.py:333-336` | Happy path: model answered with text only. `final_response` = that text. TUI/headless treat as success |
| `BUDGET_EXHAUSTED` | `budget_exhausted` | `loop.py:263-266` | Any `BudgetState.check_ceiling()` dimension hit. `error="budget exhausted: <dim>"`. Headless exit code `EXIT_BUDGET_EXHAUSTED=4` (`cli/_pipe.py`) |
| `MAX_ITERATIONS` | `max_iterations` | `loop.py:362-368` | Loop used all 50 turns still calling tools. If phased + did work, a synthesis pass runs first and the reason is still `MAX_ITERATIONS` but with `synthesized=True` + final text. Otherwise `final_response` = concatenated narration, no dedicated closing answer |
| `USER_INTERRUPT` | `user_interrupt` | `loop.py:352` (cooperative cancel flag), `loop.py:371` (`CancelledError`) | `cancel()` (`loop.py:873-876`) sets `_cancelled` + `cancel_event`; in-flight tools polling `cancel_event` abort (`shell.py:497-499,461-463`); unanswered tool calls get "Not executed" placeholders (`loop.py:996-1002`) so the session stays resumable |
| `ERROR` | `error` | `loop.py:283` (stream error after retries), `loop.py:374` (unexpected exception) | `error=explain(...)` human-readable diagnosis; headless non-zero exit |
| `STOP_HOOK` | `stop_hook` | **Nowhere.** `grep STOP_HOOK src/` hits only the enum + docstring. No hook dispatcher in the loop returns "stop", `hooks/runner.py` results are only checked for PreToolUse blocks | **Dead code.** A reviewer should flag: either wire Stop-hook evaluation at the bottom of each iteration or delete the variant (see §15 G-02) |

`LoopResult` (`loop.py:86-96`) carries `termination_reason, iterations, total_tokens
(=budget.tokens_used), final_response, error, synthesized`.

### 3.3 Cancellation is cooperative, not preemptive — read this carefully

`cancel()` does **not** kill anything (`loop.py:873-876`):

```python
def cancel(self) -> None:
    self._cancelled = True
    self._cancel_event.set()
```

The loop notices only at three points: before the next iteration's budget check (via
`while _running` + explicit `if self._cancelled` after tools, `loop.py:351`), and
`_execute_tools` short-circuits *queued but unstarted* serialized tools (`loop.py:978-984`).
Already-running tools must poll `ctx.cancel_event` themselves — `shell.py` does
(`_collect_stream` + `_run_with_timeout`), most other tools do not. The model-stream itself is
not cancelled mid-chunk; the handler drains what the gateway yields. This is the standard
cooperative-cancellation design: safe (no torn writes — `WriteTool` is atomic) but not instant.

---

## 4. Compaction and summarization on context overflow (4 mechanisms, not 1)

Newcomers expect "compaction = one summarizer." This codebase has **four nested defenses**,
innermost first. Learn them in firing order inside a single `_stream_turn`:

```
_stream_turn (loop.py:428-560)
 └─ _assemble_for_tier(tier) (loop.py:574-655)
 │    ├─ toolhost.schemas(...)            # MECHANISM 2 (shrink tier → tool profile)
 │    ├─ _ensure_context_fits(...)        # MECHANISM 3 (compaction / trim)
 │    ├─ assembler.assemble(...)          # MECHANISM 1 (pressure ladder)
 │    └─ fit_request_size(...)            # TPM preflight; may escalate tier and loop
 └─ tpm_allowance preflight               # may escalate tier and `continue`
 └─ gateway.stream(...)                   # with gateway retry (see §9)
```

Plus the outer phased-rollup loop (MECHANISM 4) between iterations.

### 4.1 Mechanism 1 — ContextAssembler pressure ladder (every turn, no LLM call)

`src/kalash/runtime/context.py:26-40,200-230`:

- `_compute_pressure` maps fill ratio → `L0…L7` via thresholds
  `(0.60, 0.70, 0.74, 0.78, 0.82, 0.92)` (`context.py:26`). Input is `estimated_prompt_tokens /
  context_window` when the loop supplies it, else session budget usage.
- `_allocate_budgets` assigns slot percentages; under pressure the skills catalog goes to 0%
  at ≥L6, memory 8%→3% at ≥L4, recent turns get trimmed front-first to their allocation at ≥L3
  (`context.py:212-230,294-309`).
- Tool schemas abbreviate to `name: description[:80]` at ≥L2 (`context.py:246-255`); memory slot
  empties at ≥L4 (`context.py:278-286`); skills empty at ≥L6 (`context.py:257-264`).
- Slot order is fixed 0–8 (`context.py:47-58`); cache breakpoints are emitted as literal
  `<cache_breakpoint/>` system messages after slots 2 and 4 (`context.py:62,323-339`) and later
  stripped by `split_system` (`serialize.py:220-242`) — a hack the providers tolerate because
  Anthropic gets real `cache_control` markers separately (`providers/anthropic.py:286-307`).

Cost: zero model calls, purely extractive. Limitation: it *drops* content; nothing is summarized.

### 4.2 Mechanism 2 — Shrink tiers (TPM preflight + error-driven escalation)

`src/kalash/runtime/shrink.py:15-45` + `loop.py:602-653,460-472,535-545`:

| Tier | `force_tool_profile` | compact prompt? | drop memory? | drop project ctx? |
|---|---|---|---|---|
| 0 NORMAL | auto | no | no | no |
| 1 STANDARD_TOOLS | `standard` | no | no | no |
| 2 MINIMAL_TOOLS | `minimal` | no | no | no |
| 3 COMPACT_PROMPT | `minimal` | **yes** | no | no |
| 4 DROP_MEMORY | `minimal` | yes | **yes** | no |
| 5 DROP_PROJECT_CONTEXT | `minimal` | yes | yes | **yes** |

Firing conditions: (a) preflight `prompt_tokens + output_tokens > tpm_allowance` before streaming
(`loop.py:460-472`); (b) `fit_request_size` reports `needs_shrink` inside `_assemble_for_tier`
(`loop.py:641-652`); (c) post-error `diagnose(...).reduce_input` (`loop.py:535-545`). Each step
logs (`"preflight over TPM allowance…"`, `"retrying after throughput error at tier …"`).
`_assemble_for_tier` loops at most `DROP_PROJECT_CONTEXT - tier + 2` times, so it always
terminates. The chosen tier persists in `self._shrink_tier` and is reported in the TURN_START
event (`loop.py:479-504`) — visible in `/status` and headless JSON.

### 4.3 Mechanism 3 — `_ensure_context_fits` + `Compactor` (the "real" compaction)

`loop.py:787-831` + `runtime/compaction.py` (331 lines):

```
ratio = estimate(system + conversation + tools) / assembler.context_window
if ratio < 0.82: return existing summary (no-op)
if len(conversation) <= 3:
    summary = extractive(first/last lines, 400 chars each, loop.py:833-848)
    conversation = trim tool results > 12_000 chars (loop.py:850-871)
else:
    head, tail = conversation[:-3], conversation[-3:]
    if session_repo: summary = await Compactor(...).compact(session_id, head)  # LLM call
    else:            summary = extractive(head)
    conversation = tail
```

`Compactor.compact` (`compaction.py:93-143`): emits COMPACT_START → `_extract_preserved`
(heuristic file paths + TODOs, `compaction.py:222-246` — note `decisions`,
`failed_approaches`, `user_constraints` are **never populated**, see gap G-09) →
`_hierarchical_summarize` (chunk by 20 msgs, summarize each via `gateway.complete` with
`COMPACTION_SYSTEM_PROMPT`, recurse to depth 3, `compaction.py:149-216`) → `_build_final_summary`
→ `_persist_summary` as a **new** `system/compaction_summary` row (never UPDATE, I-021) →
COMPACT_COMPLETE. On any exception: `_fallback_summary` (first 3 + last 3 messages,
`compaction.py:324-331`).

Key facts for review answers:

- The summarizer call goes through `gateway.complete` — full retry/pacer/fallback stack, and it
  **consumes budget** (`budget.record_usage` is *not* called for it explicitly — the gateway
  accumulates internally; loop-level `tokens_used` does not see it — gap G-10).
- Threshold constant `COMPACTION_THRESHOLD = 0.82` is shared by `_ensure_context_fits` and
  `_emit_context_usage` (which emits BUDGET_WARNING / SOFT_LIMIT / EXCEEDED at 0.82 / 0.95).
- With `session_repo=None` (tests, `run_isolated` children with `persist=False`), compaction is
  always extractive — no LLM call. That is why `test_iteration_phases` rollup tests pass without
  a gateway.

### 4.4 Mechanism 4 — Phased rollup + synthesis (TPM-bound models only)

`loop.py:660-688` (`_should_rollup`, `_rollup_for_next_iteration`) + `loop.py:714-785`
(`_synthesize_final_answer`):

- `_should_rollup` fires only when `iteration_budget.phased` **and**
  `estimate(conversation) > max_carry_tokens` (e.g. ~45% of a ~7 k prompt allowance ≈ ~3 k tokens
  on Groq — very frequent).
- `_rollup_for_next_iteration` folds everything except `[task_anchor, last_2_msgs]` into
  `_compacted_summary` via the **extractive** summarizer (no LLM call — deliberately cheap at
  this frequency), resets shrink tier to NORMAL, and logs.
- `_build_work_context` (`loop.py:699-712`) assembles Prior steps + Working memory + last 5
  assistant notes for the synthesis prompt; the synthesis stream uses `tools=[]` and the
  iteration output cap, records usage, emits TURN_START/COMPLETE with `phase=synthesis`.

### 4.5 Which mechanism handles which overflow — cheat sheet

| Symptom | Handler | LLM call? |
|---|---|---|
| Prompt near window but < 82% | Pressure ladder trims/drops slots | No |
| `prompt+output > TPM allowance` (Groq-style) | Shrink tiers escalate; tool profile shrinks | No |
| Fill ≥ 82% with history | Compactor summarizes head, keeps tail of 3 | **Yes** (or extractive fallback) |
| Fill ≥ 82% with ≤ 3 msgs (one giant tool result) | Truncate tool results at 12 k chars | No |
| Phased model accumulates across iterations | Rollup to extractive summary every few turns + final synthesis | Synthesis only: **yes, once** |

---

## 5. Prompt construction (what the model actually sees)

### 5.1 `build_system_prompt` — 12 sections, 2 tiers

`src/kalash/runtime/prompt.py:879-951`. Full tier (≈3.9 k tokens, per the comment at
`prompt.py:905-910`):

1. `_IDENTITY` (`prompt.py:55-95`) — "You are Kalash, a senior terminal-native SWE agent… Your
   job is not to explain programming… own the task end-to-end." Plus the anti-chatbot list.
2. `_REASONING` (`prompt.py:102-133`) — private CoT stays private; Parse→Inspect→Hypothesize→
   Validate→Implement→Verify→Conclude (collapsed for tiny tasks).
3. `_DOCTRINE` (`prompt.py:140-197`) — own the task, inspect before editing, smallest diff,
   follow the repo, debug scientifically, no speculative work, high-risk boundaries, finish loop.
4. `_TOOL_DISCIPLINE` (`prompt.py:204-257`) — dedicated tool over shell equivalent, batch
   independent calls, read-before-overwrite, background long-lived processes, `sandbox_warning`
   handling, REFUSED/ERROR is diagnostic (don't retry identically).
5. `_PLANNING` (`prompt.py:264-313`) — **mandatory `todo` list first** for multi-file work +
   the ReAct loop restated for the model.
6. `_CONTEXT` (`prompt.py:320-340`) — scratchpad refs `#f3/#s1/#w2`, `expand(ref[, grep/offset])`,
   `note` for durable state.
7. `_MEMORY` (`prompt.py:343-361`) — `recall`/`remember`/`forget` discipline.
8. `_FEW_SHOT` (`prompt.py:368-501`) — 8 calibration examples A–H (bug fix, feature, ambiguity,
   verification failure, tool error, destructive op, nested instructions, "run the unit tests").
9. `_VERIFICATION` (`prompt.py:508-531`) — see §11.
10. `_COMMUNICATION` (`prompt.py:538-575`) — terminal-native terseness; completion report format.
11. `_SAFETY` (`prompt.py:582-634`) — low/med/high risk model; untrusted-data rule incl.
    prompt-injection ("ignore previous instructions" is data); never echo secrets.
12. Mode contract: `_BUILD_MODE` (`prompt.py:641-657`, act-don't-describe) or `_PLAN_MODE`
    (`prompt.py:660-679`, read-only investigation → executable plan).

Compact tier (`compact=True`, `prompt.py:911-921`) keeps 7: identity, tool discipline, planning,
context, memory, safety, mode — drops reasoning, doctrine, few-shot, verification,
communication. Used when `_fit_prompt` (`agent.py:363-397`) finds the full prompt + minimal
tools + 2048 output don't fit `input_budget`, and at shrink tier ≥ COMPACT_PROMPT
(`loop.py:588-589,604-605`).

Static prompt is deliberately cache-friendly: volatile env goes in slot 3, not the prompt
(`prompt.py:19-22`, `detect_environment` kept separate at `prompt.py:820-872`).

### 5.2 Project instructions (KALASH.md chain)

`discover_project_instructions` (`prompt.py:727-772`): user-global (`~/.kalash/KALASH.md`) → walk
`base … parents[:12]` outermost-first, first of (`KALASH.md`, `AGENTS.md`) per directory, capped
at 12 k chars each (`_read_capped`, `prompt.py:698-715`). Rendered with precedence note
("nearer wins", `prompt.py:779-813`) and **injected as the last system section** — i.e. lower
priority than system prompt, higher than nothing else; instruction-like text elsewhere is
explicitly untrusted data. `Agent.send` passes `kalash_md_chain=list(instructions)`
(`agent.py:140`); shrink tier 5 drops it (`loop.py:607-608`).

### 5.3 Assembly into the wire request

Per turn: `assembler.assemble(...)` 9 slots (`context.py:113-186`) → `_split_system`
(`serialize.py:220-242`: join SYSTEM texts with `\n\n`, drop `<cache_breakpoint/>`, keep the
rest as conversation) → `_estimate_request_tokens` (system prose + minified tools JSON +
messages, ×1.05 + 32 framing, `loop.py:1128-1166`) → `fit_request_size` TPM clamp
(`limits.py:355-382`) → `gateway.stream(messages, system=..., tools=..., max_tokens=...)`
(`loop.py:915-920`). Provider adapters re-serialize: Anthropic keeps system separate + adds
`cache_control` (`providers/anthropic.py:286-307`); OpenAI folds system in as first message and
converts `input_schema` → `function.parameters` (`providers/openai.py:82-99,160-176`), assistant
`ToolUseBlock`s → `tool_calls`, `ToolResultBlock`s → `role: tool` messages
(`providers/openai.py:101-157`).

---

## 6. Tool schema (how tools are described to the model)

- **Authoring:** implement the `Tool` Protocol (`tools/base.py:172-240`): `name, version,
  description, params (Pydantic model), side_effect (NONE/READ/WRITE/EXEC), capabilities,
  timeout_s, max_output_bytes, idempotent, cancellable, dynamic_capabilities(args), async
  execute(args, ctx) -> ToolEnvelope`.
- **Registration:** `default_registry(include_memory=True, include_task=True)`
  (`tools/builtins.py:82-100`) builds `core_tools` (15: read, glob, search, list, write, edit,
  multi_edit, shell, fetch, web_search, todo, note, expand, skill + …) in fixed order, plus
  memory (`recall/remember/forget`) and `task`. `ToolRegistry` resolves builtin > plugin > MCP
  (`tools/registry.py:29-73`); MCP wired post-build (`agent.py:273-278`, `bootstrap.py:14-22`).
- **Wire shape:** `tool_schema()` (`tools/schema.py:92-115`) emits
  `{name, description[:200], input_schema: minify(params.model_json_schema())}` —
  **Anthropic-native `input_schema` key**, which the OpenAI adapter also reads
  (`providers/openai.py:171-174`). Minification drops `title/default/additionalProperties` and
  truncates descriptions (field ≤100 chars) — lossless for call validity.
- **Sizing:** `select_profile` (`tools/schema.py:118-169`) measures the minified JSON cost and
  picks the largest of full → standard (`STANDARD_TOOLS` = minimal + glob/multi_edit/note/expand)
  → minimal (`MINIMAL_TOOLS` = read/write/edit/search/shell/list/todo/web_search/fetch) that fits
  `budget_tokens - prompt_tokens - reserve_output(2048)`. `todo` is deliberately kept in minimal
  ("without it a small model … dives straight into a build", `schema.py:42-45`); `web_search` +
  `fetch` kept because "'I don't have a web search tool' is technically accurate and completely
  unacceptable" (`schema.py:53-56`). `ToolHost.schemas()` (`toolhost.py:262-321`) adds plan-mode
  filtering (withhold WRITE/EXEC tools entirely — "a tool the model can see but never use wastes
  schema tokens and invites a rejected call every turn"), `supports_tools` gating (deepseek-r1,
  groq/compound get `[]`, `limits.py:188-192,426-428`), and `force_profile` override from shrink
  tiers.
- **Model-facing consequence:** on an 8 k-TPM model the agent may legitimately see only 9 tools;
  the model cannot know the other 10 exist. That is by design (fit) but worth stating to users
  via `/tools` + the `minimal-overflow` profile name.

---

## 7. Tool calling — correctness path end-to-end

Follow one call, e.g. `write(path, content)`, through every function it touches:

```
1. Model emits tool_use ── StreamHandler accumulates BlockDelta JSON ── _repair_tool_json
   (stream.py:33-88: strip fences → json.loads → json_repair → brace-closing → {"_raw": raw})
   → ToolUseBlock(id, name, input) (stream.py:115-134)
2. loop._execute_tools partitions by host.category(name) (loop.py:952-957;
   category derives from SideEffect, toolhost.py:323-336; unknown → WRITE/serialize)
3. Per call → _execute_tool_with_hooks (loop.py:1016-1063):
     emit TOOL_START → await host.execute(name, input, tool_use_id) → wrap str in
     ToolResultBlock; ANY exception → ToolResultBlock(is_error, "Error executing…") + TOOL_COMPLETE(ok=False)
4. host.execute (toolhost.py:338-395):
     a. risk = classify_tool_call(name, args, cwd, writable_roots)  # paths/hosts/command/risk class
     b. blocked = _run_pre_hooks(PRE_TOOL_USE) → "REFUSED by a PreToolUse hook…" on block (toolhost.py:455-488)
     c. task-announce AGENT_SPAWN for task tool (toolhost.py:357-368)
     d. verdict = _gate(risk, args) → refusal string or None (toolhost.py:514-559; detail §7.1)
        → on refusal: emit TOOL_DENIED, return verdict (model sees REFUSED…)
     e. ctx = build_context(allow_network=_needs_network(risk)) (toolhost.py:240-258,132-153)
     f. envelope = registry.dispatch(name, args, ctx) (registry.py:127-167: get → params.model_validate
        → tool.execute → catch-all → ToolEnvelope.fail; NEVER raises)
     g. rendered = _render(envelope) (toolhost.py:645-669: ERROR code/message/remediation | content +
        truncation hint → _maybe_defer) ; _publish_result TOOL_COMPLETE/AGENT_COMPLETE (toolhost.py:397-451);
        _run_post_hooks (observational, toolhost.py:490-510)
5. Back in loop: results appended as one USER message (loop.py:341-346), persisted
   (_persist_tool_results → persist_user_message → SQLite turns/messages, loop.py:1123-1125,1102-1120),
   next iteration streams with tool results in context.
```

### 7.1 The gate in detail (`ToolHost._gate`, `toolhost.py:514-600` + `permissions/policy.py` 6 stages)

Order matters (first match wins): plan-mode WRITE refusal → read-only-network fast-path
(`web_search`/`fetch` allowed without prompt outside `read_only`) → policy evaluate
(deny → protected → grants → sandbox → approval → confirmation-classes; stage 6 can only ADD a
prompt) → `_ask` approval dialog (`ALLOW_ONCE/SESSION/ALWAYS/MODIFY/DENY`; session grants keyed
by `_signature`: `shell:<program>:<risk>` so `npm install` approval never covers `rm -rf`,
`toolhost.py:602-617`, persisted via `GrantStore`, `toolhost.py:619-641`).

Fail-closed properties the tests pin (`tests/unit/test_agent_loop.py:266-379`): plan withholds
+ refuses; ordinary workspace writes don't prompt; DENY blocks `rm -rf`; no approval channel
denies instead of hanging; protected paths (`.env`) refused even on ALLOW_ALWAYS; outside-roots
writes refused; force-push prompts with `WRITE_REMOTE + DESTRUCTIVE_GIT + irreversible`.

### 7.2 Correctness of argument handling

- **Validation:** `tool.params.model_validate(args)` in `dispatch` (`registry.py:147-156`) —
  wrong types → `KALASH_TOOL_INVALID_ARGS (recoverable)` with remediation, model can retry.
- **Malformed JSON:** 4-layer repair (`stream.py:33-88`). Unrepairable → `{"_raw": raw}` sentinel.
  If the turn also hit `MAX_TOKENS`, the loop treats `_raw` as *truncation evidence* and doubles
  the output budget to 16 k max (`loop.py:306-325`). If the provider rejects the call server-side
  ("failed to parse tool call…"), `diagnose` classifies `invalid_tool` and the loop re-prompts
  with the available-tools list and retries (`loop.py:547-556`, `diagnose.py:139-151`).
- **Unknown tool:** `dispatch` → `KALASH_TOOL_UNKNOWN`; hallucinated names → provider error →
  `diagnose` `invalid_tool` ("The model tried to call `X`, which is not loaded. Use only the
  tools listed…", `diagnose.py:153-167`).
- **Pairing invariant:** every `ToolUseBlock` gets exactly one `ToolResultBlock`, even on cancel
  (`loop.py:988-1003`) and across resume (dangling calls dropped, `serialize.py:190-217`) —
  providers reject unbalanced histories, so this is load-bearing.

---

## 8. Parallel calls

**Model side (emit):** both provider adapters support multiple calls per turn. Anthropic:
`block_index` increments per `content_block_stop` (`providers/anthropic.py:441-443`); OpenAI:
`tc_to_block_index` routes each `tool_calls[].index` delta stream to its own block
(`providers/openai.py:384-460`). `StreamResult.tool_calls` returns all
(`stream.py:156-163`). Capabilities declare `parallel_tool_use=True`
(`providers/anthropic.py:247`, `providers/openai.py:295`).

**Execution side (the actual policy), `loop.py:938-1004`:**

```python
reads      = [c for c in tool_calls if host.category(c.name) == READ]
serialized = [the rest]   # WRITE + EXEC + unknown
read_results = await asyncio.gather(*[read(call) for call in reads], return_exceptions=True)
for call in serialized:   # strictly sequential, cooperative-cancel checked between calls
    results.append(await write(call))
# then: guarantee pairing for every call.id
```

- READ concurrency capped by `asyncio.Semaphore(read_concurrency=5)` (`loop.py:148,162,1006-1009`).
- WRITE/EXEC serialize on `asyncio.Lock` (`loop.py:149,163,1011-1014`) — two `rm` calls in one
  turn cannot interleave; a `write` + `edit` to the same file cannot race.
- `return_exceptions=True` on reads means one failing read doesn't cancel siblings; each
  exception becomes an error `ToolResultBlock` (`loop.py:964-974`).
- Ordering note: all reads complete before the first write starts, regardless of the model's
  emission order; results are appended in `[reads…, writes…]` order, not emission order. The
  model correlates by `tool_use_id`, so this is safe but slightly lossy for debugging.

**Study answer:** "parallel calls? Reads yes (≤5 concurrent), writes/execs no (serialized); the
distinction derives from each tool's declared `SideEffect` via `ToolHost.category`."

---

## 9. Retries (two layers)

### 9.1 Layer A — `ModelGateway` (transport reliability)

`src/kalash/models/gateway.py:217-472`:

- `RetryConfig(max_attempts=6, initial=1s, max=30s, jitter 25%, budget=180s)` (`gateway.py:260-274`).
- `_stream_with_retry` / `_attempt_with_retry`: classify via `classify_error`
  (`gateway.py:106-139`: CONTEXT_EXCEEDED / AUTH / TRANSIENT incl. 429/5xx/timeout/connection +
  TPM-limit learning via `Limit N` regex → `ConstraintCache.record_tpm` / error BEFORE retry).
  AUTH/PERMANENT/CONTEXT_EXCEEDED → raise immediately (no retry). Transient → sleep
  `Retry-After(+0.5s)` if parseable (`_extract_retry_after`, `gateway.py:221-257`) else
  exp-backoff+jitter, capped by remaining budget. **If any chunk already yielded
  (`yielded_any`), never retry** — would duplicate output (`gateway.py:371-374`).
- Proactive pacing *before* the call: `SlidingWindowRatePacer.pace` sleeps up to 60 s so the 60-s
  token window + estimate stays under 95% of TPM (`gateway.py:182-213`); usage recorded per call
  (`_accumulate_usage` → `pacer.record_usage`, `gateway.py:321-334`, `_track_stream_usage`,
  `gateway.py:599-614`).
- Provider fallback chain: `complete()` tries primary then fallbacks; AUTH on one → try next;
  CONTEXT_EXCEEDED on last → `ModelContextExceededError`; all fail → `ModelAllFailedError`
  (`gateway.py:476-540`). `stream()` yields `StreamError(all_failed)` instead of raising
  (`gateway.py:542-597`) — which is what the loop's `explain()` path consumes.
- `fit_request_size` output clamp per provider before each attempt (`gateway.py:503-506,569-572`).

### 9.2 Layer B — `AgentLoop._stream_turn` (request-shape repair, 6 attempts)

`loop.py:444-560`: assemble at tier → TPM preflight (escalate tier, `continue`) → emit
TURN_START with `binding_constraint/shrink_tier/iteration_output_cap/phased` → stream → on
success return; on error `diagnose(error)` (`models/diagnose.py:68-203`, kinds
`tpm/max_output/no_tools/invalid_tool/auth/context/rate/quota/unknown` with parsed
`Limit/Requested` numbers and caps) → `_record_observed_limits` (feeds `ConstraintCache`,
`loop.py:562-572`) → repair cascade: output-cap error once (`adjust_max_output`, `loop.py:521-533`)
→ input-shrink tiers (`reduce_input`, `loop.py:535-545`) → invalid-tool re-prompt with tool names
(`loop.py:547-556`) → else return the error to `run()` → `ERROR` finish.

Total worst-case streams per iteration: 6 (loop) × 6 (gateway) × providers — bounded by the
180-s gateway budget per provider attempt chain. There is **no retry of tool executions**: a
failed tool returns an error string once; retrying is the model's decision on the next turn
(prompt explicitly discourages identical retries: `_TOOL_DISCIPLINE`, `prompt.py:252-254`).

---

## 10. Timeouts and truncated-output handling

### 10.1 Timeouts — declared everywhere, enforced in three places

Every `Tool` declares `timeout_s` + `max_output_bytes` (`tools/base.py:211-219`), but
**`ToolRegistry.dispatch` and `ToolHost.execute` never enforce them** — no `wait_for` wrapper
exists at the seam. Enforcement is per-tool, opt-in:

| Tool | Declared `timeout_s` | Actually enforced? How |
|---|---|---|
| `shell` | 120 s (param max 1800 s) | **Yes** — `_run_with_timeout` deadline loop + `SIGTERM → 5 s → SIGKILL` process-group kill (`shell.py:481-545`); `stdin=/dev/null`, setsid isolation, live 0.25-s stream polling that also watches `cancel_event` |
| `task` (subagent) | 300 s (param max 1800 s) | **Yes** — `asyncio.wait_for(run_isolated(...), timeout)` → `KALASH_TOOL_TIMEOUT` (`task.py:171-189`) |
| `search` | (tool-specific) | **Yes** — `asyncio.wait_for(...communicate...)` (`search.py:165-173` → `KALASH_TOOL_TIMEOUT`) |
| `fetch` / `web_search` | 30 s (`web.py:34,104-105`) | **Yes** — httpx request timeout; timeout envelope at `web.py:187` |
| `read/write/edit/glob/list/todo/note/expand/skill/memory/*` | various | **No central enforcement** — fast local ops; a hung NFS read or hung memory provider would hang the turn until gateway/loop ceilings hit. `core/events.py` `emit` likewise has no timeout (a slow handler stalls the loop — noted in prior review M9) |
| Gateway request | retry *budget* 180 s, pacing sleep ≤60 s | **No per-request deadline** — relies on provider SDK defaults. A provider that hangs without bytes keeps the turn open; mitigation is only the outer session `max_wallclock_s`, which itself is currently untracked (gap G-03) |
| Loop iteration | none explicit | Bounded indirectly by model latency + tool timeouts + `max_iterations` |

Retry-After parsing (`gateway.py:221-257`: header + "try again in Ns" patterns, ms-aware) and
jittered backoff prevent thundering retries; env sanitization (`_safe_env` strips
`LD_PRELOAD/NODE_OPTIONS/PYTHONPATH/…`, `shell.py:143-148`) and `cwd`-allowance checks
(`shell.py:253-266,415-426`) are the timeout-adjacent safety rails.

### 10.2 Truncated output — four layers, each with a different job

1. **Byte caps at the source.** `shell`: 128 KiB per stream, live-truncated during collection
   (`shell.py:38,447-478`); `fetch`: 5 MiB response cap + truncated/full/selective modes
   (`web.py:32-70`); each tool reports `truncated + TruncationInfo` in its envelope
   (`tools/base.py:36-44,113-127`).
2. **Scratchpad deferral at the host** (`toolhost.py:77-88,671-698`): only `DEFERRABLE =
   {shell, search, fetch, glob, list}` and only > 6,144 bytes; reads/edits deliberately excluded
   ("truncating [a read] would produce edits that fail to apply"). Stores full body via
   `get_scratchpad(session).put(...)` (content-hash deduped), returns first 24 lines +очные
   `[N more lines stored as #xN — expand(#xN) …]` stand-in. Uses `risk.summary` as the headline.
   On store failure, falls back to inlining (never loses data).
3. **History trim when too short to compact** (`loop.py:851-871`): `ToolResultBlock`s > 12,000
   chars get `content[:12000] + "… [N chars truncated]"` when the whole conversation is ≤ 3
   messages (giant first-turn result). Shape-preserving (still a ToolResultBlock with same id).
4. **Truncated *tool calls* (input side)** (`loop.py:305-325` + `stream.py:33-88`): 4-layer JSON
   repair (fences → `json.loads` → `json_repair` → brace-closing → `{"_raw": raw}`); if the stop
   reason is MAX_TOKENS *and* any input carries `_raw`, the turn is retried with doubled output
   budget (≤16,384). This is the "model got cut off mid-arguments" path, distinct from "tool
   output too big."

Token estimation underneath it all is heuristic (`len/3.2 code, /4.0 prose, ×1.05`,
`core/budget.py:145-154`; request estimate adds ×1.05 + 32 framing, `loop.py:1166`) —
conservative by design, imprecise by construction (gap G-11).

---

## 11. Verifier — does it run tests/lint/typecheck and self-correct?

### Short answer: no automatic verifier; verification is a prompt doctrine plus emergent ReAct feedback.

**What exists (prompt-level):**

- `_VERIFICATION` (`prompt.py:508-531`): "Before reporting implementation work as complete: re-read
  request → check criteria → run strongest relevant verification → investigate failures → state
  what was verified and what was not. Narrowest checks first: targeted tests → focused
  typecheck/lint → broader tests → build/integration. Zero exit ≠ correct. If blocked, say so
  plainly. Never imply a test passed when not run."
- `_DOCTRINE` §5–6 + item 8 "Finish the loop: a patch is not done when edited but when verified"
  (`prompt.py:170-197`); `_FEW_SHOT` examples B/D model verify-then-report and treating test
  failure as evidence (`prompt.py:395-448`); completion-report format demands a `Verified:` line
  (`prompt.py:558-564`).

**What does NOT exist (code-level) — verify each by grep:**

- No `pytest`/`ruff`/`mypy`/`tsc`/`npm test` invocation anywhere in `src/kalash/runtime/`,
  `src/kalash/tools/`, or the loop. The only test runner is the *repo's own* dev harness
  (`pyproject.toml` pytest config), not something the agent calls.
- No post-edit hook: `WriteTool`/`EditTool` (`tools/fs.py`) return diff metadata; nothing
  triggers a check. `ToolHost._run_post_hooks` is observational (`toolhost.py:490-510`).
- `CheckpointManager.auto_checkpoint_before_destructive` (`runtime/checkpoint.py:174-186`) exists
  but **no caller invokes it** from the loop/tool path (grep `auto_checkpoint` → definition only).
- The loop's "self-correction" is exactly one mechanism: tool results (including `ERROR …` +
  `remediation`) are appended as user messages and re-streamed (`loop.py:338-346`), so the model
  *can* notice and adapt — but nothing *forces* it to (no re-test gate, no "must re-run failing
  check" state machine, no circuit breaker counting repeated identical failures).

**So "just emits code blindly"?** Not blindly — the Observe step is real (results, errors with
codes/remediation, diff metadata, exit codes, sandbox warnings all reach the model). But
**verification is voluntary**: a model that skips `shell: pytest …` still terminates
`NO_TOOL_CALLS` successfully. The `_VERIFICATION` prompt + `TURN_COMPLETE`/`TOOL_COMPLETE` events
give a UI the *visibility* to notice; no code gives the *guarantee*.

A proper verifier (see §15, G-01) would be: post-`write/edit` policy that runs the repo's
detected check (`pytest -x -q <touched>`, `ruff`, `mypy`) in the sandbox, appends the outcome as
a non-bypassable observation, and blocks `NO_TOOL_CALLS` termination while a touched-file check
is red — i.e. move verification from prompt advice into loop state.

---

## 12. Determinism — same prompt + seed = stable diff?

### Short answer: no. Temperature is a dead knob; seed/top-p don't exist.

Evidence chain (follow it yourself):

1. `core/config.py:50` declares `ModelConfig.temperature: float | None = None`; `config.py:241`
   parses it from settings. **No other non-provider file reads `.temperature`** (grep
   `\.temperature` → only provider-internal forwards).
2. All four provider adapters accept `temperature: float | None = None` and forward it only when
   not None (`providers/anthropic.py:269,309-310`; `openai.py:313,329-330,353,371-372`;
   `google.py:264-302`; `openai_compatible.py:114-209`).
3. **Nobody passes it.** `AgentLoop._stream_response(messages, system, tools, max_output_tokens,
   on_text_delta)` (`loop.py:896-932`) has no temperature parameter; both gateway call sites
   (`loop.py:915-920`) omit it; `gateway.stream/complete` default it to None
   (`gateway.py:483,549`); `build_agent` never reads `settings.model.temperature` (`agent.py:177-360`).
   Net: providers always receive `temperature=None` → provider-side default (usually 1.0 /
   sampling on).
4. `top_p`, `seed`, `top_k`, `stop_sequences` (plumbed but never set by the loop), `response_format`
   appear nowhere in the loop path. `seed = machine-id` in `tui/auth_store.py:29-32` is credential
   encryption, unrelated.
5. Beyond sampling params, determinism is structurally impossible today: tool results embed
   timestamps/git state, `read_concurrency` changes result *order* only (safe), but web/fetch
   results drift, and the TPM pacer sleeps variable durations — none of which a seed would fix.

**What "stable diff" would require** (gap G-04, §15): thread `temperature/top_p/seed` from
`KalashConfig.model` → `AgentLoop` → `gateway.stream/complete` → providers (all four already
accept temperature; add `seed`/`top_p` to `ProviderProtocol`, `gateway.py:52-88`, and each
`_build_request`); expose `--temperature/--seed` CLI flags and `KALASH_MODEL_*` env; default
`temperature=0` for edit tasks or at least document non-determinism; add a determinism eval
(same prompt × N, diff-stability metric) in `evals/`.

---

## 13. Exactly what is implemented, at which file (inventory)

Starred (`★`) = must-read for the four asked topics. Line numbers are from the current checkout.

### ★ Agent loop & assembly

| File | Lines | What lives here (exact) |
|---|---|---|
| `src/kalash/runtime/loop.py` ★ | 1166 | `TerminationReason` (70-78), `LoopResult` (86-96), `AgentLoop` dataclass incl. `max_iterations=50, read_concurrency=5, max_output_tokens=8192` (109-159), `run()` (195-376), `_finish` (378-408), `_emit_context_usage` (410-426), `_stream_turn` 6-attempt repair loop (428-560), `_record_observed_limits` (562-572), `_assemble_for_tier` (574-655), rollup (660-688), work-context + `_synthesize_final_answer` (699-785), `_ensure_context_fits` + extractive summary + 12 k trim (787-871), `cancel/is_running/conversation` (873-891), `_stream_response` via `StreamHandler` (896-932), `_execute_tools` READ-parallel/WRITE-serial + pairing guarantee (938-1004), `_execute_tool_with_hooks` (1016-1063), persistence `_persist_turn/persist_user_message/_persist_tool_results` (1069-1125), `_estimate_request_tokens` (1128-1166) |
| `src/kalash/runtime/agent.py` ★ | 531 | Module docstring = "build_agent is that place" (1-18), `Agent` incl. `send()` recall→`loop.run`→capture (56-167), `build_agent()` full wiring: session→registry→policy→approval→hooks→MCP→ToolHost→budget→assembler→prompt tiers→AgentLoop (177-360), `_fit_prompt` full-vs-compact choice (363-397), `_discover_skills` names-only (400-419), `run_isolated()` child entry (422-496), `_load_history/load_history_async` (499-521) |
| `src/kalash/runtime/context.py` ★ | 385 | `PRESSURE_THRESHOLDS` (26), `PressureLevel L0-L7` (29-40), `Slot` 0-8 (47-58), `CACHE_BREAKPOINTS` (62), `ContextAssembler.assemble` 9 slots (113-186), pressure/budget alloc (200-230), slot builders incl. L2/L3/L4/L6 degradation (236-312), `_emit_messages` + breakpoint markers (323-339) |
| `src/kalash/runtime/prompt.py` ★ | 982 | `_IDENTITY/_REASONING/_DOCTRINE/_TOOL_DISCIPLINE/_PLANNING/_CONTEXT/_MEMORY/_FEW_SHOT-A-H/_VERIFICATION/_COMMUNICATION/_SAFETY/_BUILD_MODE/_PLAN_MODE` (55-679), `discover_project_instructions` (727-772), `detect_environment` (820-872), `build_system_prompt(full/compact)` (879-951) |
| `src/kalash/runtime/toolhost.py` ★ | 698 | `ToolCategory` (57-63), `ToolHostProtocol` (65-74), `DEFERRABLE/DEFER_THRESHOLD/DEFER_HEAD` (81-88), `_MODE_CAPABILITIES` (91-103), `ToolHost` incl. `build_context/schemas/category/execute/_gate/_ask/_render/_maybe_defer` (184-698) |
| `src/kalash/runtime/compaction.py` ★ | 331 | `COMPACTION_THRESHOLD=0.82` (24), `MAX_SUMMARY_DEPTH=3` (27), `COMPACTION_SYSTEM_PROMPT` (30-38), `Compactor.should_compact/compact/_hierarchical_summarize/_summarize_chunk/_extract_preserved/_build_final_summary/_persist_summary(append-only)/_fallback_summary` (86-331) |
| `src/kalash/runtime/shrink.py` ★ | 58 | `ShrinkTier` 0-5 (15-24), `TierFlags` + `_TIER_FLAGS` table (26-45), `tier_flags/next_tier` (48-58) |
| `src/kalash/runtime/stream.py` ★ | 302 | `_repair_tool_json` 4 layers (33-88), `_BlockAccumulator` (96-134), `StreamResult.tool_calls/has_tool_calls/text_content` (156-170), `StreamHandler.feed/result` + per-event handlers (212-302) |
| `src/kalash/runtime/serialize.py` | 242 | `FORMAT_VERSION=1`, block↔JSON, `serialize/deserialize_blocks`, `rehydrate_messages` (merge same-role, skip system), `_drop_dangling_tool_calls`, `split_system` (drops `<cache_breakpoint/>`) |
| `src/kalash/runtime/bootstrap.py` | 29 | `wire_agent_tools` (MCP, non-fatal), `prepare_agent` (logging + MCP) — called by every entry point before first turn |
| `src/kalash/runtime/session.py` | ~140 | `SessionManager.create/resume` (SQLite), turn-seq seeding used by `loop._next_turn_seq` (loop.py:179-189) |
| `src/kalash/runtime/checkpoint.py` | 302 | `CheckpointManager.create/rewind/auto_checkpoint_before_destructive` — implemented, **not called** by loop/tools |
| `src/kalash/runtime/scratchpad.py` | 577 | `get_scratchpad(session).put(kind, headline, content)` content-hash dedupe; `render_notes/render_index`; `expand` tool backend for deferral refs |

### ★ Model gateway, limits, diagnosis

| File | What lives here |
|---|---|
| `src/kalash/models/gateway.py` ★ (640) | `ProviderProtocol` (52-88, incl. `temperature` passthrough), `ErrorKind` + `classify_error` with TPM learning (95-139), `SlidingWindowRatePacer` (147-213), `_extract_retry_after` (221-257), `RetryConfig(6/1s/30s/25%/180s)` (260-274), `_stream_with_retry` incl. `yielded_any` no-retry rule (338-392), `_attempt_with_retry` (394-472), `complete` with fallback chain (476-540), `stream` yielding `StreamError(all_failed)` (542-597), `_track_stream_usage` (599-614) |
| `src/kalash/models/limits.py` ★ (445) | `DEFAULT_MAX_OUTPUT=4096`, `MIN_USABLE_OUTPUT=1024`, `TARGET_OUTPUT_TOKENS=2048`, `TPM_REQUEST_SHARE=0.9` (28-52), `IterationBudget` phased envelope (60-84), `_PROVIDER_TPM_DEFAULTS` groq 8 k… (102-109), `ConstraintCache` monotonic learning (112-156), `_TABLE` ~25 patterns (160-203), `effective_limits/binding_constraint/iteration_budget/tpm_allowance/fit_request_size/resolve_output_tokens/input_budget/supports_tools/describe` (246-444) |
| `src/kalash/models/diagnose.py` (218) | `Diagnosis(summary/remedy/kind/adjust_max_output/reduce_input/tpm_limit)` (30-61), `_TPM/_MAX_OUT` regexes (25-27), `diagnose()` 8-way classifier (68-203), `explain()` (215-218) |
| `src/kalash/models/normalize.py` (270) | `Role/StopReason`, `Text/ToolUse/ToolResult/Thinking/Image/Memory/Opaque` blocks, `Message/ModelResponse`, stream events `MessageStart/BlockStart/BlockDelta/BlockStop/UsageUpdate/MessageStop/StreamError`, `ModelCapabilities(parallel_tool_use…)` |
| `src/kalash/models/resolve.py` (222) | `active_selection/credential_for/base_url_for/build_provider/build_gateway` — saved `/connect` > env; constructs the four adapters; never raises (returns `Resolution`) |
| `src/kalash/models/providers/anthropic.py` (467) | Wire serialize/deserialize, usage normalization, `cache_control` breakpoints (system + last tool), streaming event map, `temperature` forward-if-set |
| `src/kalash/models/providers/openai.py` (478) | `input_schema→function.parameters`, assistant `tool_calls`, user `tool` messages, multi-call `tc_to_block_index` routing, `max_completion_tokens`, usage incl. cached/reasoning tokens |
| `src/kalash/models/providers/google.py` (454) + `openai_compatible.py` | Same `temperature` pattern; compatible provider carries `tool_use/context_window/provider_name` for unknown endpoints (Groq/Together/local) |

### Tools, permissions, orchestration, budgets, tests

| File | What lives here |
|---|---|
| `src/kalash/tools/base.py` (260) | `SideEffect`, `TruncationInfo/ToolError/SideEffectRecord`, `ToolContext` (incl. `can_spawn`, `allow_network`, `cancel_event`), `ToolEnvelope(success/fail)`, `Tool` Protocol, `tool_json_schema/tools_manifest` |
| `src/kalash/tools/registry.py` (167) | `ToolRegistry`: builtin>plugin>mcp priority, `get/has/list_tools/list_schemas/unregister`, `dispatch` (validate→execute→catch-all, never raises) |
| `src/kalash/tools/builtins.py` (100) | `core_tools()` fixed order, `memory_tools()`, `task_tools()`, `default_registry()` |
| `src/kalash/tools/schema.py` (177) | `_DROP_KEYS`, `MINIMAL_TOOLS` (9) / `STANDARD_TOOLS` (13), `minify_schema/tool_schema(input_schema)/select_profile/schema_tokens` |
| `src/kalash/tools/shell.py` (545) | `ShellParams(command/cwd/timeout≤1800/background)`, setsid + `_safe_env`, `_wrap_sandboxed`, bg registry `_BACKGROUND`, `_run_with_timeout` + `_collect_stream` (128 KiB, live `TOOL_OUTPUT` events, cancel-aware), SIGTERM→5 s→SIGKILL |
| `src/kalash/tools/task.py` (220) | `TaskParams(prompt/context_files/capabilities/timeout_s≤1800/max_turns≤200)`, capability-subset check, `ctx.can_spawn` depth check, `wait_for(run_isolated)` → `TaskResult`-backed envelope with `task_id/turns/child_tokens/termination` |
| `src/kalash/tools/fs.py` (1072), `search.py`, `web.py` (fetch 5 MiB/30 s), `todo.py`, `scratch.py` (`note/expand`), `skill.py`, `memory_tools.py` | Tool implementations; each declares `timeout_s/max_output_bytes` (enforcement varies — §10.1) |
| `src/kalash/permissions/classify.py` (586) + `policy.py` (393) + `grants.py` + `console.py`/`prompt.py` | `classify_tool_call` → `ToolRisk`; 6-stage policy; grant store; approval prompt UI |
| `src/kalash/orchestration/subagent.py` (261) | `SubagentRunner.spawn`: depth check, cycle check (agent+prompt-prefix key), `reserve_for_child($0.50 default)`, isolated `run_isolated`, envelope-only return, reservation release |
| `src/kalash/orchestration/team.py` (422) | `TeamPattern` fan-out/fan-in with `max_concurrency=8` semaphore, budget ceilings, cycle detection — available, not in the hot loop path |
| `src/kalash/core/budget.py` (154) | `BudgetDimension`, `Usage`, `Pricing` (Decimal), `BudgetState` ceilings + `check_ceiling/check_warning/record_usage/reserve_for_child/release_reservation`, `estimate_tokens` heuristic |
| `src/kalash/core/events.py` (166) | 23 `EventType`s (`TURN_START/COMPLETE, TOOL_START/COMPLETE/DENIED/OUTPUT, AGENT_SPAWN/COMPLETE, COMPACT_START/COMPLETE, BUDGET_*…`); `EventBus.emit` gather (no timeout — gap) |
| `src/kalash/core/config.py` (278) | 6-layer merge, `SECURITY_KEYS` user-scope-only, `ModelConfig.temperature` parsed-but-unconsumed (gap G-04) |
| `tests/unit/test_agent_loop.py` (579) | Fake-gateway proof "model tool call creates a real file", tools-reach-provider, system-prompt, tool-result feedback, history carry, 10 gate tests, host mechanics (deferral, categories, errors), serialize/resume round-trips, instruction precedence |
| `tests/unit/test_iteration_phases.py` (149) | Phased envelope math, rollup keep-`[task+2]`, synthesis text production |

---

## 14. Connections — who calls whom (wiring map)

### 14.1 Build-time wiring (once per agent): `build_agent` is the switchboard

`src/kalash/runtime/agent.py:177-360`. Every entry point converges here:

```
CLI/TUI/SDK/scheduler/cron
 ├─ cli/main.py → _pipe.run_pipe_mode  (headless: -p / stdin, OutputSink text|json|stream-json)
 ├─ tui/app.py KalashApp               (Textual worker runs loop; approval dialogs; /connect /models /status)
 ├─ sdk/__init__.py                    (import build_agent, run_isolated)
 └─ scheduler/daemon.py + manager.py   (cron runs build_agent headless)
        │  all call
        ▼
build_agent(cwd, provider_id, model_id, mode, session_id, resume, config, event_bus, interactive, persist, max_iterations)
 ├─ models/resolve.build_provider ──► AnthropicProvider | OpenAIProvider | GeminiProvider | OpenAICompatibleProvider
 ├─ SessionManager.create/resume ──► StorageEngine(SQLite WAL) ──► SessionRepository + GrantStore
 ├─ default_registry(memory?, task?) + wire_mcp_tools_sync + build_hook_runner
 ├─ PermissionPolicy + build_approval_prompt ──► ToolHost(registry, policy, approval, hooks, mode, sandbox, model/provider ids)
 │     └─ host.reserved_prompt_tokens = estimate(system + instructions)
 ├─ ContextAssembler(budget, context_window=provider.window or 200_000)
 ├─ discover_project_instructions + _discover_skills(names only) + _fit_prompt(full|compact)
 └─ AgentLoop(gateway=ModelGateway(primary), tool_registry=host, session_repo, event_bus, budget, assembler, prompts, ids)
       └─ host.cancel_event = loop.cancel_event   # shared cancellation signal
prepare_agent(agent)  [bootstrap.py:24-29]  →  configure_logging + wire_mcp_tools (async)
```

Fail-soft choices worth noting: session-storage failure → ephemeral `ses_ephemeral` session, still
runnable (`agent.py:237-245`); MCP/hook wiring failure → logged, agent runs without them
(`agent.py:265-278`).

### 14.2 Run-time data flow (every turn): `Agent.send` → `AgentLoop.run`

`agent.py:107-167`:

```
Agent.send(prompt, on_text_delta)
 ├─ loop.persist_user_message ──► SQLite turns/messages (resume read path)
 ├─ detect_environment(cwd) ──► {cwd, platform, python, git_branch, git_status}
 ├─ scratchpad_blocks() [todo list + notes + index] + memory recall_blocks(prompt, limit=12)
 │     (recall failure only logged; memory off → scratchpad only)
 ├─ loop.run(user_message, system_identity, skills_catalog, kalash_md_chain, memory_blocks,
 │           recent_turns=history, environment, on_text_delta)
 │     ├─ … ReAct iterations (§2.2) …
 │     └─ returns LoopResult
 ├─ history = loop.conversation        # continuity for next send()
 └─ capture_turn_memories(...)         # failure only logged
```

Inside each iteration the loop touches, in order: `BudgetState` → `ToolHost.schemas` →
`ContextAssembler` → `limits.*` → `ModelGateway` → provider SDK → `StreamHandler` →
`diagnose`/`ConstraintCache` → `ToolHost.execute` → `ToolRegistry` → tool → `SessionRepository`
→ `EventBus` → (TUI widgets / headless sink / hooks / memory service). The `EventBus` is the
observability fan-out: `TURN_START (with binding_constraint/shrink_tier/phased/caps)`,
`TOOL_START/COMPLETE/DENIED/OUTPUT`, `AGENT_SPAWN/COMPLETE`, `COMPACT_START/COMPLETE`,
`BUDGET_WARNING/SOFT_LIMIT/EXCEEDED`, `TURN_COMPLETE`.

### 14.3 Subagent path (the loop calling itself recursively, once removed)

```
Parent loop emits task ToolUseBlock
 └─ ToolHost.execute: AGENT_SPAWN announce → gate → build_context → TaskTool.execute
      ├─ capability-subset + context-files-exist + can_spawn checks
      └─ asyncio.wait_for(run_isolated(prompt, cwd, max_turns, spawn_depth+1, model_id, context_files))
           └─ build_agent(persist=False, interactive=False, max_iterations=max_turns)
                └─ child Agent.send(brief) → child AgentLoop.run (own conversation, own budget slice)
                     └─ returns {output, usage, turns, termination} → parent envelope content
Parent loop sees only the summary string + {task_id, turns, child_tokens, termination} metadata;
child transcript rows are never written (persist=False) and never enter parent context (I-028).
`orchestration.SubagentRunner` offers the same with DB run-records + $0.50 reservation + cycle
detection, used by team patterns / scheduler rather than the hot path.
```

### 14.4 Resume path (how history survives)

Write: `loop._persist_turn / persist_user_message` → `serialize_blocks` → SQLite.
Read: `SessionRepository.get_session_messages` → `rehydrate_messages` (merge same-role, drop
system, drop dangling tool_use) → `Agent.history` (`_load_history`, `agent.py:499-521`) →
`recent_turns` seeded into the next `run()` (`loop.py:239-240`) → `assembler` slot 7. Compaction
summaries persist as `content_type=compaction_summary` rows and re-enter via slot 6
(`context.py:288-292`).

---

## 15. What lacks (gaps, ordered by importance)

Each gap: **where** (file:line), **why it matters** (impact), **how to fix** (concrete sketch).
Severity is reviewer judgment (evidence + blast radius), not category.

### G-01 [HIGH] No enforced verifier — prompt asks, code never checks

- **Where:** absence spanning `runtime/loop.py:327-336` (termination needs no verification state),
  `tools/fs.py` (no post-edit hook), `runtime/checkpoint.py:174-186` (auto-checkpoint unwired).
- **Impact:** the agent can terminate `NO_TOOL_CALLS` ("done!") with failing tests, unrun lint, or
  an unapplied edit. Self-correction depends entirely on the model obeying `_VERIFICATION`.
  Evals that grade "tests pass after edit" will be flaky for reasons outside the model's control.
- **Fix:** add `Verifier` protocol + `loop._verify_before_finish()` gate: after last tool call,
  if touched files changed this turn, run detected checks (`pytest -x -q`, `ruff`, `mypy`) via the
  existing `shell` sandbox path, append outcome as a system observation, and require green (or
  explicit user waiver) before `NO_TOOL_CALLS`. Start with "verify on `write/edit/multi_edit`
  turns only" to bound cost. Wire `auto_checkpoint_before_destructive` into shell destructive
  paths first (cheapest safety win).

### G-02 [MEDIUM] `STOP_HOOK` termination is dead

- **Where:** `runtime/loop.py:76` defined; zero emitters/checkers (grep §10 verification above);
  docstring at `loop.py:4` promises it.
- **Impact:** hook authors cannot stop the loop; the enum misleads reviewers into assuming a
  control that doesn't exist.
- **Fix:** evaluate Stop hooks at the end of each iteration (after `_persist_tool_results`, before
  `while` re-entry); on block → `_finish(STOP_HOOK, error=hook detail)`. Or delete the variant +
  docstring line. One-line test: hook returning blocked → termination is `STOP_HOOK`.

### G-03 [MEDIUM] Wallclock (and partly cost) budgets are declared but never ticked

- **Where:** `core/budget.py:64,72,107-108` defines `max_wallclock_s/wallclock_used_s`; no
  assignment to `wallclock_used_s` exists in `runtime/loop.py` or `models/gateway.py` (only
  `turns_used/tool_calls_used/tokens_used` are incremented: `loop.py:285,1036`, `budget.py:113-127`
  for tokens; `pricing` never passed so `cost_used` stays 0).
- **Impact:** a hung provider (no per-request deadline, §10.1) + no wallclock enforcement = a turn
  can hang indefinitely. Cost ceiling silently never fires.
- **Fix:** timestamp `run()` entry/exit + per-iteration accumulation into `wallclock_used_s`;
  pass provider `pricing` into `budget.record_usage` at `loop.py:287-293`; add a `wait_for` around
  `gateway.stream` drain with `BUDGET_EXHAUSTED(wallclock)` on expiry. Tests: fake clock, assert
  ceiling fires.

### G-04 [MEDIUM] Determinism knobs missing/dead (temperature parsed, never sent; no seed/top_p)

- **Where:** `core/config.py:50,241` → dead end; `runtime/loop.py:896-932` (no temperature param);
  `models/gateway.py:52-88` protocol lacks `seed/top_p`; zero `seed|top_p` hits in `src/`.
- **Impact:** "same prompt = same diff" unachievable; evals/flakes unreproducible; no low-temp mode
  for surgical edits.
- **Fix:** `KalashConfig.model += top_p/seed`; `AgentLoop += (temperature, top_p, seed)` with
  defaults from config; forward through `gateway.stream/complete` → all four providers
  (`temperature` already accepted; add `seed`/`top_p` kwargs); CLI `--temperature/--seed` +
  `KALASH_MODEL_*` env; document defaults (suggest `temperature=0.2` for edit tasks only after
  measuring tool-call validity vs creativity).

### G-05 [MEDIUM] Tool timeouts not enforced at the seam; most tools ignore `timeout_s`

- **Where:** `tools/base.py:211-214` declares; `tools/registry.py:127-167` + `runtime/toolhost.py`
  never wrap; only shell/search/task/web enforce (§10.1 table).
- **Impact:** one hung local tool (NFS read, FTS lock, memory provider) hangs the whole turn; the
  declared contract misleads tool authors into assuming coverage.
- **Fix:** wrap `registry.dispatch` in `asyncio.wait_for(ctx_timeout)` where
  `ctx_timeout = min(tool.timeout_s, remaining wallclock)`; on timeout return
  `KALASH_TOOL_TIMEOUT` envelope (consistent with shell/task). Keep per-tool streaming/cancel
  semantics (shell's graceful kill) by letting tools opt out via `cancellable` + own deadline.

### G-06 [MEDIUM] All tool exceptions become chat text — infra failures look like model errors

- **Where:** `runtime/loop.py:1043-1063` (`except Exception → ToolResultBlock(is_error)`); prior
  review M3 flagged the same.
- **Impact:** disk-full/OOM/DB-locked surface as "Error executing read: …" and the model may
  "retry differently" instead of aborting; no circuit breaker; `LoopResult(ERROR)` unreachable for
  infra causes.
- **Fix:** classify in `_execute_tool_with_hooks`: `ToolEnvelope` error codes `KALASH_TOOL_*` with
  `recoverable=False` + `OSError`/`sqlite3.Error` → re-raise to `run()` → `_finish(ERROR)` with
  `TURN_FAILED` event; keep recoverable tool-domain errors as text. Add counter: 3 consecutive
  infra failures → abort turn.

### G-07 [MEDIUM] No tool-execution retry; identical-call loops unguarded

- **Where:** single `await registry.dispatch` per call (`toolhost.py:390`); no idempotency keys
  despite `Tool.idempotent` declared (`tools/base.py:221-224`, never read outside tests).
- **Impact:** transient `KALASH_TOOL_TIMEOUT` on an idempotent read is retried only if the model
  chooses to; a model stuck retrying a refused call burns turns until 50 with no backstop (prompt
  says don't, code doesn't enforce).
- **Fix:** host-level once-retry for `recoverable + idempotent` failures (non-mutating); loop-level
  circuit breaker: same `(name, args-hash)` failing 3× → inject "stop retrying, choose another
  approach" system note + count toward budget. Use the existing `idempotent` bit — otherwise it is
  dead metadata.

### G-08 [LOW-MEDIUM] Compaction blind spots: preservable fields never filled; gateway usage invisible to loop budget

- **Where:** `runtime/compaction.py:222-246` populates only `files_touched/open_todos`;
  `decisions/failed_approaches/user_constraints` stay empty, yet `_COMPACTION_SYSTEM_PROMPT`
  (30-38) promises all five. `Compactor._summarize_chunk` calls `gateway.complete` whose usage
  lands in `gateway._total_usage`, not `loop.budget.tokens_used` (compare `loop.py:287-293`).
- **Impact:** summaries systematically lose rationale/failures/constraints — the highest-value
  recall content; long sessions under-report spend and can overshoot `max_tokens` (compaction
  calls are invisible to `check_ceiling`).
- **Fix:** extract decisions (`decided|decision|chose`), failures (`failed|didn't work|reverted`),
  constraints (first-person user sentences, verbatim, capped) in `_extract_preserved`; after each
  summarize call, `budget.record_usage(response.usage)`; add test asserting budget delta > 0.

### G-09 [LOW] `supports_tools` ignores provider-level capability; caller model-id may be empty

- **Where:** `models/limits.py:426-428` uses `lookup(model_id)` only; `build_agent` may pass
  `resolved_model=""` when resolution yields no id (`agent.py:205-211`); `OpenAICompatibleProvider`
  carries `tool_use` per-endpoint (`resolve.py:161-168`) but `supports_tools` never consults it.
- **Impact:** empty/unknown ids get `supports_tools=True` default and full schemas to endpoints
  that reject them (extra round trip through `diagnose no_tools`, non-fatal but wasteful).
- **Fix:** `supports_tools(model_id, provider_id=None, tool_use_hint=None)`; fall back to provider
  default + `ConstraintCache` observed value; log when id empty.

### G-10 [LOW] Token accounting is heuristic and inconsistent across layers

- **Where:** `core/budget.py:145-154` (`/3.2` code vs `/4.0` prose ×1.05) vs `loop.py:1166`
  (×1.05 + 32) vs `gateway.py:626-640` (same heuristic, different floor) vs provider-reported
  usage (exact). `check_warning` exists (`budget.py:101-111`) but the loop never calls it (only
  `_emit_context_usage` ratios).
- **Impact:** preflight/shrink/compaction thresholds disagree by ~5-10%; borderline TPM requests
  flap between tiers across turns.
- **Fix (incremental):** centralize one `estimate_request_tokens` in `core/budget.py`, use
  provider-reported usage to calibrate a per-model fudge factor in `ConstraintCache`; wire
  `check_warning` into TURN_START events. Full fix (exact tokenizer per family) is a larger project
  — file as backlog, not this sprint.

### G-11 [LOW] Synthesis failure is silent; phased runs can end with narration soup

- **Where:** `loop.py:762-767` (synthesis error → `return ""`), `loop.py:354-368` (falls back to
  `_finish(MAX_ITERATIONS)` with concatenated `_response_parts`).
- **Impact:** on Groq-class models (the weakest, most needing the synthesis), a failed final pass
  yields stitched partial notes instead of a coherent answer, with no error surfaced.
- **Fix:** on synthesis failure, `_finish(MAX_ITERATIONS, error=explain(...), final=combined)` so
  callers can distinguish; emit `TURN_FAILED`-style event; consider one retry at MINIMAL tier.

### G-12 [LOW] `EventBus.emit` has no timeout — one slow handler stalls the agent

- **Where:** `core/events.py` gather (prior review M9); loop awaits emit on the hot path
  (`TOOL_START` per call at `loop.py:1028`, `TURN_START` per iteration at `loop.py:479`).
- **Impact:** a slow memory-provider recall or logging handler blocks tool execution visibly.
- **Fix:** `wait_for(gather, 5.0)` with per-handler timeout + warn; make memory recall go through
  the same path as tools (already has its own try/except in `agent.send`, `agent.py:120-134` —
  extend the pattern).

### G-13 [INFO] Checkpoint/rewind implemented but unwired; team patterns untested in hot path

- **Where:** `runtime/checkpoint.py` (no callers); `orchestration/team.py/blackboard.py/budgets.py`
  (no loop references).
- **Impact:** safety feature (pre-destructive snapshot) and scaling feature (team fan-out) exist as
  library code without product integration — fine for now, but don't cite them as mitigations.
- **Fix:** wire `auto_checkpoint_before_destructive` into the shell gate for
  `DESTRUCTIVE`/`WRITE_REMOTE` classes first; add one `team.py` integration test driving
  `SubagentRunner` with a fake engine before advertising teams.

---

## 16. Study kit — analogies, trace exercise, Q&A, glossary

### 16.1 One-analogy-per-component (for recall under pressure)

| Component | Analogy | Why it sticks |
|---|---|---|
| `AgentLoop.run` | Shift supervisor's checklist | Decides when the shift ends (5 closings), not how each chore is done |
| `ContextAssembler` 9 slots | Packing a carry-on with fixed pouches | Order fixed so airport security (prompt cache) waves you through |
| Pressure ladder L0–L7 | Airline baggage sizer | Leaner bag, same trip; at L7 you wear everything (system + recent only) |
| Shrink tiers | Phone on low-battery mode | Same apps, dimmer screen → fewer tools → shorter replies |
| `Compactor` | Intern summarizing last week's notebooks | Keeps files/decisions/TODOs, shreds the rest; append-only (never erases) |
| Phased rollup | Standup notes on a whiteboard | Erase details, keep "done / doing / blocked"; final memo at the end |
| `ToolHost` gate | Nightclub bouncer with a list | Classify (who are you) → policy (are you on the list) → approval (call the manager) |
| `ToolRegistry.dispatch` | Kitchen pass | Validates the ticket (Pydantic), never throws the plate (envelope) |
| `_repair_tool_json` | Auto-correct for drunk texts | Fences → dictionary → repair shop → close the brackets → give up gracefully |
| `SlidingWindowRatePacer` | Bouncer counting heads in the last 60 s | Sleeps *you* before the fire marshal (429) does |
| `ConstraintCache` | Scar tissue | Remembers only smaller limits ("only remember smaller" = monotonic) |
| `diagnose` | ER triage nurse | Reads the provider's scream and writes "TPM, needs smaller request" on the chart |
| `temperature=None` | Oven with no dial connected | Knob exists (config), wire cut (loop never sends it) |
| No verifier | Driving test with no examiner | Handbook says "check mirrors"; nobody checks |

### 16.2 Trace exercise (do this with a debugger or prints)

Setup: `FakeGateway([tool_turn("read", {path}), text_turn("done")])` (pattern from
`tests/unit/test_agent_loop.py:65-107`), `make_host` + `make_loop`, `await send(loop, "read it")`.

Predict, then verify each:

1. How many `gateway.calls`? (2 — one per iteration.)
2. What is `calls[0]["tools"][0]`'s key for the schema — `parameters` or `input_schema`?
   (`input_schema` — `schema.py:111-115`. Both providers read it.)
3. Where does the `read` result appear in `calls[1]["messages"]` — which role, which block type?
   (USER message, `ToolResultBlock` — `loop.py:341-346`.)
4. What are `loop._iteration`, `budget.turns_used`, `budget.tool_calls_used` at the end?
   (2, 2, 1 — iterations/turns per stream, tool_calls per execution.)
5. Termination reason and `final_response`? (`NO_TOOL_CALLS`, "done" — `loop.py:333-336`.)
6. Now change the fake to yield `MessageStop(MAX_TOKENS)` with a `{"_raw": …}` input: what happens
   to `max_output_tokens`? (Doubles, ≤16,384; assistant msg popped; turn re-streamed —
   `loop.py:306-325`.)
7. Set `model_id="openai/gpt-oss-20b", provider_id="groq"` and 8 fake tool turns: when does
   `_should_rollup` first return True, and what does `_conversation` look like after?
   (`> max_carry_tokens` ≈ 3 k tokens; collapses to `[task, last_2]` — `loop.py:660-688`.)

### 16.3 Self-test Q&A (cover the page, answer aloud)

1. **Q:** Why does the loop keep `ToolHostProtocol`, not `ToolRegistry`? **A:** So the permission
   gate, context construction, and deferral are inseparable from execution (`loop.py:115-121`;
   `toolhost.py:1-26` names the five gaps the seam closes).
2. **Q:** Who owns cross-turn history, loop or agent? **A:** Agent (`agent.py:149`);
   loop is per-request (`loop.py:223-240` resets).
3. **Q:** What three things must fit in `tpm_allowance`, and who measures them? **A:** system +
   tools + output reservation; `_estimate_request_tokens` (`loop.py:1128-1166`) + `fit_request_size`
   (`limits.py:355-382`).
4. **Q:** Name the two places `input_schema` is produced/consumed. **A:** Produced
   `schema.py:111-115`; consumed OpenAI `openai.py:171-174`, Anthropic natively.
5. **Q:** How does a cancelled run stay resumable? **A:** Unanswered calls get placeholder results
   (`loop.py:996-1002`); resume drops dangling `tool_use` (`serialize.py:190-217`).
6. **Q:** What is the *only* network fast-path in the gate, and why does it exist? **A:**
   `web_search`/`fetch` bypass approval outside read-only (`toolhost.py:524-532`) — otherwise every
   web lookup looked frozen.
7. **Q:** Why do reads never defer? **A:** Exact-match edits need the full body
   (`toolhost.py:77-80`; test `test_reads_are_never_deferred`).
8. **Q:** What does `yielded_any` protect against? **A:** Retrying a partially-streamed response
   would duplicate output (`gateway.py:371-374`).
9. **Q:** Where is the model's "plan"? **A:** Nowhere as an object — `todo` tool state +
   scratchpad notes + `_compacted_summary`, rendered into slot 5 each turn (`agent.py:84-99`).
10. **Q:** What single line would you change to make edits deterministic-ish? **A:** Thread
    `temperature` from config into `loop._stream_response → gateway.stream → providers`
    (G-04) — then set 0–0.2 for edit tasks and measure.

### 16.4 Glossary (terms as this repo uses them)

- **Turn:** one `Agent.send` → `LoopResult` (may contain many iterations).
- **Iteration:** one assemble→stream→(tools) cycle; `_iteration` counts these.
- **Phased:** TPM-bound gear with fixed per-iteration envelope + rollup + synthesis.
- **Tier (shrink):** input-shrinking stage 0–5, orthogonal to pressure L0–L7.
- **Envelope:** `ToolEnvelope` (tool) / `ResultEnvelope` (subagent) — never-raise result wrapper.
- **Gate:** classify → hooks → policy → approval chain in `ToolHost`.
- **Rollup:** extractive fold of old turns into `_compacted_summary` (phased gear).
- **Compaction:** LLM summarization of head-of-history at 0.82 fill (either gear).
- **Synthesis:** final no-tools pass producing the user-facing answer (phased gear only).
- **Deferral:** bulky tool output → scratchpad ref + 24-line headline.
- **Grant/signature:** persisted approval (`shell:<program>:<risk>` scoping).
- **Binding constraint:** `tpm | window | none` — tightest ceiling for this prompt.

---

## Sources — every claim above traces to these files

`runtime/loop.py`, `runtime/agent.py`, `runtime/context.py`, `runtime/prompt.py`,
`runtime/toolhost.py`, `runtime/compaction.py`, `runtime/shrink.py`, `runtime/stream.py`,
`runtime/serialize.py`, `runtime/bootstrap.py`, `runtime/session.py`, `runtime/checkpoint.py`,
`runtime/scratchpad.py`, `models/gateway.py`, `models/limits.py`, `models/diagnose.py`,
`models/normalize.py`, `models/resolve.py`, `models/providers/anthropic.py`,
`models/providers/openai.py`, `models/providers/google.py`,
`models/providers/openai_compatible.py`, `tools/base.py`, `tools/registry.py`,
`tools/builtins.py`, `tools/schema.py`, `tools/shell.py`, `tools/task.py`, `tools/web.py`,
`tools/search.py`, `permissions/classify.py`, `permissions/policy.py`,
`orchestration/subagent.py`, `orchestration/team.py`, `core/budget.py`, `core/events.py`,
`core/config.py`, `tests/unit/test_agent_loop.py`, `tests/unit/test_iteration_phases.py`.

*End of guide. Next step suggested: pick one gap from §15 and implement it with a test —
G-02 (STOP_HOOK, smallest) or G-04 (temperature threading, most educational for determinism) —
then re-run `pytest tests/unit/test_agent_loop.py tests/unit/test_iteration_phases.py -q`.*
