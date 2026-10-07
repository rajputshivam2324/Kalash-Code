# Kalash harness audit — 2026-10-04

Historical review snapshot. Follow-up implementation, regression results and
remaining limits are recorded in [session 1](HARNESS_SESSION_1_2026_10_05.md).
Keep the original findings and captured probe results as evidence.

Kalash's principal weakness is that its feature surface has grown faster than its
end-to-end guarantees. The repo has meaningful building blocks, but provider,
permission, accounting, persistence and public API contracts disagree at their
boundaries. More skills, a prettier terminal or a larger reasoning allowance will
not repair those disagreements.

This is a review of the current **dirty working tree**, including the preceding
TUI and opt-in spending changes. It is not a release certification or an audit of
every line or every possible input. Findings below distinguish **reproduced**
offline behavior, **inspection** of code paths, and **unproved** operational claims.
Runtime code was not changed in this review. No paid providers, actual publication,
real credential reads, live benchmark runs, commits or pushes were used.

## Evidence and scope

[Offline probe source](evals/harness_audit_2026_10_04/probes.py) and
[captured observations](evals/harness_audit_2026_10_04/results.json) cover 14 diagnostic
groups. These are observations of defects and controls, not 14 passing correctness
tests. A temporary pytest capture wrapper passed once in 1.21 seconds; it checked
capture completion, not that the harness was correct. Providers were fake SDK
objects/scripted events. SQLite storage and files were temporary. The stale-write
probe used the supported pre-edit hook seam. None of these proves an exploit in a
particular deployment or a live provider failure rate.

The prior 731-test pass, strict typing and selected lint pass remain useful
regression evidence. They were **not rerun for this review**, and do not erase the
new defects. Historical live evaluations still show Sarvam repair failures at both
tested output caps, and incomplete builds. See [the handoff](../NEXT_SESSION.md),
[repair results](COMPACT_EVAL_RESULTS.md) and [build evaluation](BUILD_JUDGE.md).
Those outcomes measure the model/configuration/controller/tool/evaluation system
together; they do not isolate a numerical harness score.

| Area reviewed | Main contract examined |
| --- | --- |
| Agent loop, request, execution, evidence | When a run can act, continue, fail and claim completion |
| Providers, gateway, normalized stream | Identity, arguments, signatures, usage, retry and termination |
| Permissions, grants, sandbox, trust | Authority precedence and actual containment |
| Filesystem, shell, search, web tools | Safe effects, bounded work, cancellation and observations |
| Sessions, serialization, storage | What survives interruption and what resume reconstructs |
| Context, compaction, scratchpad, instructions | Relevance, token admission and continuity |
| Memory and retrieval | Provenance, scope, correction and evidence quality |
| Delegation, agents, skills | Child authority, work isolation and loaded guidance |
| MCP client/manager/server, hooks | Extension authority, protocol boundaries and lifecycle |
| SDK, CLI, scheduler | Public promises versus connected runtime behavior |
| TUI, events and transcript | Streaming, ordering, reader control and lifecycle |
| Artifact inspection | Supported formats, resource limits and claims |
| Config, credentials, logging | Typed policy, secure storage and visibility |
| Tests, packaging, CI and evals | Independent proof and reproducible release criteria |

## Release blockers and reproduced defects

P0 below means fix before expanding usage with valuable workspaces or stronger
authority. P1 means a fundamental behavior or public contract is broken. These are
engineering priorities, not formal vulnerability scores.

### R1 — P0: a remembered approval can bypass an explicit deny

**Reproduced.** A session grant for `write` was inserted, an explicit deny rule was
added, and `ToolHost._gate()` returned permission to proceed. No effect was executed
in the probe. [The gate](../src/kalash/runtime/toolhost.py) checks its in-memory
grant set before calling the policy evaluator.

This makes an allow cache an authority source stronger than policy. Also, grant
keys are coarse: tool/program plus risk class, without complete argument, path,
host and confirmation scope. The control probe shows **commit and push currently
have different keys**; this review does not claim approving a commit approves a
push. The broader problem is that same-key operations can cover materially
different targets and intent.

**Change:** always apply immutable restrictions and current denies first. Evaluate
remembered grants inside the policy, scoped to canonical effects and confirmation
classes. Make revocation and changes to policy effective immediately. Define what
“allow session” means in the approval UI, including its target scope.

**Proof:** deny after grant; protected target after ordinary grant; changed host,
argv, workspace or policy; persisted grant on resume; revocation during a run.

### R2 — P0: stale-write protection has a race

