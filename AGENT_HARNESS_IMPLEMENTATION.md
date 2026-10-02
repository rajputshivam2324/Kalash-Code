# Agent Harness — Core Loop: Detailed Implementation (Sep 2026)

> Scope: only the harness — loop, prompt, context, tools seam, model gateway, guards, persistence.
> Style: candid points, no essays. Every bullet traces to a real `file:line`.

---

## 1. Summary — what the thing is

- Single ReAct loop: `assemble → stream → tools → repeat` until model stops calling tools.
- Lives in `src/kalash/runtime/loop.py:195-376` (`AgentLoop.run`). Everything else feeds it.
- One loop, one conversation. No planner agent, no supervisor. Planning = prompt text + `todo` tool.
- Subagents = just a `task` tool call. Child runs isolated loop, parent sees only summary string.
- Limit-aware by design: every request preflighted against TPM/context/tool caps before sending.
- Permission gate is the only path to tools. No backdoor `registry.dispatch` from loop.
- Tools never raise. Everything becomes text the model reads (`ERROR …` / `REFUSED …`).
- Context has 4 overflow defenses, not 1: pressure ladder, shrink tiers, LLM compaction, phased rollup + synthesis.
- No auto-verifier. Prompt says "verify", code never runs pytest/ruff/mypy itself.
- No determinism. `temperature` parsed, never sent. No seed/top_p anywhere.

---

## 2. Why we built it like this — Sep 2026 reality

- **Groq free-tier trauma:** 8k TPM bills `prompt + max_tokens`. Our 5k prefix + 8k reservation = instant 413. Hence: `limits.py`, TPM pacer, shrink tiers, phased iterations, compact prompt. Not theory — three hard failures quoted in `models/limits.py:1-22`.
- **Multi-provider chaos:** Anthropic / OpenAI / Gemini / Groq / Together / local all differ on tool format, output caps, context windows, 429 shapes. Hence: `ModelGateway` + `normalize.py` + per-family adapters + `diagnose.py`. One loop, N wire formats.
- **Prompt caching = money:** Anthropic bills full input every turn unless breakpoints set. Hence: fixed tool order (`builtins.py:37-65`), fixed 9-slot order (`context.py:47-58`), explicit `cache_control` (`providers/anthropic.py:286-307`), schema minification (3888 → ~small, `tools/schema.py:1-19`).
- **`rm -rf` fear:** agent with shell = liability. Hence: classify → 6-stage policy → approval → sandbox (Landlock/bwrap/Seatbelt). Fail-closed filesystem (`fs.py`), plan-mode capability filter, grant scoping (`shell:<program>:<risk>`).
- **Context explosion:** real repos blow 200k windows in ~15 turns of shell output. Hence: 6KB deferral to scratchpad, 12k trim, 0.82 compaction, L0-L7 pressure drops. Drop is cheaper than summarize; summarize is cheaper than fail.
- **Resume must not poison provider:** unbalanced `tool_use` without `tool_result` = provider rejects next request forever. Hence: pairing guarantee on cancel (`loop.py:988-1003`) + dangling-drop on resume (`serialize.py:190-217`) + append-only compaction rows.
- **TUI + headless + CI, one core:** same `build_agent` serves interactive worker, `kalash -p`, piped stdin, cron daemon, SDK import. Hence: `agent.py:177-360` switchboard, `bootstrap.py` post-wiring, `EventBus` fan-out instead of direct UI calls.

---

## 3. Market snapshot Sep 2026 + what we can update

What Claude Code / Cursor / Codex / OpenCode / Aider all converged on in 2026 (per public docs, guides, Terminal-Bench chatter):

- Hooks as deterministic enforcement (PreToolUse block/ask/allow + rewrite). We have it — keep.
- Subagents for context isolation + skills as lazy-loaded knowledge. We have both — keep.
- Checkpoints/rewind (conversation + working tree snapshot before destructive ops). We built it, never wired it — wire it.
- Verification loops (agent runs tests, reproduces bug, proves change; "Spec→Code→Deploy→Test→Verify→Ship"). We only prompt it — enforce it.
- Plan mode as capability filter + approval modes. We have it — keep.
- Multi-agent parallel runs + review agents. We have library code (`team.py`), not productized — productize or cut claims.
- Model routing / Zen-style curated lists + TPM-aware degraded toolsets. We are ahead here (phased iterations are rare in OSS) — document it as differentiator.

