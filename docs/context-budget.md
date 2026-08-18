# Context Budget

> **Status:** normative.
>
> The context window is the scarcest resource in Kalash and every subsystem wants a share of it. This
> document is the allocator: the assembly order, the reserves that are never negotiable, the priority
> ordering that decides what yields under pressure, the compaction contract, and — separately — the
> money-and-time ceilings that bound a run.
>
> [`model-gateway.md`](./model-gateway.md) deliberately refuses to solve this: it raises
> `KALASH_MODEL_CONTEXT_EXCEEDED` rather than truncating, because truncation policy requires knowing
> what is safe to drop and the gateway does not. This document is the thing that knows.
>
> Inherited constraints: I-020 (memory is data, not instruction), I-021 (append-only log), I-026
> (agent runs bounded), I-029 (cancellation is real), I-033 (external content not authoritative),
> I-037 (sessions explainable). Asset at risk: A9 (money). Threat: T-J (operator error).

---

## 1. Assembly order

Nine slots, fixed order ([`CLAUDE.md`](../CLAUDE.md) §6). Static content first, volatile last.

| # | Slot | Volatility | Owner |
|---|---|---|---|
| 1 | System identity + operating contract | Static per version | `runtime/context.py` |
| 2 | Tool schemas | Static per session | `tools/registry.py` |
| 3 | Skills catalog — name + description only | Static per session | `skills/index.py` |
| 4 | Environment: cwd, git state, platform | Per session, re-resolved on IDLE→ACTIVE | `runtime/context.py` |
| 5 | `KALASH.md` hierarchy: user → project → nearest subdir | Static per session | `runtime/context.py` |
| 6 | `<memory>` block | **Every turn** | `memory/pipeline/inject.py` |
| 7 | Compacted history summary | On compaction | `runtime/compaction.py` |
| 8 | Recent verbatim turns | Every turn | `runtime/context.py` |
| 9 | Current user message | Every turn | `runtime/context.py` |

Cache breakpoint hints are emitted after slot **3** and slot **5**. The gateway translates them for
`EXPLICIT_BREAKPOINT` providers, ignores them for `AUTOMATIC_PREFIX` and `NONE`
([`model-gateway.md`](./model-gateway.md) §9); the assembler does not know which provider is in use.

### 1.1 Why the order is load-bearing, quantified

Prompt caching prices a stable prefix at a discount and everything after the first difference at full
rate. Take a 200k-window model, a 120k assembled prompt, cache reads at 10% of input price, and a
40-turn session:

| Arrangement | Cached prefix | Full-price input per turn | 40-turn input cost, relative |
|---|---|---|---|
| Correct: volatile content in slots 6–9 | ~48k (slots 1–5) | ~72k + 0.1 × 48k = 76.8k | **1.00** |
| One volatile token moved into slot 2 | 0 | 120k | **1.56** |

A single volatile token early in the prompt does not cost one token. It costs the cache discount on
every token before it, on **every subsequent request in the session**. A timestamp in the environment
block, a per-turn counter in the identity header, a tool schema that serializes a `dict` in
non-deterministic order — each is a ~1.5× bill on a long session and produces no visible symptom.

Two controls, because this failure is silent:

- **Snapshot tests** (`syrupy`) on the rendered slots 1–5 and on breakpoint positions. A prefix change
  shows up as a reviewable diff, not as a cost anomaly three weeks later.
- **Cache hit rate is a monitored metric**: `cache_read_tokens / (input_tokens + cache_read_tokens)`
  per turn, on any model whose `CachingStyle` is not `NONE`. A sustained drop below the session's own
  baseline is reported by `kalash doctor` as a prefix-stability regression. The metric is what makes
  the snapshot tests meaningful rather than decorative.

Slot 6 sits **after** the last breakpoint for exactly this reason: the `<memory>` block changes every
turn ([`memory.md`](./memory.md) §9.1). Putting it at slot 4, where it reads more naturally, would
invalidate the `KALASH.md` hierarchy on every request.

---

## 2. The budget allocator

`core/budget.py` resolves every number below at the start of `ASSEMBLING`, against the **candidate
model's** `context_window` and `max_output_tokens` — not against a global default.

### 2.1 Reserves

Subtracted from the window before any slot gets anything.

