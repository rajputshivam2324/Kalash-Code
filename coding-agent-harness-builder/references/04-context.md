# 04 — Context Engineering

## Contents
1. Prompt layout (cache-friendly)
2. Token accounting
3. Instruction files (AGENTS.md / CLAUDE.md style)
4. Reminders and dynamic injections
5. Compaction ladder
6. Summary template
7. File-state cache and re-reads
8. Retrieval and repo orientation
9. Context tests

---

## 1. Prompt layout (cache-friendly)

Providers cache by **prefix** (Anthropic with explicit `cache_control` breakpoints; OpenAI and DeepSeek automatically on repeated prefixes). A stable prefix cuts cost and latency dramatically in long agent runs, so layout is an engineering decision.

```text
[1] System prompt (static, versioned)                       ← never changes within a run
[2] Tool definitions (sorted, stable order, stable JSON)    ← never changes within a run (don't add/remove tools mid-run)
[3] Instruction files (user, repo root, nested as needed)   ← changes rarely; load once at run start
[4] Environment block (cwd, OS, git branch, date)           ← snapshot at start; do NOT put timestamps that change per turn here
[5] Conversation history (append-only)                      ← only grows; edits invalidate cache from the edit point
[6] Volatile tail: reminders, todo state, budget notes      ← appended as a late user message, kept small
```

Rules:
- **Append-only history.** Mutating earlier messages (e.g. replacing old tool results) busts the cache from that point. Do compaction in batches (rarely, big), not continuously.
- Deterministic serialization: sorted keys, no random ids in prompt text, no set iteration order.
- Put a cache breakpoint after [3] and one near the end of history (Anthropic); for automatic-cache providers just keep the prefix identical.
- Track `cache_read_tokens / input_tokens` per run; alert when the hit rate drops (usually a nondeterminism bug).
- Switching models mid-run invalidates the cache; route by *task*, not per turn.

## 2. Token accounting

- Use provider counting endpoints or local tokenizers (tiktoken-compatible, model-specific tokenizers for DeepSeek/local). Keep a **safety margin** of 5–10% because local counts drift.
- Maintain a ledger: `system`, `tools`, `instructions`, `history_text`, `history_tool_results`, `images`, `reasoning`. Log it at every `ModelRequested`; it explains most cost surprises.
- Reserve output budget: `available_input = context_window − max_output − margin`.
- Reasoning models: reasoning tokens count toward output limits and cost; set `max_output` high enough that reasoning does not starve the visible answer, and track `reasoning_tokens` separately.

## 3. Instruction files

Discovery order (concatenate, most specific last so it wins):
1. Managed/org file (read-only to users)
2. User-level: `~/.harness/AGENTS.md`
3. Repo root `AGENTS.md` (also accept `CLAUDE.md`, `.cursorrules`, `CONTRIBUTING.md` hints via config)
4. Nested directories: load `dir/AGENTS.md` **lazily** when the agent first reads/edits a file under `dir/` (inject as a system-reminder next to that tool result; keeps the base prefix stable)

Features: `@path/to/file` includes (depth-limited, in-repo only), size cap (e.g. 32 KB total; warn beyond), content hashed into the run fingerprint. Provide an `init` command that drafts one from repo inspection (build/test commands, layout, conventions).

Security: repo instruction files are **untrusted-ish**. They may guide behavior, but they cannot change permissions, approve tools, or disable the sandbox (see `08-security.md`).

## 4. Reminders and dynamic injections

A small, typed mechanism for harness → model nudges, always as `Message(role="user", meta={"origin":"reminder"})` appended late (never edit the system prompt mid-run):

| Reminder | Trigger |
|---|---|
| budget low | <20% iterations/tokens/time left |
| todo state | after compaction; every N tool calls if list non-empty |
| plan-mode reminder | each turn while in plan mode ("do not modify files") |
| file-changed-externally | file-state cache sees mtime change from outside the agent |
| loop warning | `LoopDetector` rung 1 |
| gate failure | completion gate failed |
| nested instructions | first touch of a directory with its own instruction file |
| tool denied guidance | after policy deny, once |

Wrap in tags (`<harness-reminder kind="budget">…</harness-reminder>`) so models can tell them from user intent, and strip from user-visible transcripts.