| Market does (Sep 2026) | We have | Verdict / update |
|---|---|---|
| ReAct single loop as default | Yes (`loop.py`) | Keep. It wins on simplicity; don't add supervisor until needed |
| Mandatory plan-then-act for multi-file | Prompt-only (`_PLANNING`) + `todo` tool | Harden: block `write/edit` until `todo` exists for multi-file tasks (cheap, high-signal) |
| PreToolUse hooks with block semantics | Yes (`toolhost.py:455-488`, `hooks/runner.py`) | Keep. Add: Stop-hook evaluation per iteration (currently dead `STOP_HOOK`) |
| Checkpoints before destructive ops | Built (`checkpoint.py:174-186`), zero callers | Wire into shell gate for DESTRUCTIVE/WRITE_REMOTE. Smallest safety win on board |
| Auto-verify (tests/lint after edit) | No — prompt only | Build `Verifier` gate before `NO_TOOL_CALLS`. Biggest eval-score lever |
| Determinism knobs (temp/seed per task) | Dead (`temperature` parsed, never sent) | Thread config→loop→gateway→providers; add `--temperature/--seed`. Needed for reproducible evals |
| TPM-aware shrinking + synthesis | Yes, best-in-class (`shrink.py`, phased gear) | Keep + market it. Nobody else does per-iteration fixed envelopes this explicitly |
| Parallel tool calls | Reads parallel (≤5), writes serial | Matches market (Cursor/Claude same split). Keep |
| Timeout per tool + global wallclock | Half (shell/task/search/web yes, rest no; wallclock untracked) | Central `wait_for` in `dispatch` + tick wallclock in loop. Prevents hung turns |
| Reasoning/thinking blocks round-trip | Yes (opaque + thinking, Anthropic sigs) | Keep. Needed for Claude extended-thinking models |
| Audit log of tool actions | Events exist, no durable audit view | Add `kalash session audit` from existing TOOL_COMPLETE payloads. Cheap |
| Background agents / cron | Daemon exists (`scheduler/`) | Keep. Connect budget caps to daemon (currently loop-local) |

---

## 4. Detailed implementation — file by file, candid points

### 4.1 Loop heart

**`src/kalash/runtime/loop.py` (1166) — THE file**

- `run()` (`195-376`): resets per-run state, `while _iteration < 50`, budget check → `_stream_turn` → usage record → persist → truncated-call retry → no-tools finish → `_execute_tools` → rollup → cancel check. After loop: phased synthesis or `MAX_ITERATIONS`.
- `_iteration` counts model turns, not tools. One iteration can hold N tool calls.
- `_finish` (`378-408`): concatenates ALL text parts. `final_response` = whole turn narration.
- `_stream_turn` (`428-560`): 6 attempts. Assemble tier → TPM preflight → TURN_START event → stream → diagnose → repair cascade (output-cap once → shrink tier → invalid-tool re-prompt).
- `_assemble_for_tier` (`574-655`): schemas → `_ensure_context_fits` → `assembler.assemble` → estimate → `fit_request_size`. Loops until fits or tiers exhausted.
- `_execute_tools` (`938-1004`): READ concurrent (`gather`, semaphore 5), WRITE/EXEC serial (lock), cancel-aware, pairing guarantee for every `tool_use_id`.
- `_execute_tool_with_hooks` (`1016-1063`): emits TOOL_START, try/except → error `ToolResultBlock`. Never raises.
- `_ensure_context_fits` (`787-831`): <0.82 no-op; ≤3 msgs → extractive + 12k trim; else LLM `Compactor` (repo) or extractive (no repo).
- `_should_rollup` / `_rollup` (`660-688`): phased only, folds to `[task, last_2]`, extractive, resets tier.
- `_synthesize_final_answer` (`714-785`): phased only, no-tools pass, `synthesized=True`. Failure returns `""` silently (gap).
- `_stream_response` (`896-932`): gateway async-gen → `StreamHandler`, no temperature param (gap).
- `cancel()` (`873-876`): sets flag + event only. Cooperative, not preemptive.
- Candid: best-engineered file in repo. Two dead bits: `STOP_HOOK` never emitted; `_task_message: Message | None` needs no other owner.