| Reserve | Default | Floor | Ceiling | Why |
|---|---|---|---|---|
| `output` | 0.10 · W | 1,024 tok | 0.25 · W | If `max_output_tokens` does not fit, the model truncates mid-answer and `MAX_TOKENS` arrives with a half-written tool call ([`model-gateway.md`](./model-gateway.md) §3.1). Resolved as `clamp(configured_max_output, 1024, 0.25·W)` |
| `headroom` | 0.03 · W | 512 tok | 0.10 · W | Token-count estimation error. Adaptive: widened on a `CONTEXT_EXCEEDED` (§4.4) |
| `slack` | 0.02 · W | 256 tok | — | Provider-side framing we do not model: wire envelopes, system-prompt wrappers, tool-schema serialization overhead |
| `system_floor` | 0.05 · W | **1,500 tok, absolute** | — | Slot 1 is the operating contract. A model without it is not Kalash. Non-negotiable at any pressure level |
| `message_floor` | 0.02 · W | **2,048 tok, absolute** | — | The current user message. Dropping the request the user just typed is not a degradation, it is a wrong answer |

`system_floor` and `message_floor` are floors *inside* the arena, not additional subtractions.

### 2.2 Per-slot soft caps

Fractions of `W`. These are ceilings, not allocations — a slot that needs less takes less, and the
remainder is offered to slot 8 (recent turns), then slot 7 (compacted history).

| # | Slot | Cap | Class | Notes |
|---|---|---|---|---|
| 1 | Identity + contract | 0.05 | **guaranteed** | Floor 1,500 tok absolute |
| 2 | Tool schemas | 0.08 | guaranteed-if-required | Required whenever tool results are pending |
| 3 | Skills catalog | 0.03 | **compressible** | Full → name+description → names only → omitted |
| 4 | Environment | 0.02 | compressible | Full git status → branch + dirty flag |
| 5 | `KALASH.md` hierarchy | 0.06 | elastic | Nearest-first; ancestors trimmed before descendants |
| 6 | `<memory>` block | 0.08 | **sacrificial** | `memory.recall_budget`, default 0.08 ([`memory.md`](./memory.md) §9) |
| 7 | Compacted history | 0.12 | elastic | Re-summarized rather than truncated |
| 8 | Recent verbatim turns | 0.39 | elastic | Oldest-first eviction into slot 7 |
| 9 | Current user message | 0.02 | **guaranteed** | Floor 2,048 tok absolute; may borrow from 8 |
| | **Σ slot caps** | **0.85** | | |

### 2.3 The arithmetic

```
W                       = 1.00   context_window
  − output reserve      = 0.10
  − headroom            = 0.03
  − slack               = 0.02
  ─────────────────────────────
  = input arena A       = 0.85   ← Σ slot caps is exactly 0.85

Σ caps  = 0.05 + 0.08 + 0.03 + 0.02 + 0.06 + 0.08 + 0.12 + 0.39 + 0.02
        = 0.85                                                    ✓ = A
Σ caps + reserves = 0.85 + 0.10 + 0.03 + 0.02 = 1.00              ✓ ≤ W
```

Compaction triggers at **0.82 · W** of measured input (§5), which sits 0.03 · W below the arena
ceiling. That gap is deliberate: compaction must have room to run *before* assembly would otherwise
fail, and the summarizer's own request needs to fit somewhere.

Worked, `W = 200,000`, `max_output_tokens = 64,000`:

```
output reserve   min(64_000, 0.25·200_000 = 50_000) → 20_000   (0.10·W, under both bounds)
headroom          6_000
slack             4_000
arena           170_000

slot 1   10_000     slot 4    4_000     slot 7   24_000
slot 2   16_000     slot 5   12_000     slot 8   78_000
slot 3    6_000     slot 6   16_000     slot 9    4_000
                                        ───────────────
                                        Σ = 170_000        ✓
compaction trigger 0.82 · 200_000 = 164_000
```

Worked, `W = 32,768`, `max_output_tokens = 4,096` — a local model, where the allocator stops being
comfortable:

```
output reserve   clamp(4_096, 1_024, 8_192) = 4_096        (0.125·W — exceeds the 0.10 default)
headroom            983
slack               655
arena            27_034                                    (0.825·W, not 0.85 — reserves won)

slot caps are re-normalized against the residual arena: cap_i × (A / 0.85·W)
slot 1    1_590     slot 2    2_544     slot 6    2_544
```

Slot 2 at 2,544 tokens is the honest problem. The 14 built-in tool schemas serialize to ≈5,500
tokens; identity + contract is ≈2,000. That is ≈7,500 tokens of effectively **fixed** cost — 23% of a
32k window before a single line of the user's code is in context.

So the allocator states its own operating range:

| Window | Behaviour |
|---|---|
| `W < 16,384` | **Refuse.** `KALASH_CONTEXT_WINDOW_TOO_SMALL` at model resolution, naming the fixed cost and the window. Fixed cost would exceed 45% of the arena |
| `16,384 ≤ W < 49,152` | Warn once, and auto-select `tools.profile = "minimal"` — a 6-tool core (`read`, `write`, `edit`, `search`, `shell`, `todo`) at ≈2,400 tokens. Skills catalog compresses to names only |
| `W ≥ 49,152` | Full defaults |

