# Invariants

> **Status:** normative. Every statement here is a hard requirement.
>
> These are the properties that must hold at all times, in every code path, on every platform. They
> are numbered so that tests, code comments, and PR descriptions can reference them by ID. A test
> that proves an invariant should name it (`test_i004_no_tool_before_permission_eval`). A PR that
> touches an invariant's enforcement path must say which IDs it affects.
>
> **If you cannot state which invariants your change preserves, you do not understand your change
> yet.**

An invariant differs from a design decision: a design decision can be revisited with a good argument,
an invariant can only be removed by deliberately deciding Kalash is a different product. Violating
one is a release-blocking bug, not a backlog item.

---

## How to read this document

| Field | Meaning |
|---|---|
| **ID** | Stable. Never reused, never renumbered. Retired invariants are marked `RETIRED` and kept. |
| **Class** | `SAFETY` (prevents harm), `PRIVACY` (prevents disclosure), `INTEGRITY` (prevents corruption), `AVAILABILITY` (prevents breakage), `CORRECTNESS` (prevents wrong output) |
| **Enforced by** | The module that is architecturally responsible. Not "everywhere" — one owner. |
| **Proven by** | The test that fails if the invariant breaks. If this is empty, the invariant is aspirational, not real. |

**Enforcement preference order.** When there is a choice, enforce an invariant in this order:

1. **Architecturally** — make the violation impossible to express (private constructors, single
   chokepoint, type system)
2. **Statically** — make the violation fail CI (import-linter contracts, `mypy`, grep-based bans)
3. **At runtime** — assert and fail closed
4. **By convention** — documented and code-reviewed

Convention is the weakest and should never be the only enforcement for a `SAFETY` or `PRIVACY`
invariant. Roughly: if a new contributor could plausibly violate it by writing reasonable-looking
code, the enforcement is too weak.

---

## Data boundary

### I-001 — Session transcripts never leave the device via the memory layer
**Class:** PRIVACY · **Enforced by:** `memory/router.py` · **Proven by:** `test_i001_session_kind_rejected_by_router`

`MemoryKind.SESSION` and `MemoryKind.WORKING` records are rejected by `MemoryRouter.write()` for any
provider whose transport is not local. The router raises rather than silently dropping, because a
silent drop hides a caller bug.

Verbatim conversation history lives in local SQLite. External memory providers receive *derived*
records (semantic, episodic, procedural, entity) and never the raw transcript. A user's conversation
with their coding agent is among the most sensitive data on their machine.

> Not covered by this invariant: the model provider necessarily receives conversation content — that
> is what inference is. This invariant is about the *memory* layer specifically. See I-020.

### I-002 — Source code egress requires explicit opt-in
**Class:** PRIVACY · **Enforced by:** `core/egress.py` · **Proven by:** `test_i002_artifact_egress_default_denied`

`MemoryKind.ARTIFACT` records — file contents, code chunks, diffs — are not transmitted to any remote
memory provider unless `memory.egress.artifacts` is `true`. Default is `false`. The default
configuration must never ship a user's proprietary source code to a third party.

### I-003 — Every byte leaving the machine passes the egress gateway
**Class:** PRIVACY · **Enforced by:** `core/egress.py` · **Proven by:** `test_i003_no_direct_http_client_outside_gateway`, `lint: import-linter contract "egress-chokepoint"`

No module outside `core/egress.py` may construct an `httpx.Client`/`AsyncClient`, open a raw socket,
or invoke a subprocess that performs network I/O without declaring `network.connect`. All outbound
traffic — model calls, memory providers, MCP remote servers, web fetches, telemetry, notifications,
plugin update checks — is routed through the gateway, which applies redaction (I-006), network policy
(I-014), and audit (I-007).

Enforced **statically**, not by convention: an import-linter contract plus a CI grep. Bypassing it
must fail the build, because "remember to call redact()" is not a security control.

### I-004 — No tool executes before permission evaluation completes
**Class:** SAFETY · **Enforced by:** `runtime/loop.py` · **Proven by:** `test_i004_no_tool_before_permission_eval`

The order is fixed and total: schema validation → argument normalization → capability resolution →
permission evaluation → sandbox admission → `PreToolUse` hooks → execute. There is no path from a
model-emitted tool call to execution that skips a stage. Tool implementations are unreachable except
through the dispatcher.

Argument normalization happens *before* permission evaluation for a specific reason: policies match
on canonicalized paths. Evaluating a policy against `../../etc/passwd` and then executing the
resolved path is how sandbox escapes happen.