**`src/kalash/runtime/agent.py` (531) — the switchboard**

- `Agent.send()` (`107-167`): persist user msg → env detect → scratchpad + memory recall (fail-logged) → `loop.run` → `history = loop.conversation` → capture memories (fail-logged).
- `build_agent()` (`177-360`): THE wiring order — provider → session/repo/grants (fail-soft ephemeral) → registry → policy → approval → hooks → MCP → ToolHost → budget → assembler → instructions/skills → prompt tiers → AgentLoop → `host.cancel_event = loop.cancel_event` → resume history.
- `_fit_prompt` (`363-397`): full prompt fits (budget − 2048 output − minimal tools)? Else compact. Prevents stillborn requests.
- `_discover_skills` (`400-419`): names + descriptions only. Bodies lazy via `skill` tool. Cheap by design.
- `run_isolated()` (`422-496`): child entry. `persist=False` (no orphan rows), own budget (`max_turns=25`, `max_tokens`), `spawn_depth+1`, returns `{output, usage, turns, termination}` only. Parent never sees transcript (I-028).
- `set_mode()` (`73-82`): flips `host.mode` + rebuilds prompt. Plan enforced by runtime, not requested of model.
- Candid: docstring ("nothing was connected, now it is") is accurate history. `manager._ensure_engine()` private access is the one smell; acceptable.

**`src/kalash/runtime/bootstrap.py` (29)**

- `prepare_agent()`: `configure_logging()` + async MCP wiring. Called by every entry point before first turn.
- Failures non-fatal by design. Agent runs degraded, not dead.
- Candid: 29 lines doing the most thankless job. Fine.

**`src/kalash/runtime/session.py` (~140)**

- `SessionManager.create/resume`, turn-seq seeding (`loop._next_turn_seq`, `loop.py:179-189`).
- Storage failure → caller falls back to ephemeral. Session never blocks agent.
- Candid: thin, correct. Resume history rebuilt via `serialize.rehydrate_messages`, not here.

### 4.2 Context, prompt, overflow

**`src/kalash/runtime/context.py` (385) — 9-slot suitcase**

- Slots fixed 0-8: SYSTEM_IDENTITY, TOOL_SCHEMAS, SKILLS_CATALOG, ENVIRONMENT, KALASH_MD, MEMORY, COMPACTED_HISTORY, RECENT_TURNS, CURRENT_MESSAGE (`47-58`). Order = cache stability.
- Pressure L0-L7 via thresholds `(0.60…0.92)` (`26-40, 200-210`). Degradation: L2 abbreviate schemas, L3 trim old turns, L4 drop memory (8%→3%), L6 drop skills (5%→0%).
- `_emit_messages` inserts literal `<cache_breakpoint/>` after slots 2,4 (`323-339`). Stripped later by `split_system`. Hack, works.
- Candid: textbook context engineering. Only complaint: pressure input mixes prompt-estimate vs budget-usage depending on caller — pick one.

**`src/kalash/runtime/prompt.py` (982) — employee handbook**

- 12 sections: IDENTITY, REASONING, DOCTRINE, TOOL_DISCIPLINE, PLANNING (mandatory `todo` + ReAct), CONTEXT (scratchpad refs), MEMORY, FEW_SHOT A-H, VERIFICATION, COMMUNICATION, SAFETY (untrusted-data + no secret echo), BUILD/PLAN mode.
- Full ≈3.9k tokens. Compact keeps 7 (drops reasoning/doctrine/few-shot/verification/communication) (`911-921`).
- `discover_project_instructions` (`727-772`): global → ancestors (≤12 deep), `KALASH.md` > `AGENTS.md`, 12k chars cap each. Nearer wins. Injected last = lowest priority among system content.
- `detect_environment` (`820-872`): cwd/platform/python/git branch+dirty count. Kept OUT of static prompt for cache stability.
- Candid: `_VERIFICATION` + few-shot D are the entire "verifier". Good writing, zero enforcement. Prompt can't do what code won't.