Refusing is better than running. A 12k-window model with 7,500 tokens of fixed prefix produces a
loop that compacts on every turn, forgets constantly, and looks like a bug in Kalash rather than a
choice of model.

### 2.4 Allocation order and priority

Three passes:

1. **Guaranteed** — slot 1 at its floor, slot 9 at its floor, output reserve, headroom, slack. If
   these do not fit in `W`, the turn fails immediately with `KALASH_CONTEXT_UNSATISFIABLE`; no ladder
   step helps, because there is nothing left to sacrifice.
2. **Required** — slot 2 when tool results are pending; slot 9 beyond its floor. A 30k-token pasted
   stack trace is still the user's message: it borrows from slot 8, and if it cannot fit even then
   the turn fails loudly rather than silently answering half a question.
3. **Discretionary**, in descending priority: 5 → 7 → 8 → 4 → 3 → 6.

The ordering, justified:

| Rank | Slot | Why here |
|---|---|---|
| 1 | 1 identity + contract | The contract *is* the agent's safety behaviour. Trimming it trades correctness and I-033 framing for room |
| 2 | 9 current message | The turn is a wrong answer without it. Not a degradation — a defect |
| 3 | 2 tool schemas | A tool-capable model without schemas cannot act. `require_capabilities` refuses this at the gateway rather than degrading ([`model-gateway.md`](./model-gateway.md) §4.3) |
| 4 | 5 `KALASH.md` | Standing user constraints. Loss produces confidently wrong work, which is worse than slow work |
| 5 | 7 compacted history | Already the cheapest possible representation of the past. Dropping it discards work already paid for |
| 6 | 8 recent turns | Ground truth about the current task. Elastic only because eviction is *into* slot 7, not into nothing |
| 7 | 4 environment | Compressible with little loss; the agent can re-derive git state with a tool call |
| 8 | 3 skills catalog | Progressive disclosure already means it is an index. Names-only still lets the model ask |
| 9 | 6 `<memory>` block | **Yields first.** See §3.1 |

---

## 3. Pressure ladder

Trigger is `assembled_input / W`, evaluated during `ASSEMBLING`. Each step is attempted in order and
re-measured; the ladder stops as soon as the assembly fits with reserves intact.

| Step | Trigger | Action | Durable? |
|---|---|---|---|
| **L0** | < 0.60 | Nothing | — |
| **L1** trim memory | ≥ 0.60 | Slot 6 cap 0.08 → 0.04. Diversity pass preserved so kinds stay represented | render-only |
| **L2** drop memory | ≥ 0.70 | Slot 6 → 0. `<memory>` omitted entirely; the omission is recorded in turn metadata (I-037), never silent | render-only |
| **L3** shed bulk | ≥ 0.74 | `ARTIFACT` chunks dropped from history; tool-result bodies over 8 KiB replaced by digest + head + retrieval hint ([`tools.md`](./tools.md) §4 marker form) | render-only |
| **L4** compress history | ≥ 0.78 | Superseded file reads collapsed to the latest version only; old tool results rendered as one-line outcomes; slot 4 to branch + dirty flag; slot 3 to names only | render-only |
| **L5** compaction | ≥ 0.82 | `ASSEMBLING → COMPACTING` ([`state-machines.md`](./state-machines.md) §2). Turns older than the verbatim window are summarized into slot 7 | **durable rows** |
| **L6** hard compaction | ≥ 0.92 after L5, or on `KALASH_MODEL_CONTEXT_EXCEEDED` | Verbatim window reduced to the single most recent turn; everything else summarized, recursively if needed (§5.3); slot 5 to nearest-file only | **durable rows** |
| **L7** fail | Pass-1 set does not fit | `KALASH_CONTEXT_UNSATISFIABLE`, turn `FAILED`, session survives | — |

L1–L4 are **rendering decisions**: they change what this request contains and mutate nothing. L5 and
L6 write summary rows and therefore change every subsequent turn. The boundary matters for fallback
re-fit (§6) and for the user's mental model — a turn that got tight is different from a session that
lost detail.

### 3.1 Memory versus history under pressure — memory yields first

Two reasons, and the second is the real one.

**Regenerability.** A dropped `<memory>` block is reconstructible: the records still exist in KME and
in the external providers, and the next turn re-runs the same recall query. Nothing is lost but this
turn's recall. Dropped recent turns are not reconstructible *within the turn* — the transcript rows
survive (I-021), but the model's working state does not, and it will re-do or contradict work it
already did.

**Epistemic status.** Slot 8 is ground truth: what actually happened. Slot 6 is a *guess* about
relevance — a fused ranking across providers, filtered by salience, weighted by decay. Under pressure
you keep the record and drop the guess. A curated set of maybe-relevant claims plus amnesia about the
last five minutes is the worse of the two failures.