## 5. Compaction ladder

Escalate only as needed; each rung is logged as `ContextCompacted{level, before_tokens, after_tokens}`.

| Level | Trigger (of usable window) | Action | Cost |
|---|---|---|---|
| L0 | at insertion | Truncate tool results (30 KB default; shorter for noisy tools), artifact the rest | free |
| L1 | > 60% | **Clear stale tool results**: for results older than the last K=10 tool turns, replace body with a stub `"[output omitted: 4.2KB grep result; re-run to view]"`. Keep the tool_use blocks (the model sees what it did). Batch this: do it once, then not again until the next threshold | free, cache bust once |
| L2 | > 80% | **Summarize**: a (cheap/fast) model call produces a structured summary of the transcript prefix; replace that prefix with the summary + the last N turns verbatim | 1 model call |
| L3 | > 92% or provider `ContextOverflow` | Hard reset: new context = system + tools + instructions + **state document** (task, constraints, todo, changed files, test status, decisions) + last 3 turns | 1 model call |

Rules:
- Never compact **inside** an unfinished tool-use exchange (keep tool_use/tool_result pairs adjacent and intact).
- Always preserve verbatim: the original user task and later user messages, explicit constraints, the latest todo list, and the current plan.
- Run compaction with a *different system prompt* (summarizer role); tools disabled; temperature 0; output capped.
- Verify the summary: cheap checks (contains all file paths from `changed_paths`, mentions last test result, length < cap). On failure, fall back to L1 only or keep more verbatim turns.
- After compaction, **reset the file-state cache** or mark entries stale so the model must re-read files before editing (it no longer has the text).
- Keep the pre-compaction transcript in the event log; compaction changes the *prompt*, not history.

## 6. Summary template

```markdown
## Task (verbatim from user)
…
## Constraints & preferences (verbatim where possible)
- …
## Current plan / todo
- [x] … [ ] …
## Progress so far
- Explored: <paths + one-line findings>
- Decisions & rationale: <decision> — <why> — <rejected alternatives>
- Failed approaches (do not repeat): <approach> — <why it failed>
## Code state
- Files changed (authoritative): path (+a −b) — purpose
- Key facts about code (signatures, invariants, gotchas the next turns need)
## Verification state
- Last test/lint/build command, exit code, failing tests (names), at which point
## Open issues / next steps
1. …
```

Section order puts the highest-value, hardest-to-recover items first. The `changed_paths`, test status and todo are filled **by the harness from state**, not by the model (avoids hallucinated summaries).

## 7. File-state cache and re-reads

Per-run map `path → {mtime_ns, size, sha256, ranges_read, read_at_event}`:
- Enables read-before-edit and stale-edit errors (`02-tools.md` §4).
- Dedupe unchanged re-reads with a stub.
- External change detection: before each model call, `stat` files in the cache; on mismatch inject a `file-changed-externally` reminder (catches user edits in their IDE, formatters, codegen).
- After compaction, entries are invalidated (§5).

## 8. Retrieval and repo orientation

Order of investment (stop when evals plateau):
1. Good `grep`/`glob`/`read_file`/`list_dir` tools + instruction file (what Codex and Claude Code rely on).
2. A compact **repo map** at run start (top-level tree + key files + build/test commands detected from manifests), ≤ 1.5k tokens.
3. Symbol index (tree-sitter) with a `find_symbol` tool; Aider-style ranked repo map (graph ranking of definitions/references) for large repos.
4. Embedding search as a *tool* (not auto-injected) for natural-language-to-code lookups; keep the index incremental and committed-state aware.

Never auto-stuff retrieved chunks into the prompt without provenance; present them as tool results with paths and line numbers.

## 9. Context tests

- 300-step synthetic transcript in a 32k window: run completes; constraints from step 1 still obeyed at step 300 (check with a canary constraint such as "never modify `legacy/`" and attempt to violate it late).
- Compaction never splits a tool_use/tool_result pair (property test).
- After L3, the agent resumes correctly: state document alone is sufficient to continue a seeded task.
- Cache hit rate in a 40-turn run ≥ 80% on providers that support caching.
- Summary fidelity: for seeded facts (file names, test names, a decision), the summary contains them (measured over 50 transcripts).