**`src/kalash/runtime/shrink.py` (58) — battery-saver table**

- Tiers 0-5: NORMAL → STANDARD_TOOLS → MINIMAL_TOOLS → COMPACT_PROMPT → DROP_MEMORY → DROP_PROJECT_CONTEXT (`15-45`).
- Pure data + `tier_flags/next_tier`. Loop owns the walking logic.
- Candid: smallest file, highest leverage on Groq. Perfect as-is.

**`src/kalash/runtime/compaction.py` (331) — librarian**

- Fires at 0.82 (`24`). Depth ≤3 (`27`). Prompt preserves files/decisions/TODOs/failures/constraints (`30-38`).
- `compact()` (`93-143`): COMPACT_START → `_extract_preserved` → hierarchical chunk-summarize (20 msgs/chunk via `gateway.complete`) → build → persist NEW row (never UPDATE) → COMPACT_COMPLETE. Exception → `_fallback_summary` (first3+last3).
- Reality check: `_extract_preserved` only fills files + TODOs (`222-246`). Decisions/failures/constraints promised, never extracted. Summarizer usage invisible to loop budget.
- Candid: correct architecture, half-finished extraction. Fill the three empty fields before touching anything else here.

**`src/kalash/runtime/stream.py` (302) — drunks-to-JSON**

- `_repair_tool_json` (`33-88`): fences → `json.loads` → `json_repair` → brace-closing → `{"_raw": raw}`. Unrepairable never crashes; sentinel drives loop's double-budget retry.
- `_BlockAccumulator` + `StreamHandler.feed` (`212-302`): `match` on 7 event types, unknown block-delta index only warned.
- `StreamResult` (`142-170`): `tool_calls`, `has_tool_calls`, `text_content` properties. Loop branches on these.
- Candid: repair pipeline is why Groq malformed-args don't kill turns. Keep. Log line on repair is the right observability.

**`src/kalash/runtime/serialize.py` (242) — resume read path**

- `serialize/deserialize_blocks` with `FORMAT_VERSION=1`. Thinking sig dropped (not portable across model switch — deliberate).
- `rehydrate_messages` (`158-187`): ordered rows → merge same-role (provider rejects adjacent same-role) → skip system (rebuilt fresh) → `_drop_dangling_tool_calls` (unanswered `tool_use` removed or provider rejects resume forever).
- `split_system` (`220-242`): joins SYSTEM texts, drops `<cache_breakpoint/>` markers.
- Candid: the file that makes `resume` actually work. Dangling-drop is the kind of bug you only learn from production.

**`src/kalash/runtime/scratchpad.py` (577) — overflow parking lot**

- `get_scratchpad(session).put(kind, headline, content)` content-hash deduped. `render_notes/render_index`, `expand(ref[, grep/offset])` backend.
- Deferral target for bulky shell/search/fetch/glob/list output (see toolhost).
- Candid: durable working state done right. `plan` first, never dropped — prevents mid-build amnesia.

**`src/kalash/runtime/checkpoint.py` (302) — built, unwired**

- `CheckpointManager.create/rewind/auto_checkpoint_before_destructive`: msg-seq + content-addressed file manifest (blobs), git-tracked files or 1000-file walk fallback, per-session JSON meta.
- Zero callers in loop/tool path. `auto_checkpoint_before_destructive` is the saddest function in the repo: perfect, invoked by nobody.
- Candid: wire it into DESTRUCTIVE shell gate first. Don't build more until this is called.

### 4.3 Model layer

**`src/kalash/models/gateway.py` (640) — phone company**