**One exception, and it is a correctness exception.** A `SEMANTIC` record with `source: user-stated`
and `trust: high` encoding a standing constraint — "never touch `vendor/`", "we use npm, not pnpm" —
is not dropped at L1/L2. It is **promoted into the compaction summary's constraints section** (§5.2)
and carried verbatim from then on. Constraint loss is the same correctness bug whether it arrives via
compaction or via budget pressure, so both paths get the same protection. Promotion preserves I-020
framing: the record moves into the summary's delimited constraints block with its provenance
attached, never into slot 1.

---

## 4. Token counting

### 4.1 Sources, in preference order

| Source | Used for | Marked as |
|---|---|---|
| Local exact tokenizer | OpenAI families (`tiktoken`), local models with a shipped HF tokenizer | `EXACT_LOCAL` |
| Provider count endpoint | Verification and calibration, **not** per-assembly — it is a network round trip on the critical path | `PROVIDER_COUNTED` |
| Character heuristic | Everything else, including most OpenAI-compatible endpoints | `HEURISTIC` |

Heuristic divisors, deliberately pessimistic: **3.2 chars/token for code and structured text**, 4.0
for prose, then a **× 1.05 safety multiplier**. Code tokenizes denser than prose — identifiers,
punctuation runs, and indentation all fragment — so using a prose divisor on a diff under-counts by
20–25%, which is exactly the direction that hurts.

Every count carries its source, and any assembly containing a `HEURISTIC` count is reported as an
estimate in the TUI (`~118k / 200k`) and in `--output-format json`. An estimate presented as exact is
a number users will make decisions against.

### 4.2 Why over-estimating is the safe direction

The costs are asymmetric, so the bias is not a matter of taste.

| Direction | Consequence |
|---|---|
| **Under-estimate** | The request exceeds `context_window`. The provider rejects it, the gateway raises `CONTEXT_EXCEEDED`, the allocator escalates a ladder step, re-assembles, and re-requests. The user pays two round trips of latency, the input tokens of the rejected attempt are billed by some providers, and the failure lands mid-turn where it is most disruptive |
| **Over-estimate** | A few hundred tokens of window go unused. One or two fewer memory records are injected |

Wasting 0.3% of a window is cheap. A hard API failure mid-turn is not.

### 4.3 Caching counts

Key: `(sha256(content), tokenizer_id)`. In-memory LRU (default 8,192 entries) backed by a
`token_counts` table so the cache survives restarts. `tokenizer_id` is in the key because identical
text tokenizes differently under different tokenizers, and a cross-tokenizer cache hit is a silent
under-count. Slots 1–5 are static per session and hit the cache every turn after the first, so
assembly cost is dominated by slots 6–9 — which is the point of the ordering.

### 4.4 Adaptive headroom

When a request the allocator believed fit is rejected with `CONTEXT_EXCEEDED`, the estimate was wrong
for this model and tokenizer. The allocator records `estimated` versus `provider_reported` on the turn
(I-037) and widens `headroom` for the `(model, tokenizer)` pair for the remainder of the session:
`H := min(H × 1.5, 0.10 · W)`. One mis-estimate teaches the session instead of repeating every turn.

---

## 5. Compaction

### 5.1 Trigger and mechanics

Threshold `context.compaction_threshold`, default **0.82** of the window, configurable. Fires on the
`ASSEMBLING → COMPACTING` transition and unconditionally on `KALASH_MODEL_CONTEXT_EXCEEDED` — the
gateway does not truncate, so the allocator must ([`model-gateway.md`](./model-gateway.md) §4.3).

`PreCompact` hooks run before, `PostCompact` after. `PreCompact` may narrow the range or add
must-preserve items; it cannot suppress compaction, because suppressing it means the turn cannot be
assembled at all.

**Compaction is recorded as new rows, never an `UPDATE` (I-021).** The summary is a new message with
its own `seq`, marked as superseding a `seq` range; the verbatim turns it summarizes stay in the
database. That is what makes `kalash rewind` to a pre-compaction point possible, and why "the context
was compacted here" is a fact a session can explain rather than an absence.

### 5.2 What the summarizer must preserve

Extracted as a **structured** record via the gateway's structured-output path
([`model-gateway.md`](./model-gateway.md) §10), validated against a schema, not parsed out of prose.

| Must preserve | Why |
|---|---|
| Files touched, with paths | The rewind manifest and the agent's own sense of what it has changed both depend on it |
| Decisions made, with rationale | Without the rationale the agent re-litigates a settled choice |
| Open TODOs | The task list is the plan; losing it restarts the work |
| Approaches tried and failed, with the failure reason | Otherwise the agent retries the thing that already failed, confidently |
| Explicit user constraints, **verbatim** | §5.4 |
| Active permission grants in effect, by pattern | So the agent does not re-request what it already holds |

