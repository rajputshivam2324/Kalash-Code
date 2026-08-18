# State Machines

> **Status:** normative.
>
> Prose is ambiguous for lifecycle logic. These are the formal machines for every long-lived entity in
> Kalash: session, turn, agent run, tool call, permission request, memory write, memory record,
> scheduled run, MCP connection, circuit breaker, trust grant — plus crash recovery (§12) and
> multi-process coordination (§13).
>
> The tool-call machine (§4) is the I-004 ordering made explicit; the permission-request machine (§5) is
> the [`permissions.md`](./permissions.md) §5.3 no-response matrix made executable. Inherited
> constraints: I-008, I-012, I-018, I-021, I-023, I-024, I-025, I-026, I-029, I-031, I-032, I-037.

---

## 0. Conventions

| Notation | Meaning |
|---|---|
| `▣` | Terminal state. No outgoing transitions; a request to leave it is an error, not a no-op |
| `⇄` | Bidirectional, repeatable |
| **Persisted?** | `✓` written to SQLite before the transition is observable · `mem` in-process only · `✓ tx` inside the same transaction as its side effects |
| Illegal transition | Raises `KALASH_STATE_ILLEGAL_TRANSITION` with both states. Never silently ignored — a silent no-op turns a logic bug into a data bug discovered much later |

**A state that lives only in memory cannot be recovered on restart, only re-derived.** Which states
those are is the entire content of §12, so each machine names its persistence point explicitly.

---

## 1. Session

Persisted: `sessions.state`, `sessions.lease_owner`, `sessions.lease_expires_at`, `sessions.closed_at`.
Terminal: `CLOSED`.

```
CREATED ──first turn──▶ ACTIVE ⇄ IDLE ──close──▶ CLOSED ▣
                          │ ▲                      ▲
            lease expiry  │ │ resume               │ rm / gc
                          ▼ │                      │
                       ORPHANED ──▶ RESUMED ───────┘
```

| From | Event | To | Side effects | Persisted? |
|---|---|---|---|---|
| — | `kalash` / `kalash -p` | CREATED | Row inserted, workspace + git state resolved, sandbox admitted (I-011) | ✓ |
| CREATED | First user message | ACTIVE | Lease acquired with heartbeat; `SessionStart` hooks | ✓ |
| ACTIVE | No input for `session.idle_timeout` (30m) | IDLE | Model clients released, MCP connections may degrade (§9). Lease heartbeat continues | ✓ |
| IDLE | User input | ACTIVE | Workspace and git state re-resolved — the tree may have moved while idle | ✓ |
| ACTIVE / IDLE | `/exit`, EOF, `SIGTERM` | CLOSED ▣ | `SessionEnd` hooks; process groups killed (I-013); ledger flushed; lease released | ✓ |
| ACTIVE / IDLE | Lease expired, no heartbeat | ORPHANED | Recorded by **another** process or the next start — never by the dead one | ✓ |
| ORPHANED | `kalash resume <id>` | RESUMED | Transcript replayed, blobs verified (I-025), dangling turn/tool rows resolved (§12) | ✓ |
| RESUMED | Replay complete | ACTIVE | Fresh lease | ✓ |
| ORPHANED | `kalash session rm`, GC | CLOSED ▣ | Blobs dereferenced | ✓ |
| CLOSED ▣ | Any | — | `KALASH_SESSION_CLOSED` | — |

`RESUMED` is a distinct state rather than a direct hop to `ACTIVE` so the audit log records that history
was replayed rather than continued live, which keeps I-037 replay honest.

**On process death:** in `CREATED` the row may not exist and nothing needs recovery. In `ACTIVE`/`IDLE`
the lease stops heartbeating and the session becomes `ORPHANED` at expiry (90s, three missed beats);
recovery is `kalash resume`, and `kalash session ls` shows it as orphaned. A crashed session is never
*locked* — that is the whole reason for a lease rather than a boolean flag.

---

## 2. Turn

Persisted: `turns.state`; content in `messages` / `content_blocks`, append-only (I-021).
Terminal: `COMPLETED`, `INTERRUPTED`, `FAILED`.

```
PENDING ─▶ ASSEMBLING ⇄ COMPACTING        RETRYING ◀── retryable error
              │  ▲                            │
              │  └────────────────────────────┘
              ▼
       MODEL_STREAMING ─▶ TOOL_DISPATCH ─▶ TOOL_EXECUTING
              │                                  │
              └───────▶ ACCUMULATING ◀───────────┘
                             │
          COMPLETED ▣    INTERRUPTED ▣    FAILED ▣
```