- `ProviderProtocol` (`52-88`): `complete/stream`, `temperature` passthrough, capabilities/pricing. All adapters satisfy it.
- `classify_error` (`106-139`): CONTEXT/AUTH/TRANSIENT(429/5xx/timeout)/tool-parse/PERMANENT/UNKNOWN. TPM `Limit N` regex → `ConstraintCache.record_tpm` before retry. Order matters (AUTH before TRANSIENT or `429`-in-auth-msg misfires — prior M4).
- `SlidingWindowRatePacer` (`147-213`): 60s deque per model, sleeps (≤60s) to stay under 95% TPM. Proactive, not reactive. Why we survive Groq.
- `RetryConfig(6, 1s, 30s, 25% jitter, 180s budget)` (`260-274`). `_stream_with_retry` (`338-392`): no retry on AUTH/PERMANENT/CONTEXT; no retry after first byte yielded (`yielded_any` — prevents duplication); else Retry-After or backoff.
- `complete()` (`476-540`): per-provider `fit_request_size` clamp + pace + try chain; AUTH → next provider; CONTEXT on last → raise; all fail → `ModelAllFailedError`. `stream()` (`542-597`): same but yields `StreamError(all_failed)` instead of raising.
- No per-request deadline. Relies on SDK defaults + 180s retry budget.
- Candid: transport layer is production-grade. Missing: wallclock contribution + cost accumulation passthrough.

**`src/kalash/models/limits.py` (445) — the knowledge**

- Constants: `DEFAULT_MAX_OUTPUT=4096`, `MIN_USABLE_OUTPUT=1024`, `TARGET_OUTPUT_TOKENS=2048`, `TPM_REQUEST_SHARE=0.9` (was 0.75, harmful — comment at `42-52` explains why).
- `IterationBudget` (`60-84`): phased envelope for TPM models (output ≤ allowance×0.25, carry ≤ prompt×0.45) vs generous non-phased.
- `ConstraintCache` (`112-156`): monotonic — only remembers SMALLER limits. Correct for tiered accounts.
- `_TABLE` ~25 patterns (`160-203`), longest-match wins, `normalize_model_id` strips `provider/` prefix.
- `effective_limits` (`246-283`): table → observed → provider default (groq 8k, together 6k…). `binding_constraint/tpm_allowance/fit_request_size/resolve_output_tokens/input_budget/supports_tools/describe` — each used by a distinct loop/host/gateway call site.
- Candid: standout design artifact. `supports_tools` ignores per-endpoint `tool_use` hint — small fix pending.

**`src/kalash/models/diagnose.py` (218) — triage nurse**

- `Diagnosis` (`30-61`): summary + remedy + kind + `adjust_max_output` / `reduce_input` / parsed `tpm_limit/requested_total`. `retryable` property drives loop repairs.
- 8-way classifier (`68-203`): max_output (regex cap) → tpm (`Limit X, Requested Y` + over-by math) → no_tools → invalid_tool (malformed args + hallucinated names) → context → auth → rate → quota → readable-fallback.
- Candid: turns provider screams into actionable chart notes. The loop's repair cascade is only as good as this file's regexes.

**`src/kalash/models/normalize.py` (270) — shared language**

- Frozen dataclasses: `Role/StopReason`, `Text/ToolUse/ToolResult/Thinking/Image/Memory/Opaque` blocks, `Message/ModelResponse`, 7 stream events, `ModelCapabilities(parallel_tool_use…)`.
- `OpaqueBlock` round-trips Anthropic thinking signatures without interpreting them.
- Candid: boring, essential, correct. Immutability prevents aliasing bugs across resume/rollup.

**`src/kalash/models/resolve.py` (222) — credentials → live provider**

- Order: saved `/connect` > env var. `build_provider` never raises (returns `Resolution` with reason). `build_gateway` chains primary + fallbacks.
- Notes catalog lives under `tui/` (wrong layer, lazily imported to avoid Textual in headless — comment at `13-15` admits it).
- Candid: the reason `kalash -p` respects TUI `/connect`. Move catalog to `models/` when convenient, not urgent.

**`src/kalash/models/providers/*` (anthropic 467, openai 478, google 454, openai_compatible)**