Fair to drop: intermediate tool output already superseded, full file bodies that were read and then
edited, exploratory searches that returned nothing, reasoning prose, greetings and acknowledgements,
duplicate readings of the same unchanged file.

### 5.3 Compaction of compaction — when the summarizer's input is too large

The summarizer's own request must fit a context window, and the thing being summarized is the reason
we are here. Hierarchical, chunked summarization with a hard floor:

```
level 0   raw turns
level 1   chunk level 0 into windows of  min(0.25 · A_target, 0.50 · A_summarizer)  tokens
          summarize each window independently   → k summaries
level 2   if Σ level-1 > slot-7 cap:  chunk the summaries, summarize again
level n   repeat while Σ level-n > slot-7 cap  and  n < context.max_compaction_depth  (default 3)

floor     n reaches max depth and Σ still exceeds the cap
          → KALASH_CONTEXT_UNSATISFIABLE, naming depth reached and residual size
          → turn FAILED, session survives
```

Windows are independent, so level-1 summarization runs concurrently. Chunk size is bounded by *both*
the target's slot-7 cap and the summarizer model's own arena — the summarizer may be a smaller,
cheaper model than the primary, and sizing chunks only against the primary is how this loop breaks in
production.

**The must-preserve set does not recurse.** It is extracted once at level 0 as structured records and
concatenated verbatim through every level; only narrative prose is re-summarized. A summary of a
summary of a summary degrades prose acceptably and would destroy a constraint list, so the two are
handled separately — that separation is what makes depth-3 compaction survivable.