### I-005 — A memory provider failure never fails a user turn
**Class:** AVAILABILITY · **Enforced by:** `memory/router.py` · **Proven by:** `test_i005_all_providers_down_turn_completes`

Recall degrades to `[]`. Writes degrade to a durable local ledger intent. Timeouts, connection
errors, auth failures, malformed responses, and rate limits are all absorbed and recorded. No memory
provider condition propagates an exception into `runtime/loop.py`.

Memory is an enhancement to a working agent. An agent that stops working because a memory API had an
outage is a worse product than one with no memory at all.

### I-006 — Every outbound record is redacted before transmission
**Class:** PRIVACY · **Enforced by:** `core/egress.py` · **Proven by:** `test_i006_secret_corpus_never_transmitted`

Redaction is applied inside the egress gateway, on the payload, immediately before the write to the
wire. Not at the call site. Not in the provider adapter. A provider adapter receives already-redacted
data and has no ability to opt out.

Proven against a corpus of real-shaped secrets (AWS keys, GitHub PATs, JWTs, private key PEMs, DB
connection strings, bearer tokens). The test asserts that no corpus entry appears in captured
outbound traffic under any configuration.

### I-007 — Every egress event is audited
**Class:** PRIVACY · **Enforced by:** `core/egress.py` · **Proven by:** `test_i007_egress_produces_audit_row`

One `audit_log` row per outbound request: destination host, purpose, capability that authorized it,
payload digest, byte count, record IDs where applicable, redactions applied. The audit row is written
in the same transaction boundary as the dispatch decision, so an egress cannot occur without a trace.

Audit rows store **digests, not payloads**. The audit log must not become a second copy of the
sensitive data it is auditing.

---

## Filesystem and execution

### I-008 — Every filesystem write is atomic
**Class:** INTEGRITY · **Enforced by:** `tools/fs.py`, `storage/blobs.py` · **Proven by:** `test_i008_interrupted_write_leaves_original`

Writes go to a temporary file in the *same directory* as the target (same filesystem, so `rename` is
atomic), are `fsync`ed, then `os.replace`d. A crash, kill, or cancellation at any point leaves either
the complete old content or the complete new content. Never a truncated file.

Same directory matters: a temp file in `/tmp` crossing a filesystem boundary makes `os.replace` a
copy, which is not atomic.

### I-009 — No write outside the resolved writable roots
**Class:** SAFETY · **Enforced by:** `sandbox/policy.py` · **Proven by:** `test_i009_escape_corpus_denied`

Every write path is fully canonicalized — symlinks resolved, `..` collapsed, Unicode normalized — and
must fall inside a writable root. Checked after resolution, never before.

Proven by an adversarial corpus: symlink to `/etc`, symlink chains, `..` traversal, absolute paths,
`/proc/self/cwd` tricks, `/dev` targets, TOCTOU races where the path becomes a symlink between check
and open (defeated by `O_NOFOLLOW` on the final component), Windows junctions and 8.3 short names,
case-insensitive-filesystem collisions, and NFC/NFD Unicode variants of protected filenames.

### I-010 — Protected paths are never written, under any sandbox mode
**Class:** SAFETY · **Enforced by:** `sandbox/policy.py` · **Proven by:** `test_i010_protected_paths_denied_all_modes`

`.git/config`, `.git/hooks/`, `.env*`, `~/.ssh/`, `~/.aws/`, `~/.gnupg/`, `~/.kalash/credentials*`,
OS keyring stores. Denied in `workspace-write`, denied in `danger-full-access`, denied for subagents,
denied for scheduled runs.

Overridable only by an explicit config list with a startup warning — and the override is per-path, not
a blanket disable. `.git/hooks/` is on this list because writing there converts a file edit into
arbitrary code execution on the user's next commit.

### I-011 — A sandbox that cannot enforce must not silently permit
**Class:** SAFETY · **Enforced by:** `sandbox/*.py` · **Proven by:** `test_i011_unavailable_backend_fails_closed`

If the platform backend cannot be established — no Landlock, no Seatbelt, kernel too old, missing
capability, container restriction — the sandbox fails **closed**. The requested mode is refused, the
reason is surfaced, and the user must explicitly downgrade to `danger-full-access` to proceed.

A sandbox that degrades to no sandbox while still reporting "workspace-write" is worse than having no
sandbox, because the user makes decisions based on a protection that does not exist. `kalash doctor`
reports the active backend and its enforcement level.