| From | Event | To | Side effects | Persisted? |
|---|---|---|---|---|
| — | User message | PENDING | Turn row, monotonic `seq` | ✓ |
| PENDING | Scheduled | ASSEMBLING | Recall (I-005), context assembly, budget check (I-026) | ✓ |
| ASSEMBLING | Context over threshold (0.82) | COMPACTING | `PreCompact` hooks; summary written as new rows, never an `UPDATE` | ✓ |
| COMPACTING | Summary complete | ASSEMBLING | `PostCompact` hooks | ✓ |
| ASSEMBLING | `KALASH_MODEL_CONTEXT_EXCEEDED` | COMPACTING | Gateway does not truncate; the allocator does | ✓ |
| ASSEMBLING | Request dispatched | MODEL_STREAMING | Usage accrues; deltas rendered incrementally | ✓ |
| MODEL_STREAMING | `MessageStop`, `stop_reason=TOOL_USE` | TOOL_DISPATCH | Calls materialized as `tool_calls` rows | ✓ |
| MODEL_STREAMING | `MessageStop`, `stop_reason=END_TURN` | ACCUMULATING | — | ✓ |
| MODEL_STREAMING | Retryable error class | RETRYING | Partial **discarded** by digest, never continued ([`model-gateway.md`](./model-gateway.md) §5.3) | ✓ |
| RETRYING | Backoff elapsed, budget remains | ASSEMBLING | Fallback may change provider; opaque payloads stripped | ✓ |
| RETRYING | Retry budget exhausted | FAILED ▣ | Error surfaced with the attempt chain | ✓ |
| TOOL_DISPATCH | All calls admitted or refused | TOOL_EXECUTING | Scheduling per side effect ([`tools.md`](./tools.md) §3) | ✓ |
| TOOL_EXECUTING | All envelopes returned | ACCUMULATING | Results appended as new rows | ✓ tx |
| ACCUMULATING | Tool results present | ASSEMBLING | Loop continues | ✓ |
| ACCUMULATING | No tool calls, or `Stop` hook blocks | COMPLETED ▣ | Usage rolled up; memory write pipeline enqueued (non-blocking) | ✓ tx |
| Any non-terminal | User interrupt | INTERRUPTED ▣ | Cancellation propagates through the task group (I-029); no partial writes (I-008) | ✓ |
| Any non-terminal | Non-retryable error | FAILED ▣ | Error recorded; session survives | ✓ |

**Tool calls execute only after `MessageStop`**, which is why a discarded partial stream leaves no side
effects and why retry is safe: `TOOL_DISPATCH` is unreachable from an incomplete stream. The one exception
is the speculative path for `side_effect` of `none`/`read`, whose results are discarded on stream failure;
`write` and `exec` never run before `MessageStop`.

**On process death:** `PENDING`/`ASSEMBLING`/`MODEL_STREAMING` lose nothing but tokens already billed, and
the next start marks the turn `INTERRUPTED`. `TOOL_EXECUTING` is the hard case, resolved by idempotency
classification (§12.3). `ACCUMULATING` is durable because results committed in the same transaction as the
state change.

---

## 3. Agent run

Persisted: `agent_runs.state`, `agent_runs.parent_run_id`, budget columns. Terminal: `COMPLETED`,
`FAILED`, `CANCELLED`, `BUDGET_EXCEEDED`, `DEPTH_REFUSED`. Covers interactive and subagent runs
identically — one machine, so a subagent cannot have a lifecycle the parent's tooling does not understand.

```
CREATED ─▶ RUNNING ─▶ COMPLETED ▣
   │        │ ⇄ WAITING_TOOL
   │        │ ⇄ WAITING_APPROVAL
   │        ├─▶ INTERRUPTED ▣  ·  CANCELLED ▣  ·  FAILED ▣
   │        └─▶ BUDGET_EXCEEDED ▣
   └──────────▶ DEPTH_REFUSED ▣
```

| From | Event | To | Side effects | Persisted? |
|---|---|---|---|---|
| — | Spawn request | CREATED | Capabilities intersected with parent's; budget inherited-and-decremented (I-026) | ✓ |
| CREATED | Ancestor chain contains this agent, or depth cap hit | DEPTH_REFUSED ▣ | Returned to the parent as a tool error it can adapt to, not a crash (I-027) | ✓ |
| CREATED | Admitted | RUNNING | Isolated context; parent transcript not shared (I-028) | ✓ |
| RUNNING | Turn enters `TOOL_EXECUTING` | WAITING_TOOL | — | ✓ |
| WAITING_TOOL | Envelopes returned | RUNNING | — | ✓ |
| RUNNING | Permission request `ASKED` | WAITING_APPROVAL | No model stream held, no tokens accruing | ✓ |
| WAITING_APPROVAL | Decided | RUNNING | — | ✓ |
| WAITING_APPROVAL | Unattended run reached confirmation class | FAILED ▣ | `KALASH_APPROVAL_REQUIRED`; stops and reports (I-032) | ✓ |
| RUNNING / WAITING_* | Any budget ceiling crossed **at a transition** | BUDGET_EXCEEDED ▣ | In-flight work cancelled; partial result returned with the reason | ✓ |
| RUNNING | Result envelope produced | COMPLETED ▣ | Usage rolled up to the parent, separable for reporting | ✓ tx |
| RUNNING / WAITING_* | User interrupt | INTERRUPTED ▣ | Cancellation cascades to descendants (I-029) | ✓ |
| RUNNING / WAITING_* | Parent terminated | CANCELLED ▣ | Distinct from `INTERRUPTED`: nobody asked, the reason disappeared | ✓ |

**Budget checks are transitions, not background polling.** Every arrow above re-evaluates tokens, turns,
wallclock, cost, and depth. Polling would let a single long tool call or model request blow through a
ceiling and notice afterwards, which is exactly the runaway-cost case I-026 exists to prevent.

**On process death:** every state is durable and none is resumable — in-flight reasoning is not
checkpointed. Recovery marks the run `INTERRUPTED` and, for a subagent, records that the parent never
received a result. Orphaned children of a dead parent are marked `CANCELLED` by the parent's absence rather
than left `RUNNING` forever.

---

## 4. Tool call

Persisted: `tool_calls.state` plus per-stage timing. Terminal: `RETURNED`, `DENIED`, `TIMED_OUT`,
`CANCELLED`.

**This machine *is* I-004.** The stage order in [`tools.md`](./tools.md) §2 is not a convention the
dispatcher happens to follow — it is the only path through this graph: `EXECUTING` is unreachable except
from `SCHEDULED`, unreachable except from `HOOKED`, and so on back to `RECEIVED`. Stating the ordering as a
machine makes "no tool executes before permission evaluation" checkable rather than asserted.