- Anthropic: system separate + `cache_control` on system + last tool (`286-307`); block-index streaming; usage normalized (input minus cache-read).
- OpenAI: system folded first; `input_schema→function.parameters` (`160-176`); assistant `ToolUseBlock→tool_calls`, results → `role:tool`; multi-call `tc_to_block_index` routing (`384-460`); `max_completion_tokens`; `stream_options.include_usage`.
- All accept `temperature` but receive None always (loop never sends — §G-04).
- Candid: adapters are faithful, not clever. OpenAI multi-call routing is the subtlest code here; don't touch without the test.

### 4.4 Tool layer

**`src/kalash/tools/base.py` (260) — contract**

- `SideEffect(NONE/READ/WRITE/EXEC)`, `ToolContext` (roots, caps, `spawn_depth/max_spawn_depth`, `allow_network` per-call, `cancel_event`, `hooks`, `can_spawn`), `ToolEnvelope(success/fail, truncated+TruncationInfo, error+remediation)`, `Tool` Protocol (all metadata + `timeout_s/max_output_bytes/idempotent/cancellable/dynamic_capabilities/execute`).
- Rule: tools never raise; envelope always returned.
- Candid: `idempotent` declared, never read by host/loop. Either enforce (retry) or delete.

**`src/kalash/tools/registry.py` (167) — kitchen pass**

- Priority builtin > plugin > MCP (`29-73`). `dispatch` (`127-167`): get → `params.model_validate` → `execute` → catch-all. Three fail codes: UNKNOWN / INVALID_ARGS (recoverable) / ERROR (not).
- No timeout wrapper. No retry. Single attempt per call.
- Candid: correct narrowness. Timeout/retry belong here (G-05/G-07), not in each tool.

**`src/kalash/tools/builtins.py` (100) — menu**

- `core_tools()` 15 in fixed cached order; `memory_tools()` 3; `task_tools()` 1. `default_registry(memory=True, task=True)`.
- Comment ("tools that cannot act are not registered") = policy: no dead schemas wasting tokens + turns.
- Candid: fixed order is load-bearing for cache. Don't alphabetize "for cleanliness".

**`src/kalash/tools/schema.py` (177) — diet plan**

- Drops `title/default/additionalProperties`, truncates descriptions (tool ≤200, field ≤100). Lossless for call validity.
- `MINIMAL_TOOLS` 9 (read/write/edit/search/shell/list/todo/web_search/fetch) — `todo` kept deliberately (no plan → doomed small-model builds), web pair kept (else "no search tool" embarrassment).
- `STANDARD_TOOLS` 13 (+glob/multi_edit/note/expand). `select_profile` measures actual minified cost, picks largest fitting `budget − prompt − 2048 reserve`. Returns `minimal-overflow` instead of empty when nothing fits (honest squeeze signal).
- Candid: the file that makes 8k-TPM models usable at all. Measured, not guessed — right call.

**`src/kalash/tools/task.py` (220) — subagent as tool**

- Params: prompt, context_files, capabilities (subset-checked), timeout ≤1800 (default 300), max_turns ≤200 (default 50).
- Checks: caps ⊆ parent (`131-138`), files exist (`143-152`), `can_spawn` depth (`156-164`) → `wait_for(run_isolated…)` → envelope with `task_id/turns/child_tokens/termination` (`204-220`). Timeout/fail → `KALASH_TOOL_TIMEOUT/ERROR` (recoverable).
- Candid: isolation (I-028) is the whole point — child transcript never enters parent. Depth-in-context (not global) is the right bounding.

**`src/kalash/tools/shell.py` (545) — most dangerous, most careful**

- `command/cwd/timeout≤1800/background`. setsid process groups, `_safe_env` strips 13 vars, `_cwd_allowed` under roots, `_wrap_sandboxed` (Landlock/bwrap/Seatbelt, warning on unwrap).
- `_run_with_timeout` + `_collect_stream`: 128 KiB/stream live cap, 0.25s poll, cancel-aware, `TOOL_OUTPUT` live events. SIGTERM → 5s → SIGKILL whole group.
- Background registry `_BACKGROUND[session][pid]`, killable via `terminate_session_backgrounds`.
- Candid: exemplary. The `bash -c` is intentional (documented), gate is the guard. `sandbox_warning` in prompt (`_TOOL_DISCIPLINE`) closes the loop.