### I-012 — Edits require a fresh read
**Class:** CORRECTNESS · **Enforced by:** `tools/fs.py` · **Proven by:** `test_i012_stale_read_rejected`

`edit`, `multi_edit`, and overwriting `write` carry the content digest observed at read time. If the
on-disk digest differs, the call fails with `KALASH_TOOL_STALE_READ` and the agent must re-read.

This is optimistic concurrency, and it exists because the user is editing the same files in their IDE
while the agent works. Silently overwriting a developer's unsaved-then-saved change is data loss they
will not forgive, and it is invisible until much later.

### I-013 — Process trees terminate completely
**Class:** INTEGRITY · **Enforced by:** `tools/shell.py` · **Proven by:** `test_i013_no_orphans_after_timeout`

Shell commands run in a new process group (POSIX) or Job object (Windows). Timeout and cancellation
signal the entire group: `SIGTERM`, grace period, then `SIGKILL`. No orphaned children, no zombies, no
processes surviving the session that spawned them.

A `npm test` that spawns a dev server which survives session exit will hold a port and confuse the
next run.

---

## Network

### I-014 — No network access without `network.connect`
**Class:** SAFETY · **Enforced by:** `core/egress.py`, `sandbox/policy.py` · **Proven by:** `test_i014_sandboxed_process_cannot_reach_network`

Enforced at two independent layers, because they cover different attack surfaces:

- **In-process** — the egress gateway checks the capability before dispatch
- **In-sandbox** — the OS-level policy denies sockets to subprocesses

The second layer is what stops a `shell` command from `curl`ing out when the first layer has no
visibility into what the subprocess does. Defence in depth here is not redundancy.

### I-015 — Private address space is denied by default
**Class:** SAFETY · **Enforced by:** `core/egress.py` · **Proven by:** `test_i015_ssrf_corpus_denied`

Resolved destination addresses in RFC1918 space, loopback, link-local (including `169.254.169.254`
cloud metadata), CGNAT, IPv6 ULA/link-local, and `.local` mDNS are denied unless explicitly
allowlisted.

Enforcement is on the **resolved IP at connect time**, not the hostname, and the connection is pinned
to the validated address. Hostname-only validation is defeated by DNS rebinding: resolve to a public
IP for the check, then to `127.0.0.1` for the connection. Redirects are re-validated at every hop.

This is the invariant that stops a compromised MCP server or a fetched web page from using Kalash as
a proxy into the user's internal network or their cloud instance metadata endpoint.

---

## Memory

### I-016 — Every memory record carries provenance
**Class:** CORRECTNESS · **Enforced by:** `memory/protocol.py` · **Proven by:** `test_i016_provenance_required_on_write`

`source` (user-stated, agent-inferred, tool-derived, web-derived, imported), `trust` level,
`created_at`, `origin_provider`, and `session_id` are non-nullable on every record. Enforced by the
type — a `MemoryWrite` cannot be constructed without them.

Provenance is what makes memory poisoning detectable and what lets the agent weigh a user's explicit
statement above its own inference. Memory without provenance is rumour.

### I-017 — Project-scoped memory never surfaces in another project
**Class:** PRIVACY · **Enforced by:** `memory/router.py` + provider adapters · **Proven by:** `test_i017_scope_isolation` (part of the provider conformance suite)

Scope filters are applied provider-side where the provider supports it, and re-verified client-side on
every returned hit regardless. A provider bug, a misconfigured namespace, or a tenancy mistake in a
third-party service must not leak Project A's architecture into Project B's session.

Client-side re-verification is deliberate defence against provider bugs we cannot fix. Every provider
adapter must pass this test, including third-party ones.

### I-018 — Deletion is durable and verified
**Class:** PRIVACY · **Enforced by:** `memory/ledger.py` · **Proven by:** `test_i018_forget_survives_provider_outage`

`forget` writes a local tombstone **first**, then dispatches provider deletions. The tombstone
suppresses the record from recall immediately, even if every provider deletion is still pending or
failing. Pending deletions retry with backoff until acknowledged or explicitly abandoned with a
user-visible record.

A tombstoned record can never be resurrected by reconciliation, import, or provider sync. When a user
says forget, the record stops influencing behaviour at that instant — not eventually.

### I-019 — Vectors are only compared within an embedding version
**Class:** CORRECTNESS · **Enforced by:** `memory/providers/local/vectors.py` · **Proven by:** `test_i019_cross_embedding_comparison_impossible`