```
RECEIVED ─▶ VALIDATED ─▶ NORMALIZED ─▶ AUTHORIZED ─▶ ADMITTED ─▶ HOOKED
                                                                    │
   RETURNED ▣ ◀── RECORDED ◀── CAPPED ◀── EXECUTING ◀── SCHEDULED ◀──┘
                                             │
   any stage ──▶ DENIED ▣                    └──▶ TIMED_OUT ▣ · CANCELLED ▣
```

| From | Event | To | Side effects | Persisted? |
|---|---|---|---|---|
| — | `MessageStop` with a tool call | RECEIVED | Row with `call_id` + `provider_call_id` | ✓ |
| RECEIVED | Tool resolves | VALIDATED | — | ✓ |
| RECEIVED | Unknown name | DENIED ▣ | `KALASH_TOOL_UNKNOWN` + nearest names | ✓ |
| VALIDATED | Schema passes | NORMALIZED | — | ✓ |
| VALIDATED | Schema fails / bad JSON | DENIED ▣ | `KALASH_TOOL_INVALID_ARGS` / `KALASH_TOOL_MALFORMED_ARGS`; never partially applied | ✓ |
| NORMALIZED | Canonicalization complete | AUTHORIZED | Paths resolved, hosts resolved, globs expanded — **before** policy | ✓ |
| NORMALIZED | Path escapes roots | DENIED ▣ | `KALASH_SANDBOX_PATH_DENIED` naming the **resolved** path | ✓ |
| AUTHORIZED | Permission evaluation allows (§5) | ADMITTED | Capability source recorded for audit | ✓ |
| AUTHORIZED | Denied, or unattended + confirmation class | DENIED ▣ | `KALASH_PERMISSION_DENIED` / `KALASH_APPROVAL_REQUIRED` | ✓ |
| ADMITTED | Sandbox admits | HOOKED | Protected paths, writable roots, network re-checked at the OS layer | ✓ |
| ADMITTED | Protected path | DENIED ▣ | `KALASH_SANDBOX_PROTECTED_PATH` (I-010) | ✓ |
| HOOKED | `PreToolUse` exit 0 | SCHEDULED | stdout fed back as context | ✓ |
| HOOKED | `PreToolUse` exit 2 or `deny` | DENIED ▣ | `KALASH_HOOK_DENIED`, stderr verbatim; final at any depth (I-034) | ✓ |
| SCHEDULED | Concurrency slot acquired | EXECUTING | Serialization per `side_effect` | ✓ |
| EXECUTING | Returns | CAPPED | Output truncated head-and-tail with a marker | mem |
| EXECUTING | Timeout | TIMED_OUT ▣ | `KALASH_TOOL_TIMEOUT`; process group killed (I-013); partial output kept where meaningful | ✓ |
| EXECUTING | Cancellation | CANCELLED ▣ | `KALASH_CANCELLED`; no partial state (I-008, I-029) | ✓ |
| CAPPED | — | RECORDED | Audit row, `side_effects`, usage; `PostToolUse` hooks annotate but cannot un-execute | ✓ tx |
| RECORDED | — | RETURNED ▣ | Envelope handed to the loop | ✓ |

`CAPPED` is the one non-durable state, deliberately: it exists for a few milliseconds between a tool
returning and its results committing, and a crash there is indistinguishable from a crash inside
`EXECUTING`, so §12 treats them as one case.

**On process death:** `RECEIVED` through `SCHEDULED` are pure decisions with no external effect — recovery
marks them `CANCELLED` and loses nothing. `EXECUTING` and `CAPPED` are the indeterminate window resolved
by §12.3.

---

## 5. Permission request

Persisted: `permission_requests.state` plus the rendered prompt text, options offered, and answer time —
the row a grant's `request_id` points at ([`permissions.md`](./permissions.md) §4.1). Terminal:
`APPROVED`, `DENIED`, `MODIFIED`, `EXPIRED`, `ABANDONED`, `AUTO_DENIED`, `AUTO_ALLOWED`.

```
UNDECIDED ─grant matched─▶ AUTO_ALLOWED ▣        (ASKED never entered)
    ├─unattended────────▶ AUTO_DENIED ▣          I-032
    └─prompt shown──────▶ ASKED ─┬─▶ APPROVED ▣ · DENIED ▣ · MODIFIED ▣
                                 ├─▶ EXPIRED ▣    timeout → denies
                                 └─▶ ABANDONED ▣  stdin/tty lost, SIGINT → denies
```

| From | Event | To | Side effects | Persisted? |
|---|---|---|---|---|
| — | Tool call needs a capability not held | UNDECIDED | Request row with the resolved target | ✓ |
| UNDECIDED | Grant matches (§4.3 of permissions.md) | AUTO_ALLOWED ▣ | `once` consumed; audit `capabilities_source` set. **`ASKED` is skipped entirely — no prompt is rendered** | ✓ |
| UNDECIDED | Non-interactive principal | AUTO_DENIED ▣ | `KALASH_APPROVAL_REQUIRED`; run stops and reports (I-032) | ✓ |
| UNDECIDED | Interactive | ASKED | Prompt rendered and persisted verbatim; run enters `WAITING_APPROVAL` | ✓ |
| ASKED | `a` / `s` / `A` | APPROVED ▣ | Grant created for `session`/`always`; none for `once` | ✓ tx |
| ASKED | `d` / `D` | DENIED ▣ | `KALASH_PERMISSION_DENIED`; reason returned as correctable feedback | ✓ |
| ASKED | `m` | MODIFIED ▣ | A **new** tool call re-enters §4 at `VALIDATED`; this request is closed, not mutated | ✓ |
| ASKED | `approval.timeout` elapsed | EXPIRED ▣ | Denies. `KALASH_APPROVAL_EXPIRED` | ✓ |
| ASKED | stdin closed, TTY lost, SSH dropped | ABANDONED ▣ | Denies. `KALASH_APPROVAL_ABANDONED` | ✓ |
| ASKED | `SIGINT` | ABANDONED ▣ | Cancels the tool call, not the session; `KALASH_CANCELLED` | ✓ |
| ASKED | No answer, no timeout configured | ASKED | Waits indefinitely. Idle: no stream held, no tokens accruing | ✓ |