**Reproduced.** The caller supplied the correct digest. A pre-edit hook changed the
file after validation. The write succeeded and replaced that intervening change.
[WriteTool](../src/kalash/tools/fs/write.py) validates the digest before an awaited
hook and later replaces the file without checking again.

Atomic replacement prevents a partially written file; it does not prevent lost
updates. Equivalent read/validate/await/replace paths need review in edit tools.
Canonical path validation also warrants symlink/rename race tests; those attacks
were not reproduced here.

**Change:** centralize conditional file replacement. Serialize cooperating writers
per canonical target, perform hooks before final validation, and revalidate the
target inside the critical section immediately before replacement. Define behavior
for external writers explicitly: a harness lock alone cannot promise universal
filesystem compare-and-swap. Preserve user work and surface conflicts.

**Proof:** hook edits, two agent writers, external editor changes, symlink swaps,
rename/delete, permission failure and interrupted replacement. Verify original
bytes survive rejected writes.

### R3 — P1: multi-edit can succeed after its own uniqueness condition becomes false

**Reproduced.** For `A B`, applying `A -> B`, then `B -> C`, produced `C B` with
success. Both patterns are unique in the original, but after the first edit `B`
is ambiguous. [MultiEditTool](../src/kalash/tools/fs/edit.py) validates every edit
against the original and then applies sequential replacements.

**Change:** choose explicit semantics. For sequential edits, validate each edit
against the working copy before touching the file. For original-coordinate edits,
calculate spans and reject overlap. Do not silently mix both models.

**Proof:** overlapping replacements, introduced matches, repeated matches, empty
patterns, Unicode and CRLF, with all-or-nothing file behavior.

### R4 — P1: accounting can become negative

**Reproduced.** A documented Anthropic usage shape with 100 uncached input, 1,000
cache-read, 200 cache-write and 10 output tokens normalized to **-900 input and
-690 total**. Fixture pricing yielded 0.00120 instead of 0.001500. These are fixture
prices, not a claim about current product pricing.