Every vector row stores `embedding_model_id` and `dim`. Similarity queries filter on both. Changing
the embedding model creates a new version namespace and schedules re-embedding; it never silently
compares vectors from different models.

Cosine similarity between embeddings from different models is a meaningless number that looks like a
valid score. This failure is silent, produces plausible-looking bad retrieval, and is nearly
impossible to diagnose from symptoms.

### I-020 — Memory content is data, never instruction
**Class:** SAFETY · **Enforced by:** `memory/pipeline/inject.py` · **Proven by:** `test_i020_poisoned_memory_not_obeyed`

Recalled records are injected inside a delimited block, labelled with trust level and provenance, and
framed as claims about the past rather than directives. Records with `trust: low` (web-derived,
tool-derived) are additionally marked untrusted.

Sensitive-class memories — anything that looks like a standing permission grant, a credential, an
instruction to transmit data, or a claim about what the user always approves — are never injected
automatically. They require explicit user confirmation to become active.

Proven against a poisoning corpus: records that assert blanket approval, records that instruct
exfiltration, records that impersonate system instructions.

---

## Storage and recovery

### I-021 — The message log is append-only
**Class:** INTEGRITY · **Enforced by:** `storage/repositories/messages.py` · **Proven by:** `test_i021_no_update_on_messages`

No `UPDATE` or `DELETE` on `messages` or `content_blocks` during normal operation. Corrections are new
rows superseding old ones by `seq`. Only explicit user-initiated deletion and GC may remove rows, and
both operate at whole-session granularity.

This is what makes replay correct and rewind possible. A mutable history cannot be replayed.

### I-022 — Every migration is forward-only and tested from every prior version
**Class:** INTEGRITY · **Enforced by:** `storage/migrations/` · **Proven by:** `test_i022_migration_matrix`

Numbered, forward-only SQL. No down-migrations — a bad migration is fixed by shipping another
migration. Every migration has a test that runs it against a populated fixture DB from each prior
schema version.

A backup is taken before any migration and retained until the new version has started successfully
once. The database holds the user's entire history with the tool; corrupting it is unrecoverable data
loss.

### I-023 — Exactly one writer
**Class:** INTEGRITY · **Enforced by:** `storage/engine.py` · **Proven by:** `test_i023_concurrent_writers_no_corruption`

All writes serialize through one queue per database handle. Multiple `kalash` processes plus the
scheduler daemon coordinate through WAL plus `busy_timeout`. Proven by N concurrent processes issuing
interleaved writes with integrity verification after.

### I-024 — Interrupted operations leave no partial state
**Class:** INTEGRITY · **Enforced by:** `storage/engine.py`, `runtime/checkpoint.py` · **Proven by:** `test_i024_crash_recovery_matrix`

Every multi-step operation is either transactional or resumable from a durable intent record. Kill
`-9` at any point in a tool execution, a memory write, a checkpoint, a scheduled run, a migration, or
a provider dispatch, and the next start reaches a consistent state — completing, rolling back, or
reporting the operation as failed. Never a half-applied change presented as success.

### I-025 — Content-addressed blobs are verified on read
**Class:** INTEGRITY · **Enforced by:** `storage/blobs.py` · **Proven by:** `test_i025_corrupt_blob_detected`

A blob read recomputes its digest and compares against the requested key. Mismatch raises rather than
returning corrupt data. Silent bit-rot returning wrong file content into a rewind would be far worse
than a loud failure.

---

## Agents and resources

### I-026 — Every agent run is bounded
**Class:** AVAILABILITY · **Enforced by:** `orchestration/budgets.py` · **Proven by:** `test_i026_budget_ceilings_enforced`

Tokens, turns, wallclock, cost, and spawn depth all have ceilings, resolved at run creation and
inherited-and-decremented by children. A parent cannot grant a child more budget than it holds.

An agent that spawns agents is an exponential cost function. This is the invariant that keeps a
runaway multi-agent run from producing a five-figure bill overnight.

### I-027 — The spawn graph is acyclic and depth-bounded
**Class:** AVAILABILITY · **Enforced by:** `orchestration/subagent.py` · **Proven by:** `test_i027_spawn_cycle_detected`

Each run carries its ancestor chain. Spawning an agent already in the chain is refused. Depth beyond
the configured maximum is refused. Both refusals are surfaced to the parent as a tool error it can
adapt to, not a crash.