Every terminal state except `APPROVED` and `AUTO_ALLOWED` denies. **There is no transition from any
non-answer to an allow**, the property §14 asserts exhaustively.

`AUTO_ALLOWED` bypassing `ASKED` is the difference between a grant and a pre-filled prompt: a matched grant
renders no prompt at all, not even a dismissable notice, so the audit chain back to the *original* prompt
([`permissions.md`](./permissions.md) §4.1) is the only record of consent and must be intact.

**On process death:** `ASKED` is durable, which is what makes recovery possible. The next start finds an
`ASKED` row with an expired session lease, transitions it to `ABANDONED`, and reports what was being asked.
It never resumes the prompt — the user who would have answered is gone and the motivating tool call is
already indeterminate.

---

## 6. Memory write

Persisted: `memory_ledger.state` plus per-provider receipts. Terminal: `RECONCILED`, `TOMBSTONED`,
`ABANDONED`.

```
INTENT ─▶ QUEUED ─▶ REDACTED ─▶ DISPATCHED ─▶ ACKED ─▶ RECONCILED ▣
                                    │ ▲
                                    ▼ │ backoff
                                 FAILED ─▶ RETRYING
                                    └─▶ ABANDONED ▣   (retry cap)

any state ──forget──▶ TOMBSTONED ▣
```

| From | Event | To | Side effects | Persisted? |
|---|---|---|---|---|
| — | Extraction + salience keep a candidate | INTENT | **Durable before any dispatch** | ✓ |
| INTENT | Background drain picks it up | QUEUED | — | ✓ |
| QUEUED | Redaction profile applied | REDACTED | Secrets and PII removed at the boundary (I-006) | ✓ |
| REDACTED | Sent via the egress gateway | DISPATCHED | Audit row written **before** the wire (I-003, I-007) | ✓ |
| DISPATCHED | Receipt from every target provider | ACKED | Provider record IDs stored | ✓ |
| DISPATCHED | Timeout, error, breaker open (§10) | FAILED | Degradation recorded; the user's turn is unaffected (I-005) | ✓ |
| FAILED | Backoff elapsed | RETRYING | — | ✓ |
| RETRYING | — | DISPATCHED | — | ✓ |
| FAILED | Retry cap reached | ABANDONED ▣ | **User-visible** record — a silently dropped write is a memory that does not exist but appears to | ✓ |
| ACKED | Divergence check passes | RECONCILED ▣ | Mirror consistency confirmed | ✓ |
| Any | `forget` targets this record | TOMBSTONED ▣ | Tombstone written **first**, suppressing recall immediately (I-018) | ✓ tx |

**`INTENT` is durable before dispatch, and that one property buys three features**: offline writes (the
intent survives with no network), crash replay (§12), and provider migration (the ledger replays into a new
backend). Dispatch-then-record loses all three and makes divergence undetectable.

**On process death:** everything from `INTENT` onward is durable. `DISPATCHED` is the ambiguous state — the
provider may or may not have applied the write — and recovery re-dispatches, relying on idempotency keyed
by content hash. A provider without idempotent writes gets a duplicate, which the dedupe pass collapses.
Duplicate-and-collapse beats lost-write: a duplicated memory is a quality problem, a lost one is a
correctness problem.

---

## 7. Memory record

Persisted: `memory_records.state` (local provider) and the ledger's tombstone set. Terminal:
`TOMBSTONED`.

```
PENDING ─▶ ACTIVE ─▶ SUPERSEDED
   │          │           │
   └──────────┴───────────┴──forget──▶ TOMBSTONED ▣
```

| From | Event | To | Side effects | Persisted? |
|---|---|---|---|---|
| — | Ledger write `ACKED` | PENDING | Not yet recallable | ✓ |
| PENDING | Provenance verified against the ledger digest | ACTIVE | Recallable. A record whose provenance does not reconcile is downgraded to `trust: low` and flagged, never accepted at face value (T-E) | ✓ |
| ACTIVE | A newer record contradicts it | SUPERSEDED | Retained for history; excluded from recall by default | ✓ |
| ACTIVE / SUPERSEDED / PENDING | `forget` | TOMBSTONED ▣ | Suppressed at the router immediately; provider deletions dispatched after | ✓ tx |

`TOMBSTONED` is **permanent and unreachable** — by reconciliation, provider sync, `memory import`, or
migration (I-018). There is no transition out of it, not even an administrative one. When a user says
forget, the record stops influencing behaviour at that instant; a resurrection path would make "forget"
mean "forget for now."

**On process death:** all states durable; unverified `PENDING` records are re-verified on next start.
Tombstones cannot be lost, because they are written before the deletions they authorize.

---

## 8. Scheduled run

Persisted: `schedule_runs.state`; `schedules.enabled`, `schedules.next_run_at`, lease columns. Terminal:
`SUCCEEDED`, `FAILED`, `SKIPPED`, `CANCELLED`, `STOPPED_NEEDS_APPROVAL`.

```
SCHEDULED ─▶ DUE ─▶ QUEUED ─▶ RUNNING ─▶ SUCCEEDED ▣
              │                  ├─▶ FAILED ⇄ RETRYING ─▶ QUEUED
              │                  │      └─▶ FAILED ▣   (retry cap → auto-disable)
              └─▶ SKIPPED ▣      └─▶ STOPPED_NEEDS_APPROVAL ▣

DUE / QUEUED / RUNNING ──disable | rm──▶ CANCELLED ▣
```