**Rest of tools (brief, same contract):** `fs.py` (1072: atomic writes, fail-closed roots, NFC + protected paths), `search.py` (`wait_for` timeout ✓), `web.py` (fetch 5MiB/30s httpx ✓, untrusted wrapping), `todo.py` (plan state backing `scratchpad_blocks`), `scratch.py` (`note/expand` — deferral backend), `skill.py` (lazy body load), `memory_tools.py` (recall/remember/forget; no timeout enforcement ✗). Pattern: each declares `timeout_s/max_output_bytes`; only shell/search/task/web enforce.

### 4.5 Guardrails (part of harness hot path)

**`permissions/classify.py` (586) + `policy.py` (393)**

- `classify_tool_call` → `ToolRisk(paths/hosts/command/risk_class/confirmation/reversibility/summary)`.
- 6 stages: deny → protected → grants → sandbox → approval → confirmation-classes (stage 6 only ADDS prompts, never bypasses).
- Decisions ALLOW/DENY/ASK. `RiskClass`: READ/WRITE/WRITE_REMOTE/NETWORK/DESTRUCTIVE.
- Candid: stage order + "only ADD" rule is clearly reasoned. Tested to the teeth (`test_agent_loop.py:266-379`).

**`hooks/runner.py` (407) + `events.py` + `load.py` + `trust.py`**

- Types command (stdin JSON) / http (POST) / python (in-process). Exit 0 = proceed, 2 = block, else non-blocking error. Depth-1 loop protection.
- Loop integration today: SESSION_START/END (`loop.py:242-254, 386-396`), Pre/PostToolUse per call (`toolhost.py:455-510`). Stop-hook unevaluated (gap).
- Candid: matches Claude Code hooks semantics. Subprocess hooks inherit env unsandboxed (prior M5) — strip env or sandbox them.

**Sandbox (`sandbox/manager.py`, `linux.py` 384, `macos.py`, `policy.py`)**

- Linux Landlock (5.13+) → bwrap fallback; macOS Seatbelt. `wrap(argv)` returns (argv, wrapped, warning). Never raises; degradation surfaces in `doctor` + `sandbox_warning` metadata.
- Prior HIGH: x86_64 syscall numbers break ARM64 silently. Fix with `platform.machine()` branch.
- Candid: defense-in-depth behind gate, not substitute. Warning propagation into prompt is the right touch.

**`storage/engine.py` (142) + `repositories/sessions.py`**

- WAL + `busy_timeout=5000` + `synchronous=NORMAL` + single-writer `BEGIN IMMEDIATE` queue. Right SQLite recipe.
- Prior MEDIUM: single `check_same_thread=False` conn shared across `asyncio.to_thread` + sync with two different locks. Unify locks or adopt `aiosqlite`.
- Sessions repo: turns/messages with seq, `next_turn_seq` for resume continuity, `get_session_messages` ordered for rehydrate.
- Candid: persistence never blocks agent (ephemeral fallback). Correct priority.

### 4.6 Budgets, events, config + consumers (you didn't ask, but harness)

**`core/budget.py` (154)**

- `BudgetState`: tokens/cost/wallclock/turns/tool_calls/spawn ceilings + reservations for children (`reserve_for_child/release_reservation`). `check_ceiling/check_warning`, `record_usage` (Decimal cost, needs `pricing` — loop never passes it, so cost stays 0).
- `estimate_tokens`: len/3.2 code, /4.0 prose, ×1.05. Conservative, imprecise, everywhere.
- Loop increments turns (`loop.py:285`), tool_calls (`loop.py:1036`), tokens (`loop.py:287-293`). Wallclock never incremented (gap).

**`core/events.py` (166)**

- 23 types: TURN_START (with binding/shrink/phased/caps — best debug event), TURN_COMPLETE, TOOL_START/COMPLETE/DENIED/OUTPUT, AGENT_SPAWN/COMPLETE, COMPACT_START/COMPLETE, BUDGET_WARNING/SOFT_LIMIT/EXCEEDED, SESSION_START/END.
- `emit` gathers per-handler tasks, no timeout (one slow handler stalls loop — gap). Errors isolated per handler (good).