### I-028 — Subagent transcripts never enter the parent context
**Class:** CORRECTNESS · **Enforced by:** `orchestration/subagent.py` · **Proven by:** `test_i028_child_transcript_absent_from_parent`

The parent receives only the structured result envelope. Context isolation *is* the mechanism by which
delegation compresses context; leaking the child's transcript defeats the entire purpose and would
make delegation more expensive than doing the work inline.

### I-029 — Cancellation is real
**Class:** AVAILABILITY · **Enforced by:** `runtime/loop.py` · **Proven by:** `test_i029_interrupt_bounded_latency`

User interrupt propagates as `asyncio` cancellation through the task group to every in-flight
operation: model streams, tool executions, subprocess groups, provider requests, subagent runs.
Bounded acknowledgement time. No operation is unkillable, and no cancelled write leaves partial state
(I-008).

Polling a flag between steps is not cancellation — a 90-second `pytest` invocation would ignore it.

---

## Permissions and trust

### I-030 — Grants are scoped and never implicit
**Class:** SAFETY · **Enforced by:** `permissions/grants.py` · **Proven by:** `test_i030_grant_scope_enforced`

Every grant is keyed by (project, capability, normalized target pattern) with lifetime `once`,
`session`, or `always`. A grant for one project never applies to another. A grant for one argument
pattern never widens to a different one. `once` grants are consumed on use.

Deny rules always beat allow rules, at every layer, with no exception path.

### I-031 — Executable extensions require content-pinned trust
**Class:** SAFETY · **Enforced by:** `hooks/trust.py` · **Proven by:** `test_i031_mutation_invalidates_trust`

Hooks, skill scripts, and plugins are executable code loaded from project files. First load requests
trust and records a content hash in `trust_grants`. Any modification invalidates the grant and
re-prompts.

`git clone && kalash` must never equal arbitrary code execution. Cloning a repository to look at it is
something people do reflexively, including with untrusted repositories.

### I-032 — Unattended runs cannot perform confirmation-class actions
**Class:** SAFETY · **Enforced by:** `scheduler/runs.py` · **Proven by:** `test_i032_unattended_stops_at_confirmation`

A scheduled or headless run that reaches an action requiring confirmation **stops and reports**. It
never auto-approves, and `approval_policy: never` does not mean "assume yes" — it means "do not ask,
and therefore do not do things that would require asking."

Scheduled runs default to `read-only`; write capability requires explicit per-schedule opt-in.

### I-033 — External content is never authoritative
**Class:** SAFETY · **Enforced by:** `runtime/context.py` · **Proven by:** `test_i033_injection_corpus_resisted`

Tool results, fetched pages, MCP responses, file contents, and recalled memories are wrapped in
delimited, labelled, untrusted blocks. Instruction-shaped text inside them is data to report, not
commands to follow.

Proven against an injection corpus covering direct instruction override, fake system messages,
fabricated tool results, encoded payloads, and instructions embedded in code comments and commit
messages.

### I-034 — Hook denials are final
**Class:** SAFETY · **Enforced by:** `hooks/runner.py` · **Proven by:** `test_i034_denial_never_bypassed`

An explicit denial from a hook is honoured at any depth, in any cycle, under any retry. The cycle-
breaking logic that skips *nested* hook invocations must never skip a denial. Loop-protection is not
an authorization bypass.

---

## Secrets

### I-035 — Secret values exist in exactly one place
**Class:** PRIVACY · **Enforced by:** `core/secrets.py` · **Proven by:** `test_i035_secret_corpus_absent_from_all_sinks`

Resolved secret values live in process memory and the OS keyring. Never in: config files, the
database, logs at any level, trace spans, audit rows, memory records, checkpoints, session exports,
crash reports, error messages, or telemetry.

Proven by a corpus test that seeds known secrets, exercises every subsystem including failure paths,
then greps every produced artifact. Error messages are a specific target — exception strings that
interpolate a connection string are a classic leak.

### I-036 — Secrets are referenced by name, never echoed
**Class:** PRIVACY · **Enforced by:** `core/redact.py` · **Proven by:** `test_i036_no_value_echo`

Where a secret must be discussed, only its key name appears (`ANTHROPIC_API_KEY`, not its value).
Applies to agent output, tool results, UI, and logs.

---

## Reproducibility

### I-037 — Sessions record everything needed to explain them
**Class:** CORRECTNESS · **Enforced by:** `storage/repositories/sessions.py` · **Proven by:** `test_i037_replay_completeness`