| From | Event | To | Side effects | Persisted? |
|---|---|---|---|---|
| — | `kalash cron add` | SCHEDULED | `next_run_at` computed in the schedule's zone, stored UTC (I-038) | ✓ |
| SCHEDULED | `next_run_at` reached | DUE | — | ✓ |
| DUE | Overlap policy `skip` and a run is active | SKIPPED ▣ | Reason recorded | ✓ |
| DUE | Catch-up policy `skip_missed` and the fire time has passed (machine asleep) | SKIPPED ▣ | Reason recorded | ✓ |
| DUE | Runner acquires the lease | QUEUED | Advisory lock row + lease + heartbeat: exactly one runner fires it (§13) | ✓ tx |
| QUEUED | Concurrency slot free | RUNNING | Headless session created; capabilities capped by the schedule's declared set | ✓ |
| RUNNING | Run completes | SUCCEEDED ▣ | `Notification` hook; `next_run_at` advanced | ✓ tx |
| RUNNING | Confirmation-class action reached | STOPPED_NEEDS_APPROVAL ▣ | **Stops and reports.** `KALASH_APPROVAL_REQUIRED`; never auto-approves (I-032) | ✓ |
| RUNNING | Error | FAILED | Recorded with the error class | ✓ |
| FAILED | Backoff elapsed, under the retry cap | RETRYING → QUEUED | — | ✓ |
| FAILED | Consecutive failures ≥ `max_consecutive_failures` | FAILED ▣ | `schedules.enabled = false` + notification. Auto-disable, because a schedule failing every hour for a week is noise that hides real signal | ✓ tx |
| DUE / QUEUED / RUNNING | `kalash cron disable` / `rm` | CANCELLED ▣ | In-flight run cancelled (I-029) | ✓ |

`SKIPPED` carries two distinct causes — overlap and catch-up — and the row records which, because
collapsing them loses the difference between "too slow for its interval" and "my laptop was closed."

**On process death:** the daemon's lease stops heartbeating, and a `RUNNING` row with an expired lease is
resolved on the next tick by any runner — marked `FAILED (runner_died)`, then retried per policy. Because
the lease expires rather than being held, a crashed daemon's schedules recover instead of stalling forever.

---

## 9. MCP server connection

Persisted: `mcp_servers.state` is the **last known** state for `kalash mcp ls`; the live state is
in-process. Terminal: `FAILED`, `REFUSED`.

```
CONFIGURED ─first use─▶ CONNECTING ─▶ READY ⇄ DEGRADED
   ▲                        │                     │
   │ next process start     ├─▶ AUTH_REQUIRED ─auth─▶ CONNECTING
   │                        ├─▶ REFUSED ▣   (capability mismatch)
   │                        └─▶ FAILED ▣          ▼
   └───────────────── RECONNECTING ◀─backoff─ DISCONNECTED
```

| From | Event | To | Side effects | Persisted? |
|---|---|---|---|---|
| — | Config loaded | CONFIGURED | **Resting state.** Lazy connect: no startup latency for servers this session may never touch | ✓ |
| CONFIGURED | First tool reference, or `eager` warm | CONNECTING | Transport opened; stdio servers spawned under sandbox policy | mem |
| CONNECTING | Discovery succeeds | READY | Schemas validated and digested (I-037); tools namespaced `mcp__<server>__<tool>` | ✓ |
| CONNECTING | Server's declared capabilities exceed the session's | REFUSED ▣ | Startup warning naming the mismatch — a visible refusal, not mysterious tool failures | ✓ |
| CONNECTING | OAuth needed | AUTH_REQUIRED | `kalash mcp auth <server>`; tokens to the keyring, never the DB | ✓ |
| AUTH_REQUIRED | Auth completes | CONNECTING | — | ✓ |
| CONNECTING | Unreachable, bad handshake, hostile schema | FAILED ▣ | Tools absent from the model's schema set for this session | ✓ |
| READY | Some tools erroring, transport alive | DEGRADED | Failing tools reported as unavailable; the rest keep working | mem |
| DEGRADED | Recovery | READY | — | mem |
| READY / DEGRADED | Transport closed, process exited | DISCONNECTED | In-flight calls fail with `KALASH_TOOL_UNKNOWN` on the next reference | mem |
| DISCONNECTED | Backoff elapsed | RECONNECTING → CONNECTING | Schema digest re-compared; a changed schema is surfaced, not silently adopted | ✓ |

**On process death:** stdio servers are children and die with the process group (I-013); remote sessions are
abandoned server-side. Nothing needs recovery — the next start returns every server to `CONFIGURED`, the
correct resting state. This is the one machine where recovery is trivial, precisely because none of its
state is authoritative.

---

## 10. Provider circuit breaker

Persisted: `provider_state.breaker_state`, `failure_count`, `opened_at`. Used by memory providers and the
model gateway. No terminal states — a breaker is a permanent fixture of a provider's record.

```
        N consecutive failures          probe fails
CLOSED ───────────────────────▶ OPEN ◀──────────────┐
   ▲                             │ cooldown         │
   │      probe succeeds         ▼                  │
   └───────────────────── HALF_OPEN ────────────────┘
```

| From | Event | To | Side effects | Persisted? |
|---|---|---|---|---|
| CLOSED | Request succeeds | CLOSED | `failure_count = 0` | ✓ |
| CLOSED | `failure_count` reaches threshold (default 5) | OPEN | `opened_at` set; requests fail fast without a network attempt | ✓ |
| OPEN | Request arrives before cooldown | OPEN | Immediate failure, no attempt. Recall degrades to `[]` (I-005); writes go to `INTENT` (§6) | — |
| OPEN | Cooldown elapsed (exponential, capped) | HALF_OPEN | One probe permitted | ✓ |
| HALF_OPEN | Probe succeeds | CLOSED | Counter reset | ✓ |
| HALF_OPEN | Probe fails | OPEN | Cooldown lengthened | ✓ |
| Any | `kalash provider test` | HALF_OPEN | Manual probe — a user who has fixed the outage should not wait out a cooldown | ✓ |