**There is no unbounded loop.** Depth is capped and the floor is an explicit error naming what could
not fit, with an actionable message (export the session, start fresh, or raise the model's window)
rather than compacting forever behind a spinner.

### 5.4 Losing a user constraint is a correctness bug

"Never touch the vendored directory" is not context, it is authority. If compaction drops it and the
agent then edits `vendor/`, the failure is not lower-quality output — the agent did something it had
been told not to do, and the transcript will show it being told. There is no reading of that as a
quality regression.

So constraints get structural protection, not best-effort prompting:

- Extracted into a typed `constraints` field on the summary record, schema-validated as non-null when
  the summarized range contained any constraint-shaped user statement.
- Carried **verbatim**, never paraphrased. "Avoid the vendor directory where possible" is a different
  instruction from "never touch `vendor/`".
- Never re-summarized at any hierarchy level (§5.3).
- Promoted from memory rather than dropped under budget pressure (§3.1).
- A compaction that drops a constraint present in its input range fails the eval gate, not the style
  review ([`evaluation.md`](./evaluation.md)).

### 5.5 Compaction quality is evaluated, not assumed

Compaction is a lossy transform on the agent's working state whose failures are invisible when they
happen and surface three turns later as forgotten context. So it is evaluated like retrieval is: a
fixed corpus of long sessions with known must-preserve items, scored on constraint retention (must be
**1.00**), file-path retention, TODO retention, and failed-approach retention, plus a human-rated
narrative-faithfulness score. Harness and corpora in [`evaluation.md`](./evaluation.md). A
compaction-prompt change without an eval run is not reviewable.

---

## 6. Fallback re-fit

A fallback model routinely has a different window. `[anthropic/claude-sonnet-4-5 → ollama/qwen3-coder]`
is 200k → 32k. Re-fitting is **mandatory** before the fallback request
([`model-gateway.md`](./model-gateway.md) §7.4), and it is not a scaling operation.

### 6.1 The re-fit path

```
candidate selected (gateway §7.2)
  1  re-resolve reserves against candidate.context_window / max_output_tokens
  2  re-count every slot with the candidate's tokenizer          ← not the primary's counts
  3  drop opaque payloads if provider differs (gateway §2.2) and re-count the affected blocks
  4  run the pressure ladder against the candidate's arena
  5  if the fit needs a step beyond fallback.max_degradation  →  skip candidate
  6  if the fit needs L5+ and not fallback.allow_compaction   →  skip candidate
  7  issue the request as a candidate-local rendering
```

Step 2 is the one that gets skipped by mistake. Identical text tokenizes differently across
tokenizers — a Llama tokenizer and `cl100k_base` disagree by 10–20% on code — so reusing the primary's
counts against the candidate's window is an under-count in exactly the direction §4.2 warns about.

### 6.2 A candidate is skipped, not squeezed

| Condition | Behaviour |
|---|---|
| `candidate.context_window < min_viable_window` (16,384) | Skip. Recorded in the fallback chain |
| Pass-1 guaranteed set does not fit the candidate | Skip immediately — arithmetically impossible, no ladder helps |
| Fit requires a ladder step beyond `fallback.max_degradation` (default **L5**) | Skip. A fallback that answers from one turn of context is not the same agent |
| Fit requires dropping slot 2 while tool results are pending | Skip. Consistent with `require_capabilities` |
| Fit requires L5+ and `fallback.allow_compaction` is false | Skip. Compaction is durable and not undoable |
| All candidates skipped | `AllModelsFailed`, with the per-candidate skip reason in the attempt chain |

Skipping is right because the alternative is a fallback that technically succeeds while having
discarded most of the task. A visible "no usable fallback" beats an invisible "answered from 4k of
context."

### 6.3 Re-fit is candidate-local, and L5+ is not

L1–L4 produce a **rendering** for one request and mutate nothing. If the primary recovers next turn,
the session is unchanged — the correct outcome, because a transient 429 must not permanently degrade
a session.

L5+ writes summary rows (I-021) and *does* persist. Compaction driven by a fallback's smaller window
is therefore gated on `fallback.allow_compaction` (default `true`), notified alongside the
model-switch notice (gateway §7.4), and recorded on the turn as fallback-induced rather than
pressure-induced (I-037). A user asking later why detail vanished deserves to find "fell back to a
32k model at 14:07 UTC" rather than a mystery.

---

## 7. Tool results and large files

### 7.1 Single tool result

Caps and truncation strategy are owned by [`tools.md`](./tools.md) §4 and not restated here: 256 KiB
per call, 2,000 lines, 4,000 chars per line, head-and-tail retention with an elision marker carrying
the retrieval call, binary output written to a blob and referenced by digest.

The allocator's addition is what happens **after** the tool's own cap, because 256 KiB is still ≈70k
tokens and would eat a third of a 200k window:

| Condition | Behaviour |
|---|---|
| Result ≤ `context.tool_result_inline_max` (8 KiB) | Inlined verbatim |
| 8 KiB < result ≤ tool cap | Inlined, but marked as a **shed candidate**: L3 replaces it with digest + head + retrieval hint |
| Any single result > 0.15 · A | Reduced to head + tail + marker at assembly time regardless of ladder level. One tool call may not consume 15% of the arena |
| Result already shed in a prior turn | Never re-inlined. Re-expansion requires an explicit `read` by the agent |

### 7.2 File chunking

`read` returns at most 250 KiB or 2,000 lines ([`tools.md`](./tools.md) §4). Beyond that the agent
pages explicitly with `offset`/`limit`, guided by the elision marker.

Chunking rules for the retrieval path (`ARTIFACT` indexing, [`memory.md`](./memory.md) §15) and for
paged reads:

- Chunk on **syntactic boundaries** where a parser is available — function, class, top-level
  statement — never on a fixed byte offset. A chunk that starts mid-function is nearly useless to
  both retrieval and the model.
- Default target 512 tokens, hard max 2,048, with 64 tokens of overlap so a definition split across a
  boundary appears whole in one of the two chunks.
- Every chunk carries `path`, `start_line`, `end_line`, and the file digest at read time — the same
  digest I-012 checks on edit, so a chunk cited from a stale index is detectable rather than silently
  wrong.
- Minified or single-line files (no usable boundary within 4,096 tokens) fall back to byte chunking
  and are flagged `boundary: none`, so retrieval can down-weight them.

---

## 8. Budget ceilings and spend behaviour

Distinct from everything above. §§1–7 allocate a window; this section bounds money and time (I-026,
asset A9). The two are independent: a turn can fit perfectly and still be the turn that crosses a
cost ceiling.

### 8.1 Dimensions

| Ceiling | Type | Interactive default | Unattended default | Notes |
|---|---|---|---|---|
| `max_tokens` | int | 500,000 / run | 200,000 | Input + output + cache writes |
| `max_cost` | **`Decimal`** | `2.00` USD | `0.50` | Never `float` ([`model-gateway.md`](./model-gateway.md) §8.2). `cost: null` from a model without pricing counts as **unbounded** and is refused for unattended runs |
| `max_wallclock` | seconds, monotonic | 3,600 | 900 | Monotonic clock (I-038) |
| `max_turns` | int | 100 | 25 | |
| `max_tool_calls` | int | 500 | 100 | Catches tight tool loops that burn few tokens |
| `max_spawn_depth` | int | 3 | 2 | I-027 |
| `max_concurrent_agents` | int | 8 | 2 | Global, not per-parent |

Resolved at run creation and stamped on the `agent_runs` row, so a config change mid-run cannot
retroactively widen a ceiling.

### 8.2 Inheritance is by reservation

I-026 requires inherit-and-decrement. The subtlety is concurrency: if six children each read the
parent's *remaining* budget, six children each believe they may spend all of it and the ceiling is
exceeded 6×. That is the exponential-cost bug the invariant exists to prevent.

```
spawn:      child_ceiling = min(child_requested, parent_remaining − Σ outstanding_reservations)
            reserve child_ceiling against the parent
            child_ceiling ≤ 0  →  refuse the spawn, as a tool error the parent can adapt to
complete:   release the reservation, debit the parent by the child's ACTUAL spend
depth:      max_spawn_depth decremented by 1 at every level
```

A parent never grants more than it holds, and concurrent siblings cannot double-spend one remainder.

### 8.3 The graduated response

Thresholds are fractions of each ceiling, evaluated **at state transitions**, never by a poller
([`state-machines.md`](./state-machines.md) §3).

| Stage | Trigger | What the user sees | Agent continues? | Recorded |
|---|---|---|---|---|
| **Warning** | 0.75 of any ceiling | TUI status bar turns amber with `cost $1.51/$2.00 · turns 76/100`; `budget.warning` on the event bus; `budget_warning` event in `stream-json`; one stderr line in `-p` text mode | **Yes**, no prompt, no interruption | Turn metadata |
| **Soft limit** | 0.90 | Run pauses at the next transition. Prompt: dimension, spend so far, ceiling, what is still undone, and options `continue` / `continue with a raised ceiling` / `stop now` | **Only on an explicit answer.** No timeout approves continuation | `agent_runs` + audit |
| **Hard limit** | 1.00 | `BUDGET_EXCEEDED`. Which ceiling, the exact figure, and a plain statement of what remains undone | **No.** In-flight work cancelled through the task group (I-029, I-008) | `agent_runs` terminal state, exit code 4 |

At the hard limit the partial result is returned, not discarded: work already paid for is handed back
with an explicit "incomplete because X" (AGENT.md §Termination). Silent truncation is forbidden.

**The warning is not injected into the model's context.** A model told it is running out of budget
rushes, cuts corners, and produces work that has to be redone — which costs more. And the decision to
stop or continue belongs to the user, so putting it in front of the model invites the model to make
it. Budget state belongs in the UI and the event stream, not the prompt.

### 8.4 Can a hard limit trigger a fallback to a cheaper model?

**No.** Recommended and normative.

- Silently degrading model quality to stay under budget produces confusing results. The user
  attributes the drop to the task, the codebase, or Kalash — not to a rule that fired at 14:07 UTC.
- It contradicts the gateway's rule that model switches are visible (§7.4 there); a budget-driven swap
  mid-run is the least visible kind there is.
- It converts a bounded, explicit stop into an unbounded, degraded continuation. A ceiling exists to
  **stop**, not to shop for a cheaper way to keep going.
- It frequently costs *more*. A weaker model often needs more turns and more correction to reach the
  same place, and the aggregate can exceed what the stronger model would have spent.

Offered instead: at the **soft** limit the user may choose to continue on a cheaper model — an
explicit, recorded decision (I-037) made by the person paying, with the tradeoff stated. Same
mechanism, opposite direction of authority.

### 8.5 Can a ceiling be raised mid-run?

**Yes, interactively. Never for unattended runs.**

| Context | Behaviour |
|---|---|
| Interactive TUI, soft limit reached | Raise offered in the prompt. New ceiling recorded on the `agent_runs` row with a timestamp and the prior value; audit row written. Children created *after* the raise inherit the new value; already-running children keep theirs — no retroactive grants, consistent with §8.2 |
| Interactive, before the soft limit | `/budget` shows current spend and allows a raise at any time |
| `kalash -p`, schedule, subagent of an unattended run | **Refused.** `KALASH_BUDGET_IMMUTABLE`. There is nobody to ask, and I-032 forbids inferring consent |

**Unattended runs must have finite ceilings resolved before they start.** A schedule or `-p`
invocation with no explicit ceiling takes the unattended defaults from §8.1; if a dimension resolves
to unbounded — no default configured, or a model with `pricing: null` under a cost ceiling — the run
**refuses to start** with `KALASH_BUDGET_UNBOUNDED`, naming the dimension.

"Unbounded and unattended" is the precise shape of the five-figure overnight bill (A9, T-J). It is
cheap to refuse at start and impossible to undo at 3am.

---

## 9. Configuration

```toml
[context]
compaction_threshold   = 0.82     # L5 trigger, fraction of window
max_compaction_depth   = 3        # §5.3 floor
tool_result_inline_max = "8KiB"
min_viable_window      = 16384
verbatim_turns         = 6        # slot 8 target before eviction into slot 7
reserves = { output = 0.10, headroom = 0.03, slack = 0.02 }

# soft caps; must sum ≤ 1 − Σ reserves. `memory` tracks memory.recall_budget —
# changing one without the other is a load-time error.
slots = { identity = 0.05, tools = 0.08, skills = 0.03, env = 0.02, project = 0.06,
          memory = 0.08, history = 0.12, recent = 0.39, message = 0.02 }

[budget]
max_tokens     = 500000
max_cost       = "2.00"           # string → Decimal. A float literal is a load-time error
max_wallclock  = 3600
max_turns      = 100
max_tool_calls = 500
warn_at        = 0.75
soft_at        = 0.90
unattended = { max_tokens = 200000, max_cost = "0.50", max_wallclock = 900, max_turns = 25 }

[model.fallback_policy]
max_degradation  = "L5"
allow_compaction = true
```

Config load fails if `Σ slots > 1 − Σ reserves`, naming the overage. A misconfiguration that makes
every turn unassemblable should surface at startup, not on the first long turn.

---

## 10. Testing

| Concern | Approach |
|---|---|
| Assembly order | `syrupy` snapshots of rendered slots 1–5 and of breakpoint positions. A prefix diff is a review event |
| Cache stability | Assemble the same session twice with only slot 9 changed; assert slots 1–5 are byte-identical. Assert no timestamp, counter, PID, or unordered-`dict` serialization appears in slots 1–5 |
| Budget arithmetic | `hypothesis` properties over random `(W, max_output, slot demands)`: Σ allocations ≤ W always; reserves always intact; `system_floor` and `message_floor` never violated; no allocation negative; ladder is monotonic (each step never increases assembled size) |
| Priority ordering | Property test: for every pressure level, the set of surviving slots is a superset of the set surviving at the next level up |
| Small windows | Fixture models at 8k / 16k / 32k / 49k; assert refuse, warn+minimal-profile, warn+minimal-profile, full defaults |
| Ladder | Synthetic sessions engineered to land at each trigger; assert the exact step reached and that L1–L4 mutate no rows |
| Memory-first yield | Assert slot 6 empties before slot 8 loses a turn; assert a `user-stated`/`trust: high` constraint record is promoted, not dropped |
| Compaction fidelity | Corpus with known must-preserve items; assert constraint retention **1.00**, path/TODO/failed-approach retention above threshold ([`evaluation.md`](./evaluation.md)) |
| Compaction of compaction | Session whose level-1 summaries alone exceed the slot-7 cap; assert hierarchical recursion, constraints carried verbatim through every level, depth cap honoured, and `KALASH_CONTEXT_UNSATISFIABLE` at the floor rather than a loop |
| Compaction durability | Assert summaries are `INSERT`s; assert no `UPDATE`/`DELETE` on `messages` (I-021); assert rewind to a pre-compaction `seq` succeeds |
| Fallback re-fit | Fake candidates at 200k / 32k / 8k; assert re-count with the candidate tokenizer, correct skip reasons, no mutation of history for L1–L4 fits, and `allow_compaction: false` skipping rather than compacting |
| Context exceeded | Fake provider raising `CONTEXT_EXCEEDED` on a request the allocator believed fit; assert ladder escalation, adaptive headroom widening, at most 2 retries, no model fallback (gateway §5.1) |
| Token counting | Golden corpus per tokenizer; assert heuristic counts are never below exact counts; assert cache keys include `tokenizer_id`; assert `HEURISTIC` provenance propagates into UI and JSON output |
| Ceiling enforcement | Assert ceilings are checked at every `agent_runs` transition and that **no background poller exists** (I-026). Kill a run mid-tool-call at 0.99 of a ceiling; assert `BUDGET_EXCEEDED`, not overshoot |
| Ceiling inheritance | N concurrent children against one parent remainder; assert Σ child ceilings ≤ parent remaining via reservations, and that a 7th spawn is refused as a tool error rather than a crash |
| Unattended bounds | `-p` and schedule with an unbounded dimension; assert refusal with `KALASH_BUDGET_UNBOUNDED`; assert `KALASH_BUDGET_IMMUTABLE` on a mid-run raise attempt |
| Cost exactness | `Decimal` throughout; assert no `float` in the cost path; assert `pricing: null` propagates as unbounded, never as zero |

---

*See also: [`model-gateway.md`](./model-gateway.md) for capabilities, `CachingStyle`, usage
normalization, and the fallback algorithm this allocator re-fits for; [`memory.md`](./memory.md) §9
for the recall pipeline that fills slot 6; [`tools.md`](./tools.md) §4 for output caps;
[`state-machines.md`](./state-machines.md) §2–§3 for the `COMPACTING` and `BUDGET_EXCEEDED`
transitions; [`interfaces.md`](./interfaces.md) for how budget state is surfaced in the TUI and the
JSON event stream; [`evaluation.md`](./evaluation.md) for compaction-quality harnesses;
[`invariants.md`](./invariants.md) I-020, I-021, I-026, I-029, I-033, I-037.*