Each turn records: model ID and version, provider, sampling parameters, prompt template versions,
tool schema digest, skill and plugin versions, config digest, memory records injected (by ID), tool
inputs and outputs, environment digest, and timestamps.

"Replayable" must mean "we can reconstruct why the agent did that," not "we kept the chat log." A
session you cannot explain is a session you cannot debug.

### I-038 — Time is stored as UTC and computed in the user's zone
**Class:** CORRECTNESS · **Enforced by:** `core/time.py` · **Proven by:** `test_i038_dst_boundary_schedules`

All persisted timestamps are UTC with explicit timezone. Schedule next-fire times are computed in the
schedule's declared zone, honouring DST. Durations and timeouts use a monotonic clock, so a system
clock adjustment cannot cause a negative elapsed time or an infinite wait.

Proven across spring-forward, fall-back, a zone whose UTC offset changed historically, and a clock
stepped backwards mid-operation.

---

## Meta

### I-039 — Every invariant has a test
**Class:** INTEGRITY · **Enforced by:** CI · **Proven by:** `test_i039_invariant_coverage`

A test enumerates the IDs in this document and asserts that each has at least one test referencing it
by ID. Adding an invariant without a test fails CI.

An untested invariant is a comment. This test is what keeps this document from decaying into
aspirational documentation, which is the normal fate of files like this one.

### I-040 — Enforcement strength matches invariant class
**Class:** INTEGRITY · **Enforced by:** review · **Proven by:** manual audit, recorded per release

No `SAFETY` or `PRIVACY` invariant may rest on convention alone. Each must be enforced
architecturally, statically, or at runtime with a fail-closed default. Audited at each release and
recorded in the release notes.

---

## Invariant summary

| ID | Class | One line |
|---|---|---|
| I-001 | PRIVACY | Session transcripts stay local |
| I-002 | PRIVACY | Source-code egress is opt-in |
| I-003 | PRIVACY | All egress via the gateway |
| I-004 | SAFETY | Permission evaluation precedes execution |
| I-005 | AVAILABILITY | Memory failure never fails a turn |
| I-006 | PRIVACY | Redaction before transmission |
| I-007 | PRIVACY | Egress is audited |
| I-008 | INTEGRITY | Writes are atomic |
| I-009 | SAFETY | No write outside writable roots |
| I-010 | SAFETY | Protected paths always denied |
| I-011 | SAFETY | Sandbox fails closed |
| I-012 | CORRECTNESS | Edits require a fresh read |
| I-013 | INTEGRITY | Process trees fully terminate |
| I-014 | SAFETY | No network without capability |
| I-015 | SAFETY | Private address space denied |
| I-016 | CORRECTNESS | Memory carries provenance |
| I-017 | PRIVACY | Scope isolation holds |
| I-018 | PRIVACY | Deletion is durable and verified |
| I-019 | CORRECTNESS | Vectors compared within a version |
| I-020 | SAFETY | Memory is data, not instruction |
| I-021 | INTEGRITY | Message log is append-only |
| I-022 | INTEGRITY | Migrations forward-only and tested |
| I-023 | INTEGRITY | Exactly one writer |
| I-024 | INTEGRITY | No partial state after interruption |
| I-025 | INTEGRITY | Blobs verified on read |
| I-026 | AVAILABILITY | Agent runs are bounded |
| I-027 | AVAILABILITY | Spawn graph acyclic and bounded |
| I-028 | CORRECTNESS | Child transcripts stay isolated |
| I-029 | AVAILABILITY | Cancellation is real |
| I-030 | SAFETY | Grants are scoped |
| I-031 | SAFETY | Extensions need pinned trust |
| I-032 | SAFETY | Unattended runs stop at confirmation |
| I-033 | SAFETY | External content is not authoritative |
| I-034 | SAFETY | Hook denials are final |
| I-035 | PRIVACY | Secrets in one place only |
| I-036 | PRIVACY | Secrets referenced by name |
| I-037 | CORRECTNESS | Sessions are explainable |
| I-038 | CORRECTNESS | UTC storage, zoned computation |
| I-039 | INTEGRITY | Every invariant has a test |
| I-040 | INTEGRITY | Enforcement matches class |

---

*See also: [`capabilities.md`](./capabilities.md) for the capability vocabulary these invariants
constrain, [`threat-model.md`](./threat-model.md) for the attacks they defend against, and
[`evaluation.md`](./evaluation.md) for the test suites that prove them.*