The breaker stops a dead provider from costing latency on every turn: without it, a 1500 ms recall timeout
against an unreachable host is paid on every turn for the rest of the session. Persisting the state means
the next process does not re-pay it either.

**On process death:** durable. A breaker left `OPEN` is honoured by the next process, with `opened_at` still
governing the cooldown — the outage does not get a free retry just because Kalash restarted.

---

## 11. Trust grant

Persisted: `trust_grants.state`, `content_hash`, `granted_at`. Applies to hooks, skill scripts, and
plugins (I-031). No terminal states — trust is revisitable by design.

```
             ┌───────────────── re-prompt ─────────────────┐
             │                                             │
UNTRUSTED ─▶ PROMPTED ─▶ TRUSTED ──content hash changed──▶ INVALIDATED
    ▲            │
    └──declined──┘
```

| From | Event | To | Side effects | Persisted? |
|---|---|---|---|---|
| — | Executable extension discovered | UNTRUSTED | Not loaded, not executed | ✓ |
| UNTRUSTED | First load attempt | PROMPTED | Prompt shows the **file path and the code that will run**, not a yes/no | ✓ |
| PROMPTED | User approves | TRUSTED | `content_hash` recorded; extension loadable | ✓ tx |
| PROMPTED | User declines | UNTRUSTED | Extension unavailable for the session; not re-prompted this session | ✓ |
| TRUSTED | On-disk hash differs at load | INVALIDATED | Not executed. `KALASH_TRUST_INVALIDATED` | ✓ |
| INVALIDATED | Next load attempt | PROMPTED | Diff against the trusted version shown, so the user evaluates the change rather than the whole file | ✓ |
| Any | `kalash hooks trust --revoke` | UNTRUSTED | — | ✓ |

Hash-pinning is what keeps `git clone && kalash` from equalling arbitrary code execution (T-A). Showing a
diff on re-prompt rather than the whole file matters: a user re-approving a 300-line hook they already read
will skim, whereas a three-line diff is actually reviewable.

**On process death:** all states durable, nothing in flight. A crash during `PROMPTED` leaves `UNTRUSTED`,
the fail-closed direction.

---

## 12. Crash recovery

Anchored on I-024 (no partial state after interruption) and I-008 (atomic writes). The question this
section answers is not "does it recover" but "what exactly is durable, what is lost, and what does the
user get told."

### 12.1 The recovery machine

```
START ─▶ MIGRATE ─▶ LEASE_SCAN ─▶ LEDGER_REPLAY ─▶ RUN_RESOLVE ─▶ BLOB_VERIFY
                                                                       │
   READY ◀── SCHEDULE_RECONCILE ◀──────────────────────────────────────┘

any step fails ──▶ RECOVERY_FAILED ▣    refuses to start, reports, never "tries anyway"
```

Order is load-bearing:

1. **Migrations** — complete or roll back any interrupted migration (I-022) **before** anything reads a
   table whose shape may be mid-change.
2. **Lease scan** — expired session and schedule leases identified. Before ledger replay, so two
   concurrently starting processes do not both claim the same orphaned work.
3. **Ledger replay** — `INTENT`/`DISPATCHED` rows re-dispatched (§6).
4. **Run resolution** — incomplete turns, tool calls, and agent runs resolved per §12.2.
5. **Blob verification** — digests recomputed for blobs referenced by recovered checkpoints (I-025).
   Lazy for the rest; verifying every blob at every start would make startup scale with history size.
6. **Schedule reconciliation** — expired run leases released, `next_run_at` recomputed, catch-up policy
   applied.

`RECOVERY_FAILED` is a real terminal state. A start that cannot reach a consistent database refuses to
run and says why, because operating on a half-recovered database is how one crash becomes permanent
corruption.

### 12.2 `kill -9` matrix

| Killed during | Durable | Lost | Next start does | User sees |
|---|---|---|---|---|
| **Tool execution** (`EXECUTING`/`CAPPED`) | The call row through `HOOKED`; any completed atomic write | The envelope; output not yet capped | Idempotent → may re-run; non-idempotent → **indeterminate** (§12.3). Turn → `INTERRUPTED` | "This tool call may or may not have completed: `<tool>` `<args>`. Verify before continuing." |
| **Filesystem write** | Either the complete old content or the complete new content, never a mix (I-008) | The temp file, garbage-collected | Removes stale `.kalash-tmp-*` siblings in the target directory | Nothing unusual. This is the case that is genuinely solved |
| **SQLite transaction** | Everything committed before it | The open transaction | WAL rollback, automatic (I-023) | Nothing. The last turn may be absent |
| **Memory ledger write** | The `INTENT` row (durable before dispatch) | Nothing | Re-dispatches from `INTENT` or `DISPATCHED` | Nothing; memory arrives late |
| **Provider dispatch** | Intent + the pre-dispatch audit row (I-007) | The receipt | Re-dispatch; duplicates collapse by content hash | Nothing. Audit shows an egress with unknown outcome — the honest record |
| **Checkpoint creation** | Blobs already written and verified | The manifest, if incomplete | Discards partial manifests; a checkpoint without a complete manifest is not a checkpoint | The checkpoint is absent from `kalash rewind` — never listed-but-broken |
| **Scheduled run** | The `schedule_runs` row in `RUNNING` | The run's progress | Lease expires; any runner marks it `FAILED (runner_died)`; retry per policy | Failed run in `kalash cron logs` with the reason |
| **Plugin execution** | Whatever the plugin committed through Kalash APIs | In-process plugin state | Plugin re-initialized from scratch; no plugin state is resumed | Nothing, unless the plugin wrote partially through its own side channels — **not something Kalash can guarantee** |
| **Hook execution** | The `hook_runs` row up to dispatch | Exit code and output | Hook recorded as indeterminate. **Never assumed to have allowed** — an unknown gate outcome is a denial | The gated tool call reported as indeterminate |
| **Subagent run** | The `agent_runs` row and its usage | The result envelope | Child → `INTERRUPTED`; parent → `INTERRUPTED` with "child result never received" | Both runs visible in the trace tree with their states |
| **Database migration** | Steps committed in prior transactions | The open step | Completes or rolls back the step; the pre-migration backup is retained until one successful start (I-022) | Migration status; on failure, the backup path |
| **Blob write** | Nothing — blobs are written to a temp name and renamed only after the digest matches | The temp file | Orphan temp blobs swept; content-addressing means a re-written blob is byte-identical | Nothing |