[The complete adapter](../src/kalash/models/providers/anthropic.py) and
[budget math](../src/kalash/core/budget.py) disagree about whether input includes
cache reads. Stream accounting uses another interpretation. Anthropic documents
the three input partitions as additive; cached tokens are not subtracted from its
uncached `input_tokens` field. [Official usage contract](https://platform.claude.com/docs/en/build-with-claude/prompt-caching#how-do-i-calculate-total-input-tokens-from-the-usage-fields).

**Change:** one canonical usage contract with disjoint billable partitions and
explicit subset fields for reasoning. Normalize once at each provider boundary;
share complete/stream behavior. Reject negative or nonsensical usage. Cost must
carry currency, pricing version and unknown status; unknown pricing is not free.

**Proof:** cached/uncached, zero-valued updates, cumulative/delta usage, reasoning
included in output, complete versus stream, interrupted usage and currency.

### R5 — P1: failed streams erase model-visible continuity and report zero unknown usage

**Reproduced.** The UI callback received `visible partial`, then the provider
errored. The loop returned an empty final response, retained zero assistant history
entries, and charged zero tokens. [The loop](../src/kalash/runtime/loop.py) returns
on stream failure before appending the assistant message. Recent TUI changes keep
the visible partial text; that does not make it durable or available next turn.

**Change:** preserve failed/cancelled attempts separately from successful assistant
messages. Record request identity, safe partial output, observed usage and unknown
settlement. Never execute unfinished tool intents. Give the next turn an accurate
failure receipt without pretending the attempt completed. Reserve/reconcile unknown
charges under a user cap; display uncertainty when uncapped too.

**Proof:** failure before tokens, after text, midway through JSON, after a tool
effect, cancellation, missing usage tail and process loss around settlement.

### R6 — P1: streaming fallback is not implemented at the actual failure boundary

**Reproduced.** A primary stream raised before yielding any event. With one attempt
and a fallback configured, primary calls were one, fallback calls zero, and the
error escaped. [Gateway retry](../src/kalash/models/gateway.py) can treat creation
of an async generator as success; iteration is where the request actually fails.
**Inspection:** agent construction wires the primary without resolving configured
fallback providers.

**Change:** make streaming attempt lifecycle explicit. Retry classified transient
failures at the iterator boundary, honor cancellation and retry-after, and stop
automatic replay once content/effects make it ambiguous. Only use fallback that
the user has authorized for data destination, capabilities and spending. Auth,
invalid request and access-denied errors need actionable repair, not blind retry.

**Proof:** transport failures before/after first event, rate limit, malformed request,
expired auth, capability mismatch, missing credentials, retry exhaustion and cancellation.

### R7 — P1: tool identity is not a reliable protocol invariant

**Reproduced.** OpenAI-shaped chunks with late ID/name produced a completed tool
block with empty ID and name, and no stream error. Two Google calls to `read`
produced the same ID, `read`. Sources:
[OpenAI adapter](../src/kalash/models/providers/openai.py),
[Google adapter](../src/kalash/models/providers/google.py),
[stream accumulator](../src/kalash/runtime/stream.py).

IDs connect the assistant intent, permission prompt, effect, receipt and UI row.
Missing/duplicate IDs can corrupt those associations even when JSON is valid.

**Change:** accumulate fragmented identity as well as arguments; require a known
tool name and unique internal call ID before execution. Keep provider wire identity
distinct when its protocol differs. Reject ambiguous intents, preserve ordering,
and validate tool arguments against the actual exposed schema.

**Proof:** late/fragmented names and IDs, interleaved parallel calls, duplicate
names, duplicate IDs, unknown tool, malformed JSON, empty arguments and late output.

### R8 — P1: thinking signatures are dropped

**Reproduced.** An Anthropic-shaped `signature_delta` arrived and the assembled
thinking block retained `None`. The adapter handles thinking text but not this
metadata. Opaque provider payload persistence also needs a full round-trip review.
The protocol explicitly includes signature deltas.
[Official streaming contract](https://platform.claude.com/docs/en/build-with-claude/streaming#thinking-delta).

**Change:** preserve signed/opaque metadata unchanged through stream, serialization,
resume and same-provider replay. Separate display/redaction from required wire
metadata. Unsupported cross-provider conversion must be explicit.

**Proof:** signed and redacted thinking, omitted display, continuation after a tool,
resume, compaction, malformed metadata and provider switching.

### R9 — P0/P1: configuration is not typed policy

**Reproduced.** `KALASH_PERMISSIONS__NETWORK=false` becomes the string `"false"`,
which is truthy. [Configuration](../src/kalash/core/config.py) injects env values as
strings without parsing booleans consistently. Its single-underscore naming comment
also disagrees with the double-underscore implementation.

**Inspection:** malformed JSON is silently replaced with `{}`; unknown or
misformatted fields can be ignored; project budget keys are not among protected
security keys and can override a user's ceiling. Multiple writers/loaders and the
saved provider selection complicate precedence. Configuration writes are ordinary
overwrite operations, with concurrent-update and interruption risks.

**Change:** one validated schema for every input source, with strict booleans,
finite nonnegative numbers, enum validation, unknown-key diagnostics and provenance.
Reject invalid safety/spending configuration rather than silently defaulting it.
Projects may tighten a user ceiling, never relax it without the user's explicit
override. Show the effective setting and its source. Save atomically with conflict
detection.

**Proof:** `false`, `0`, null, negative/NaN values, typo, malformed file, conflicting
layers, missing environment reference, concurrent config writes and project cap removal.

### R10 — P1: resume reconstructs a different run

**Reproduced.** A session in workspace `first` consumed 110 fixture tokens. Resuming
it from `next` selected workspace `next`, reset the active budget counter to zero,
and did not restore the saved compaction summary. Sources:
[agent construction](../src/kalash/runtime/agent.py),
[history](../src/kalash/runtime/history.py),
[session persistence](../src/kalash/runtime/session.py).

Older full history remains stored; this is not a claim that every compacted fact
is deleted. It means the active projection and spend state are not reconstructed.
If limits are intended per run rather than per session, say so explicitly and keep
durable session/attempt totals independently. Do not make resume a hidden cap reset.

**Change:** restore session workspace identity, or require an explicit fork to a
different workspace. Persist/rebuild compaction projection, attempt ledger,
permissions and pending effect state. Add run ownership/fencing against two writers.

**Proof:** resume from another cwd, after compaction, after failure, after effect
before receipt, after usage before settlement, deleted workspace and concurrent resume.

### R11 — P1: the SDK's streaming and model overrides are misleading

**Reproduced.** [SDK `stream()`](../src/kalash/sdk/__init__.py) yielded nothing while
the fake agent was running, then emitted joined text after completion. A requested
model label was reported while the underlying routed provider remained unchanged.

**Inspection:** `config_overrides` is stored but not applied; per-call tool filtering
unregisters tools from the session permanently; normal completion omits termination
and error details; counters are cumulative although callers can read them as request
usage; declared tool-call output is not populated.

**Change:** a bounded async event queue yields during execution, with cancellation
and consumer backpressure handled deliberately. Per-call options build scoped
request/runtime state, resolve the actual provider and restore session defaults.
Expose actual model, finish reason, errors, tool receipts and both attempt and session
usage. Do not let the SDK become a second, weaker controller.

**Proof:** first chunk before completion, early consumer exit, slow consumer,
concurrent calls, switch providers, filtered call followed by unfiltered call,
configured cap, refusal and partial failure.

### R12 — P1: the scheduler's connected public path is broken

**Reproduced.** A schedule created through `SchedulerManager` caused the daemon's
due query to raise `OperationalError: no such column: next_fire_at`. The schema and
manager store `next_run_at`; the daemon also expects `cron` and failure fields that
do not match this schema. A manual trigger returned a session ID and wrote a
`running` row, but no corresponding session existed and no agent was started.
Sources: [manager](../src/kalash/scheduler/manager.py),
[daemon](../src/kalash/scheduler/daemon.py),
[migration](../src/kalash/storage/migrations/__init__.py).

**Inspection:** model/turn options are accepted but not stored; invalid cron can
silently create an inert schedule; cancellation changes database status without
cancelling a live task; lease takeover lacks a compare-and-swap fence; renewal
does not revoke leadership when ownership is lost. Execution awaits one job before
checking the next, and declared catch-up/overlap behavior is not fully connected.

**Change:** treat this surface as experimental until manager, schema, daemon and
run state share one contract. Manual and scheduled triggers must enter the same
durable queue. Use fenced leases, real cancellation, bounded workers, explicit
timezone/catch-up/overlap semantics and per-job workspace/authority/limits.

**Proof:** CLI add to daemon execution on a fresh DB, manual trigger to receipt,
two daemons, lease expiry during a run, long job beside short job, DST, downtime,
cancel_previous, queue, disabled schedule and crash recovery.

## Other systemic weaknesses found by inspection

### I1 — P1: credential “encryption” provides little protection against file disclosure

[Provider credential storage](../src/kalash/models/auth_store.py) derives its key
from `/etc/machine-id`, or the home-directory path. Neither is a user secret.
It uses custom XOR keystream encryption without an authentication tag. Anyone with
the encrypted file and that derivation input can decrypt it; modified ciphertext
has no cryptographic integrity check. File mode 0600 is useful access control, but
does not make this secure secret-key encryption. Files are written before chmod,
and concurrent saves can overwrite one another.

Use an OS credential store, with an explicit fallback policy. If encrypted-file
storage is required, use standard authenticated encryption with a genuinely secret
key stored separately, restrictive permissions at creation, atomic updates and
careful migration. Do not promise protection from other processes running as the
same user. Consolidate provider and MCP credential handling. Mask API-key input in
the TUI; `_ask_for()` currently uses the ordinary composer without enabling password
mode. Secret entry should not become a transcript or clipboard convenience path.

### I2 — P1: tool-safe paths and OS confinement are different boundaries

Filesystem tool protection does not automatically protect the same files from
shell execution. The Linux sandbox binds writable workspace roots; it does not
mirror every protected-file rule as a read-only overlay. An interpreter can touch
targets that argv classification does not enumerate. This is a policy-boundary
gap found by inspection, not a demonstrated OS sandbox escape.

The macOS backend's `wrap_command(cmd)` does not accept the `cwd` argument required
by the manager, and requires a prepared profile that this path does not establish.
The mismatch makes restricted shell support fail closed; this was not tested on
a physical Mac. On Linux, status probing also confuses a support property with a
callable. Preflight verifies command functionality, not a full set of forbidden
writes/network operations.

Create an explicit authority matrix for each tool, shell, hook, MCP process and
platform. Enforce protected filesystem targets at the effect boundary where
possible. Test actual denied operations, not only whether `/dev/null` and DNS work.
Unsupported confinement must remain visible and fail closed in restricted modes;
do not fix compatibility by silently running unrestricted commands.

### I3 — P1: observation limits often arrive after the expensive operation

`ReadTool` reads and decodes the whole file before limiting returned lines.
Search uses `communicate()` and per-file match bounds, which do not bound aggregate
captured bytes. Web fetch buffers the response before its size check; HTTP errors
can become successful observations. Redirect validation must occur before each
request, not just after following it. Allowed destinations and private-address
access need a deliberate policy for web tools and remote extensions.

Some scratchpad regex/file operations are synchronous. An async timeout cannot
interrupt a blocked event loop. Search subprocess cancellation needs process
cleanup even when cancellation originates outside the tool's timeout branch.

Bound bytes, matches, decompression, CPU and subprocess lifetime **while processing**.
Use incremental reads and drains; disclose truncation with a continuation reference.
Keep full evidence outside repeated model context when authorized. Reap process
groups on cancellation. HTTP status and timeout must be actionable error envelopes.

### I4 — P1: “the model stopped calling tools” is being promoted to task success

The loop's `NO_TOOL_CALLS` is a protocol termination, not proof of completion.
Scheduler and child-agent paths use it as their success condition. A model can stop
after writing one file, omitting tests or leaving a broken app. A shell exit code
of zero does not prove the correct checks ran against the final artifact.

Introduce distinct states: stopped, failed, refused, cancelled, partial, and verified
complete. For build/repair tasks, preserve an explicit task contract and verify its
deliverables using fresh evidence tied to the final file/tree versions. A later
mutation invalidates earlier evidence. For explanation/research tasks, use an
appropriate completion contract; do not force irrelevant tests on every response.
The final answer should expose completed work and remaining work accurately.

Add progress signals for repeated identical reads, repeated refusals, unchanged
errors and no artifact advancement. Recovery should address a cause rather than
spend 100 turns repeating it. Avoid a rigid controller that blocks legitimate
exploration; evaluate false stops as well as infinite loops.

### I5 — P1: too many pieces of state claim to describe the same run

Conversation history, session rows, scratchpad, compaction summary, tool evidence,
budget state, UI events and scheduler runs can disagree. Successful-message
persistence alone cannot recover an intent whose effect happened but whose receipt
was not recorded. Frozen dataclasses also contain mutable lists/dicts; they are
not deep immutable request snapshots by themselves.

Adopt a durable attempt/effect lifecycle incrementally: request admitted, attempt
started, stream settled/failed, intent validated, approval decided, effect started,
effect observed, verification recorded, turn settled. Assign stable run/attempt/call
IDs. Reconstruct model context and UI projections from the same durable facts,
with explicit safe handling of uncertain effects. Deep-freeze/copy admitted request
data. Do not replay a possibly completed non-idempotent effect after a crash.

SQLite WAL and owned transactions are valuable, but `synchronous=NORMAL` is not a
promise against all power-loss cases. Atomic file replacement plus file fsync is
not a complete directory durability guarantee. Session deletion uses separate
operations, and updated-at/reporting semantics deserve consistency tests.
Serialization needs explicit version migration and errors for incompatible records,
rather than silent loss of unfamiliar blocks.

### I6 — P2: context management spends on accumulated state rather than relevance

Scratchpad notes and indexes can be rendered without an aggregate bound. User
anchors and loaded instruction content accumulate. Skills/ancestor instruction
limits are individually bounded without a unified admission policy. Character-based
token estimation is not reliably conservative for every Unicode, code, image or
provider case. Scratchpad prompt sections are assembled at send start, so state
created within a turn is not always reflected as fresh authoritative context.

Define one context allocation policy across identity, user objective, active plan,
recent receipts, selected files, memory and extension schemas. Select observations
by task relevance and keep large originals retrievable by reference. Scope nested
instructions to the applicable files. Compaction must preserve authority, unresolved
work, exact tool relationships and file versions, then survive resume. Use provider
token counting where available and measure estimator error elsewhere. Context
admission limits remain necessary even when monetary limits are disabled.

### I7 — P1/P2: memory provenance can upgrade an agent's claim into user authority

The model-callable remember path records `Source.USER_STATED` and `Trust.HIGH`.
Automatic extraction uses heuristics whose confidence can exceed what the source
warrants, particularly quotations, negations or outdated project preferences.
Memory retrieved from another run is not more authoritative than current repo facts
or the user's current request. Local FTS is a reasonable initial implementation;
adding embeddings does not correct false provenance.

Separate user-confirmed instructions, agent inference and observed evidence.
Preserve source references, scope, time, confidence and correction history. Require
appropriate user intent before durable preference writes. Implement contradiction,
expiry, forget/export and exclusion behavior. Evaluate memory-on versus memory-off
for both accuracy and cost, including cross-project leakage and malicious retrieved
instructions. Treat retrieved content as data, not permission.

### I8 — P1: extensions increase authority faster than their validation proves

MCP side effects can be inferred from tool names/descriptions, and dynamic schemas
are reduced to permissive fields. Name heuristics are not a security classification
for arbitrary third-party tools. Unknown tools should not quietly become safe to
parallelize or usable in plan mode. Multimodal/structured results need a declared
conversion contract, rather than incidental JSON flattening.

Project hook configuration is hash-trusted, which is useful. That does not pin every
referenced executable/module; trusted hooks run with substantial host authority.
Clarify this scope to the user. Distinguish advisory hooks from mandatory safety
hooks and define timeout/failure behavior. Do not swallow a mandatory check failure
and continue the effect.

The custom MCP server reads a declared body before authenticating and lacks an
explicit body limit/read deadline. Its all-interface guard checks `0.0.0.0`, not
all possible non-loopback addresses. Its raw HTTP/JSON-RPC implementation needs
method, notification, lifecycle, concurrency and malformed-input interoperability
tests. It is not established as a hardened network service. Client transport bounds
are better, but reconnect/cancel/auth refresh need tests with real protocol peers.

Use trusted side-effect/capability metadata, full schema validation and explicit
authority delegation. Unknown extension risk defaults to restricted. Prefer a
maintained protocol implementation where that reduces custom security obligations.
Authenticate early, bound requests, validate bind targets and keep loopback default.

### I9 — P2: delegation is not isolated concurrent development

Children have useful context and permission separation, but share the same working
directory. That is not filesystem isolation. Child persistence is disabled and
partial outputs can be truncated/discarded on failure, so parents can miss effects
already performed. The default execution classification serializes task calls;
there is no demonstrated parallel team speedup. Children use the same provider,
not a measured cheaper routing policy.

Use isolated worktrees for independent edits, explicit read-only delegation for
analysis, and reviewed merges. Persist parent-child IDs, actual usage, effects and
partial outcomes. Define cancellation propagation and reservation reconciliation.
Only introduce concurrency when file ownership and merge/conflict behavior are
tested; more agents can increase cost and inconsistency.

### I10 — P2: the TUI still needs a product contract and long-session proof

The reported modal/transcript crash and late-event contamination have local fixes
and regressions. Do not present them as unfixed. Streaming is coalesced and text
settles into Markdown at boundaries. Nevertheless, repeatedly joining/repainting
a growing full message can become expensive, and accumulated widgets lack a
demonstrated long-session memory bound. Markdown settling can reflow the viewport.
Global event handlers are awaited; slow subscribers can delay execution, while
swallowed subscriber errors can hide broken presentation. Control/policy events
must be separate from best-effort presentation events.

The product layout should maintain chronological user/assistant turns, compact
tool summaries with expandable evidence, one clearly scoped activity indicator,
an explicit terminal outcome, readable errors and a stable composer. Preserve a
reader's scroll position and selection; expose new output without pulling them
away. Add multiline/paste handling, deliberate cancellation keys, visible approvals
and masked secrets. Avoid putting internal implementation trivia into normal flows.

Measure first-token latency, frame work, dropped updates, CPU/memory and scroll
stability on long streams. Exercise resize, modal allow/deny during output, SSH,
narrow terminals, light/dark themes, wide Unicode, large logs and long sessions.
Headless widget tests are essential but do not prove physical-terminal behavior.

### I11 — P1/P2: machine-readable CLI output loses useful truth

The pipe path assigns aggregate tokens to `input_tokens`; output/cost fields do not
receive equivalent accurate breakdowns. Tool-output chunks are truncated to 500
characters without a complete-output contract, and completion events omit call IDs.
This makes automation weaker than the TUI's internal events.

Version one public event schema shared by CLI/SDK/UI projections. Include run,
attempt and call identities, chunk sequence, truncation/reference metadata, actual
model and explicit terminal result. Keep stdout machine-clean and diagnostics on
stderr. Test consumer reconstruction, cancellation, refusals, late output and
per-request versus cumulative metrics.

### I12 — P2: artifact support and skills are capability descriptions, not quality proof

Bounded archive/XML/image inspection and optional parser errors are useful. They do
not establish OCR, visual understanding, formula calculation, fidelity-preserving
editing or native support for every office format. Parser CPU/resource behavior
still belongs to the execution sandbox. Complex/untrusted files need adversarial
format tests, not only successful small fixtures.

Publish a capability matrix that says inspect/extract/edit/render/verify separately,
including optional dependencies. Skills should explain supported workflows and
verification; they must not imply capabilities absent from tools. Add compatibility
and regression cases where a capability is actually claimed. An external TUI skill
would not fix provider, ledger or effect-state defects.

### I13 — P1: redaction and error visibility do not form a complete data boundary

`core/privacy.py` scrubs common textual content and exact known environment secrets,
but deliberately leaves signed/opaque reasoning and image content unchanged. Raw
text deltas reach presentation before the settled message is scrubbed. Independent
chunk redaction cannot reliably recognize a credential split across chunks. Error
strings and extension outputs also need consistent treatment. These are inspection
gaps; this review did not transmit a real secret or demonstrate comprehensive DLP.

Host-readable data is not automatically appropriate to send to a model, especially
when switching providers. Secret-bearing files should be excluded before admission,
with narrow exceptions based on user intent. Preserve required signed wire metadata
unchanged while controlling whether the source is admitted and whether it is shown.
Define protected storage for sensitive raw evidence, retention and export behavior;
do not log credentials while trying to diagnose errors.

The error taxonomy should distinguish provider rejection, incomplete stream,
permission refusal, stale write, tool failure, storage failure and presentation
failure. Silent defaulting and broad catch-and-log paths can hide why a feature is
missing or broken. Keep causal identifiers and safe remediation in the result. A
failed UI subscriber must not silently turn a healthy run into an invisible one,
and a failed mandatory check must not become an advisory warning. Test split-secret
output, logs, images, provider switching and simultaneous subsystem failures using
synthetic credentials only.

## Cost design: where the earlier idea was right and where it was incomplete

Making token and money limits user-controlled was the right direction. Silent
shrinking of generation capacity can damage reasoning and tool-call completion.
The current opt-in limits improve that policy. But removing caps does not create
efficient execution, and an unconstrained failed run can become more expensive.

Separate three concerns:

1. **User authority:** optional spending ceilings and authorized provider destinations.
2. **Operational safety:** context, output bytes, process lifetime, turns, concurrency
   and spawn depth. These prevent hangs and resource exhaustion even when uncapped.
3. **Execution efficiency:** relevant context, reliable tools, cache reuse, bounded
   observations, successful recovery and measured model/effort routing.

The optimization objective should be **expected cost per independently verified
completion**, subject to correctness, latency and permission constraints. Include
failed attempts, retries, compaction and child calls. Cheapest per-token pricing
can lose after repeated failures; higher effort can also waste money when the
task is straightforward. Unknown usage/currency/pricing must remain unknown, not
become zero USD. A cap refusal should clearly return partial work and a continuation
option; it must never be graded as complete.

Start with deterministic savings: selective read/search, stable prompt prefixes,
schema pruning without changing authority, observation references and less repeated
exploration. Then test effort/model routing on a held-out task set with escalation
when checks fail. Preserve user choice and disclose provider switching. Do not add
an opaque router before the adapters and accounting are trustworthy.

## What to learn from DeepSeek Harness

The useful comparison is architectural, not a score. Its current architecture
documents distinguish durable session facts from live agent events, derive model
history from the session log, and prepare/freeze requests before streaming. This
offers a concrete direction for Kalash's fragmented state.
[Upstream architecture](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md).

Adopt those invariants in Python incrementally. A Cordis/plugin rewrite is not
required. Upstream documentation is mutable and is not proof that its implementation
passes Kalash's tasks, permissions or crash cases. Neither harness has a measured
comparative score here. Upstream also describes boundaries around interrupted
attempt settlement; do not assume another product provides universal crash durability.
Matched trials must hold model, task, tools, permissions, environment, limits and
grading constant before making performance claims.

## Failure matrix for the next reliability work

| Case | Current evidence | Required result |
| --- | --- | --- |
| Deny after remembered approval | Reproduced bypass | Deny wins, no effect |
| File changes during pre-edit hook | Reproduced lost update | Conflict, user bytes retained |
| Sequential edit introduces ambiguity | Reproduced wrong success | Explicit reject or defined span semantics |
| Cached provider input | Reproduced negative totals | Nonnegative partitions and exact fixture cost |
| Stream dies after visible text | Reproduced history/usage gap | Failed attempt survives; unknown usage explicit |
| Primary fails before first event | Reproduced absent fallback | Classified retry/fallback or actionable terminal error |
| Late/interleaved tool identity | Reproduced empty identity | Valid unique identity before any effect |
| Two same-name Google tools | Reproduced collision | Distinct receipts and wire-correct continuation |
| Signed thinking continuation | Reproduced metadata loss | Lossless same-provider round trip |
| Environment network=false | Reproduced truthy string | Typed false through effective policy |
| Project tries to relax user cap | Inspection | User ceiling remains authoritative |
| Resume from another workspace | Reproduced changed cwd | Restore original or explicit fork |
| Resume after usage/compaction | Reproduced reset/lost projection | Durable accounting and projection semantics |
| SDK streaming/model option | Reproduced buffering/mislabel | Early chunks and actual selected provider |
| CLI add -> daemon/manual trigger | Reproduced disconnected paths | Real queued run and durable terminal receipt |
| Crash after non-idempotent effect | Unproved | Mark uncertainty; never blind duplicate |
| Two sessions edit same target | Unproved | Conflict isolation and reviewed merge |
| Subprocess cancel with grandchildren | Partly covered; audit needed | All owned processes reaped; output retained |
| Large file/log/HTTP/regex | Inspection resource gaps | Bound work before memory/CPU exhaustion |
| Redirect/private destination | Inspection policy gap | Each destination authorized before request |
| Unknown/malicious MCP tool | Inspection | Explicit capability gate and untrusted observations |
| Credential file copied/tampered | Inspection weak key/integrity | Honest threat model and standard storage |
| Secret split across stream chunks | Inspection redaction gap | Synthetic secret excluded from display/logs/admission |
| macOS restricted command | Inspection signature mismatch | Native integration test; fail closed otherwise |
| Final edit after successful tests | Inspection completion gap | Previous verification invalidated |
| Partial artifact + model stop | Demonstrated live build failure | Partial status, missing deliverables visible |
| TUI modal/late output | Regressions fixed locally | Preserve pass under sustained terminal traffic |
| Long run/resize/slow subscriber | Unproved | Stable UI, bounded memory, responsive cancellation |
| Compaction/memory injection | Inspection | Objective and authority preserved; scoped retrieval |
| Dependency/provider SDK update | Unproved across matrix | Min/latest contract fixtures and wheel smoke |
| Multiple scheduler leaders | Inspection fencing gap | Single fenced effect owner and real cancellation |

## How to change the engineering process

The present test suite proves many local behaviors while missing whole public
paths. SDK buffering and scheduler schema drift are precisely the kind of defects
an integration contract should catch. Strict typing cannot validate a provider's
dynamic wire protocol or an SQL column hidden in a string. Test quantity is not a
release criterion.

Keep the existing offline suite and add a small set of high-value contract tests:
provider-wire fragmentation, connected CLI/SDK/scheduler paths, conditional writes,
attempt settlement and recovery. Use fault injection at each await/effect boundary,
property-based malformed-event tests and negative controls. Never weaken a grader
to make a partial build pass. Record repaired grader defects with revisions and
retain failed runs.

CI currently runs Python 3.12 on Linux, while package/runtime/platform claims are
broader. Add wheel installation smoke tests in a clean environment, supported
Python/platform lanes and minimum/latest compatible dependency checks. The selected
F/I/UP lint gate is not the full configured Ruff rule set; address existing security
and correctness findings by cause instead of claiming full lint success or broadly
disabling rules. Keep optional dependencies genuinely optional and test missing them.

Real-model evaluation needs a separate, user-authorized lane. Freeze task/rubric,
workspace, interpreter, dependency versions, model version/options and ledger scope.
The build grader currently invokes `/usr/bin/python3`; builder/grader environment
equivalence must be explicit. Include broken/reference controls and fresh checks
against required files. Use multiple runs and held-out tasks, preserving all failures.
Classify model failure, provider failure, refusal, grader defect and infrastructure
failure separately. Report verified completion rate, false-completion rate, cost
per success, latency, retries and recovery outcomes. Small samples need uncertainty,
not a fabricated 9.5/10.

## Implementation order and acceptance gates

1. **Authority and data preservation:** R1, R2, R3, R9, credential storage and
   protected-target boundaries. Gate: all negative authority/conflict cases cause
   no unauthorized or lost update.
2. **Provider and accounting contracts:** R4–R8. Gate: complete/stream fixtures
   agree, identity/signatures survive, incomplete calls never execute, and every
   attempt has honest usage settlement.
3. **Durable run state and truthful completion:** R10, I4, I5. Gate: resume and
   injected failures reconstruct the same facts; uncertain effects are not replayed;
   partial artifacts cannot become verified-complete.
4. **Connected product surfaces:** SDK, CLI and scheduler. Gate: exercise each
   public option through its actual runtime and fresh schema, including failure,
   cancellation and concurrency. Hide unsupported claims until those paths pass.
5. **Bounded context, tools and presentation:** resource processing, compaction,
   memory provenance and long-session TUI. Gate: bounded resource use and retained
   task/authority state under sustained load.
6. **Measured cost and model quality:** user-authorized matched trials only after
   deterministic contracts pass. Gate: verified completion and honest cost improve
   on held-out tasks, with failures and uncertainty preserved.

Avoid unrelated feature expansion until these gates are met. Keep the module
structure, useful safety checks, failed eval evidence and recent TUI fixes. Replace
ambiguous cross-module contracts rather than rewriting the entire project at once.

The release target should be a matrix of demonstrated guarantees. Kalash currently
has enough confirmed failures that calling it production-reliable or above 9.5/10
would be unsupported. The path forward is concrete, but it requires runtime and
evaluation discipline rather than another layer of capabilities.