**`core/config.py` (278)**

- 6 layers: defaults → `~/.kalash` → `.kalash/settings.json` → `settings.local.json` → `KALASH_*` → CLI. `SECURITY_KEYS` user-scope-only (project file claiming `danger-full-access` ignored + warned).
- `ModelConfig.temperature` parsed, never consumed (gap). Silent `{}` on bad JSON (should warn in `doctor`).

**Consumers (same core, three faces):** `cli/_pipe.py` (249: `-p`/stdin → `build_agent` → `OutputSink` text/json/stream-json, typed exit codes 3/4…) — same loop as TUI; `tui/app.py` (1652: Textual worker, approval dialogs, TOOL_OUTPUT live render, `/resume /models /status`); `sdk/__init__.py` (re-exports); `scheduler/daemon.py` (cron → headless runs). Memory: `Agent.send` recall (`memory/session.py`, limit 12) + capture (`memory/capture.py`) both fail-logged, never fatal.

**`orchestration/subagent.py` (261) + `team.py` (422) + `blackboard.py` + `budgets.py`**

- `SubagentRunner.spawn`: depth check, ancestor-cycle check (agent+prompt-prefix key), `$0.50` default reservation, DB run-record, isolated `run_isolated`, envelope-only return, reservation release. Team: semaphore-8 fan-out/fan-in.
- Hot path uses `task.py→run_isolated`, NOT `SubagentRunner`. Team/blackboard unreferenced by loop. Library, not product — don't cite as mitigation.

**`evals/` + `tests/unit/test_agent_loop.py` (579) + `test_iteration_phases.py` (149)**

- Fake-gateway proof "tool call creates real file", gate matrix (10 tests), deferral/category/serialize/resume tests. Phased math + rollup + synthesis tests.
- `evals/harness.py + suites.py + project_build.py`: capability/policy/token suites + golden-file builds. Extend with TPM-preflight + determinism (same prompt × N) suites.

### 4.7 Small files you missed (still harness)

- `core/ids.py` (`ses_/run_/tsk_/chk_` prefixes — grep-able, stable).
- `core/errors.py` (269: `KalashError(code=KALASH_…)` taxonomy; headless matches on codes, not strings).
- `core/redact.py` (196: secret patterns; note quadratic loop — prior M1).
- `core/paths.py` (`kalash_home()`, `temp_dir_for_session()` — writable-roots second member).
- `storage/blobs.py` (content-addressed checkpoint bodies).
- `mcp/load.py + client.py` (471) + `registry.py`: MCP tools join registry as `source=mcp` (lowest priority), wired post-build, fail-soft.
- `skills/loader.py` + `agents/loader.py`: catalog discovery (names-only into prompt; bodies lazy).
- `runtime/session.py` vs `storage/repositories/sessions.py`: manager (lifecycle) vs repo (rows) — don't confuse.

---

## 5. What to build next (priority order, no fluff)

1. **Verifier gate (HIGH):** post-edit checks via shell sandbox, block `NO_TOOL_CALLS` while red. Biggest eval + trust win.
2. **Wire checkpoints (LOW effort, HIGH safety):** `auto_checkpoint_before_destructive` in shell gate. Half-day work.
3. **Thread temperature/seed (MEDIUM):** config→loop→gateway→4 providers + CLI flags. Unblocks reproducible evals.
4. **Central timeout + wallclock (MEDIUM):** `wait_for` in `dispatch`, tick wallclock in loop, pass pricing into `record_usage`.
5. **STOP_HOOK or delete (SMALL):** evaluate per iteration or remove enum + docstring. Dead controls lie.
6. **Host once-retry + identical-call breaker (MEDIUM):** use `idempotent` bit; 3× same-call fail → force new approach.
7. **Compaction extraction (SMALL):** fill decisions/failures/constraints; record summarizer usage into budget.
8. **ARM64 Landlock + SQLite locks + hook env strip (known HIGHs):** from prior review; unchanged.

*End. If you implement in this order, each step is independently shippable and test-covered by the existing fake-gateway pattern.*