### 12.3 Idempotency

`Tool.idempotent` is not documentation; it is the input to exactly one decision, made in `RUN_RESOLVE`:

| `idempotent` | Completion recorded | Recovery |
|---|---|---|
| `true` | no | **Safe to re-run.** Re-reading a file or re-running `git status` costs latency and nothing else |
| `false` | no | **Reported as indeterminate.** The turn is `INTERRUPTED` and the specific call is named in the user-visible report |
| either | yes | Nothing to do; the envelope is durable |

**Never assume "probably completed."** A `git push` that may or may not have landed, treated as completed,
produces an agent reasoning forward from a remote state that does not exist — a divergence that surfaces
much later and is hard to attribute. Treated as failed, it produces a duplicate attempt, which for a
non-idempotent operation may be worse. Reporting indeterminacy is the only honest option, and it costs the
user thirty seconds of verification.

---

## 13. Concurrent sessions

Several `kalash` processes plus the scheduler daemon, one repository, one `~/.kalash/kalash.db`. This is
the normal case, not the exotic one — a developer with two terminals and a cron daemon is already here.

### 13.1 Database

One serialized writer queue per database handle, WAL journaling, `busy_timeout = 5000`, `foreign_keys = ON`
(I-023). Reads go wide and uncontended. The queue serializes writes *within* a process; WAL serializes them
*between* processes.

The failure this avoids is not corruption — SQLite handles that — it is `SQLITE_BUSY` surfacing as a tool
error mid-turn. `busy_timeout` converts contention into latency, the right trade for a single-user tool where
write bursts are small and rare.

### 13.2 Session leases

`sessions.lease_owner` (pid + boot id) and `lease_expires_at`, heartbeated every 30 s with a 90 s expiry.
A crashed process's session is detectable as `ORPHANED` (§1) rather than locked forever.

Boot id is included because a pid alone is ambiguous after a reboot: pid 4711 exists again and belongs to
something else entirely, and a liveness check on pid alone would conclude a dead session is alive.

### 13.3 Two Kalash processes editing the same file

Resolved by the I-012 content-digest check, not by locking.

```
P1  read  src/auth.py       → digest sha256:9f2c…      (00:00:01)
P2  read  src/auth.py       → digest sha256:9f2c…      (00:00:02)

P1  edit  src/auth.py  digest=sha256:9f2c…
      on-disk digest == 9f2c…  → atomic write          (00:00:05)
      file now sha256:4d81…

P2  edit  src/auth.py  digest=sha256:9f2c…
      on-disk digest == 4d81…  ≠ 9f2c…
      → KALASH_TOOL_STALE_READ
        metadata:    {expected: 9f2c…, actual: 4d81…, modified_at: 00:00:05}
        remediation: "re-read the file; it changed since your read"

P2  read  src/auth.py       → digest sha256:4d81…      (00:00:06)
P2  edit  src/auth.py  digest=sha256:4d81…  → applies on top of P1's change
```

P2's agent sees a normal, recoverable tool error with an actionable remediation and re-reads. No lock was
taken, no process waited, and the second write cannot silently clobber the first.

### 13.4 The user editing in their IDE

The **same mechanism, and the common case.** A developer reading the agent's diff, fixing a typo by hand, and
saving is ordinary behaviour, and it is indistinguishable to Kalash from another agent writing. The digest
check catches it identically.

This is why I-012 is not defensive over-engineering: it costs one `stat` and a hash of a file already being
opened, while the alternative — silently overwriting a developer's save — is data loss they discover hours
later, cannot attribute, and do not forgive.

### 13.5 Advisory file locks: not used on the working tree

Deliberately rejected for coordinating writes to project files, for four reasons.

**A crash releases the lock mid-operation.** The OS drops advisory locks when the holder dies, so the lock
vanishes exactly when the tree might be inconsistent — it protected the interval it was least needed. The
digest check has no such hole: it compares content, and content survives a crash.

**The IDE will never participate.** The most frequent concurrent writer is the user's editor, which does not
know Kalash exists, so a protocol only the agents follow does not protect against the writer that matters.

**Semantics are inconsistent.** `flock` and `fcntl` interact differently, network filesystems implement them
unreliably, and Windows mandatory locking is a different model — correctness would be platform-dependent.

**Locks strand.** A stale lock file from a killed process blocks work until someone deletes it by hand: a
support burden with no upside here.

Locks *are* used for one thing (§13.7), and there it is a database row with a lease rather than an OS file
lock, because a lease expires and an OS lock carries no timeout.

### 13.6 Git worktrees as isolation

For genuinely parallel agent work, a worktree per agent is the strongest available isolation: separate
`HEAD`, index, and working tree, so two agents build and test simultaneously without fighting over
checked-out files. Sibling worktrees fall outside each session's writable roots
([`permissions.md`](./permissions.md) §6.3), which makes the isolation enforced rather than conventional.
The limits, stated plainly:

| Shared across worktrees | Consequence |
|---|---|
| Object store and most refs | Concurrent commits are fine (append-mostly); concurrent branch updates to the *same* ref race, and git's own ref locking fails one |
| `.git/config` | A config change from one affects all. Protected in every mode anyway (I-010) |
| `.git/hooks/` | One hook set for all worktrees. Protected (I-010) |
| `refs/stash` | `git stash` is global. Two agents stashing interleave confusingly |
| `git gc`, `git prune` | Repository-wide. Running one during another worktree's operation can be disruptive |
| The remote | Two worktrees pushing to the same branch collide at the remote, not locally |

So worktrees isolate the *working tree*, which is most of the problem, and do not isolate repository-level
state. They are a strong mitigation, not a partition.

### 13.7 Schedule execution

One advisory **lock row** per schedule, carrying a lease and a heartbeat. A runner claims a due schedule by
conditionally updating that row inside the transaction that moves the run to `QUEUED` (§8), so exactly one
runner fires it even with two daemons and an interactive `kalash serve` alive at once.

The lease is what makes recovery work: a dead runner's lease expires and the next tick from any runner
resolves the stranded run rather than waiting on a process that will never return. A boolean `is_running`
flag would strand the schedule permanently.

### 13.8 What is explicitly not coordinated

Honest boundaries. None of these are prevented, and pretending otherwise would be worse than saying so.

| Not coordinated | What actually happens |
|---|---|
| **Semantic conflicts** | Two agents fixing the same bug differently. The digest check catches the write collision, never the logical one. Both changes may apply and be individually valid and jointly wrong |
| **Unsaved editor buffers** | Kalash sees the disk, not the buffer. A write lands under an unsaved buffer and the editor's own conflict prompt is the only warning |
| **Subprocess resources** | Two sessions running `npm run dev` collide on a port. The OS reports it; Kalash neither prevents nor reserves |
| **The git index within one worktree** | Two concurrent `git add` in the same tree race. Git's `index.lock` fails one, and Kalash surfaces that failure rather than retrying — a retry would be guessing at intent |
| **Remote state** | Two sessions pushing the same branch. The second is rejected by the remote, as it should be |
| **MCP server instances** | One set of connections per process. Two processes spawn two stdio servers with no shared state |
| **Memory provider ordering** | Writes from concurrent sessions are eventually consistent. The ledger reconciles divergence; it does not serialize across processes |
| **Model provider rate limits** | Shared quota, unshared knowledge. Concurrent sessions can rate-limit each other, mitigated by jittered backoff, not by coordination |

---

## 14. Testing

| Concern | Approach |
|---|---|
| Transition coverage | Per machine, a test asserting **every** table row above fires with its stated side effects and persistence. A row without a test is a transition nobody has run |
| Illegal transitions | Exhaustively attempt every (state, event) pair absent from each table; assert `KALASH_STATE_ILLEGAL_TRANSITION` and no state change. A silent no-op fails the test |
| Reachability | Property test: from the initial state, every state is reachable by some event sequence. An unreachable state is dead code pretending to be a design |
| No dead ends | Property test: every non-terminal state has at least one outgoing transition available under some input. A non-terminal dead end is a hang |
| Terminal immutability | No transition leaves any `▣` state; specifically, `TOMBSTONED` resists reconciliation, provider sync, import, and migration (I-018) |
| Crash injection | `kill -9` at each state of each machine under a supervisor, then restart; assert §12.2 exactly — durable, lost, and user-visible message (I-024). Includes kill mid-write: complete-old or complete-new, never mixed, no temp siblings left (I-008) |
| Recovery ordering | `MIGRATE` precedes any table read and `LEASE_SCAN` precedes `LEDGER_REPLAY`; two simultaneous starts do not double-replay. Corrupt mid-migration → `RECOVERY_FAILED`, refusal to start, backup path reported (I-022) |
| Idempotency | Per tool, kill during `EXECUTING`; idempotent tools re-run, non-idempotent ones report indeterminate — never assumed completed (§12.3) |
| Approval no-answer | Every §5 non-answer path (`EXPIRED`, `ABANDONED`, `AUTO_DENIED`, killed while `ASKED`); assert **none reaches `APPROVED`** |
| Budget transitions | Ceilings evaluated at every §3 arrow; assert no background poller exists that could grant a grace window (I-026) |
| Concurrency | N processes interleaving writes with an integrity check after (I-023). Two processes editing one file: `KALASH_TOOL_STALE_READ` on the second, then a successful re-read (I-012) |
| Leases | Kill a lease holder; assert `ORPHANED` within expiry, resumable, never locked, and that boot-id reuse produces no false liveness result |
| Schedule exclusivity | Three concurrent runners, one due schedule → exactly one run. Kill the winner mid-run; another resolves it (§13.7) |
| Cancellation | Interrupt in each non-terminal state of §2/§3/§4; assert bounded acknowledgement, no orphan processes (I-013), no partial writes (I-029) |

---

*See also: [`invariants.md`](./invariants.md) I-008, I-012, I-018, I-021 through I-025, I-029, I-031,
I-032, I-037 for the properties these machines enforce,
[`permissions.md`](./permissions.md) §2 and §5 for the decision the §5 machine executes,
[`tools.md`](./tools.md) §2 for the lifecycle stages the §4 machine formalizes,
[`model-gateway.md`](./model-gateway.md) §5.3 for partial-stream handling and §6 for cancellation,
[`capabilities.md`](./capabilities.md) §3 for the attenuation the §3 machine applies.*
