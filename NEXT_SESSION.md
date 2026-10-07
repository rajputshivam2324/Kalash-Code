# Kalash reliability handoff

## Current baseline — session 3, continued 2026-10-07

Read [the session 3 report](docs/HARNESS_SESSION_3_2026_10_05.md) and
[recovered evidence](docs/evals/harness_session3_2026_10_05/recovery_index.json).
This section supersedes the historical pause and category-status statements below.
The user authorized a **new separate 200,000 conservative-token allowance** for
broader real Sarvam repair/build/recovery evaluation. Observed cumulative charges:
**187,770 / 200,000**, leaving **12,230** mathematically. The previous separate
60,000 allowance (29,953 charged) and historical 240,000 allowance (219,708 charged)
were not reset or combined. No paid calls occurred during the October 7 continuation.

Real output: the first frozen repair matrix passed **2/4** (decimal-ledger and
streaming-jsonl); TTL/LRU and DAG failed. Build and recovery each passed **1/10**
independent acceptance checks, with all 11 candidate tests failing. A prior
intermediate summary incorrectly said 1/12; the frozen rubric has 10 checks.
Later TTL retries failed too. One retry is specifically a harness regression:
lint cleanup changed boolean precedence and duplicated tool identities (`readread`).
The provider-wire regression now prevents this; the final live retry had normal
identities but made no patch before reservation refusal. Real-model reliability
and successful build recovery remain unproved. Do not claim 9.5/10 or a comparison win.

Session 3 local work covers categories **11–14**: bounded transcript scrollback and
presentation delivery; public run/attempt/fallback events and SDK verification;
nested MCP schema/HTTP/OAuth validation and required-hook failure handling; full-rule
lint, Python-version CI, package/resource checks, and persistent eval output defaults.
The final October 5 suites passed **824 tests on Python 3.12 and 3.14**; strict mypy
passed **169 source files**. The final October 7 suite passed **840 tests** on
Python 3.12, mypy passed 169 files, all configured Ruff plus unused-annotation
checks and formatting passed, and Linux PTY/base-wheel checks passed. Evidence is
in `docs/evals/harness_session3_2026_10_05/`; the CI matrix is configured, not
remotely executed. October 7 additional regressions have not been rerun on 3.14.

The October 5 `/tmp` run directories disappeared across the interruption/restart.
Selected historical tool-result excerpts were recovered from this conversation;
they are **not full provider traces, original manifests, or resumable ledgers**.
The generated triage program was recovered with its original SHA-256 and rechecked
offline against the unchanged acceptance file: **1/10**, OS sandbox enforced.
Do not invent per-request charges or create an empty ledger against the remaining
allowance. The report preserves observed totals and the evidence gap explicitly.
Future compact/build CLI runs default to ignored `.kalash/eval_runs/` rather than
temporary storage. Preserve original historical docs/reports and recovered excerpts.

Final review also reproduced and fixed CLI failure output dropping run/verification/
file-operation fields, SDK accounting depending on an exhausted event collector,
and deleted trusted hooks disappearing at the next startup. Hook authorization
records now validate strictly, use private atomic writes, and require explicit
`kalash hooks untrust PATH` to revoke a missing policy file. Filesystem tools and
existing OS sandbox overlays protect the user authorization registry. External
privileged deletion of the whole registry and symlink inventories are not attested.
The final three regressions cover relative and symlinked `KALASH_HOME` protection
and repointing a home alias to forged authorization records. The configured home
is now resolved and pinned before tools run. Latest evidence is
[canonical-home verification](docs/evals/harness_session3_2026_10_05/verification_canonical_home_2026_10_07.json);
earlier verification records remain preserved.

Continue from demonstrated failures: reduce repeated exploration and large recent
observations before reservations become impossible, then test actual read → edit →
independent check completion under an intact user-scoped ledger. Preserve OS sandboxing,
permission gates, digests and independent graders. The user subsequently authorized
committing this reliability work on October 7. No push is authorized.

The sections below are historical records, not current spending or completion authority.

## Latest continuation — real Sarvam output, 2026-10-05

Read [the live continuation report](docs/SARVAM_RELIABILITY_2026_10_05.md)
and [durable evidence](docs/evals/sarvam_reliability_2026_10_05.json) first.
The user resumed live testing and explicitly authorized **one end-to-end trial
with a new separate 60,000 conservative-token allowance**. That trial is done;
this supersedes the historical statements below that no live call is authorized.
It does not authorize automatic further tasks or reset the old allowance.

Actual Sarvam-105B V1 result, reasoning off / 6,144 output allowance: **0/1 TTL/LRU
repairs**, no source change, 11 completed provider requests and 11 tool attempts,
**29,953 reported/conservatively charged tokens**. The next request was refused
by the existing 52,000-token task reservation ceiling. The new suite has
**30,047 tokens left**. Historical charges remain **219,708 / 240,000**, untouched.

The model repeatedly searched for absent tests and omitted the digest on its
write. Safety refused the overwrite. Offline independent grading of the rejected
proposal also failed true LRU behavior; adding a digest would not establish
success. No output truncation occurred in this trial. Distinguish model task/
argument failure, controller reservation refusal, and successful provider/tool
transport. Do not claim improved real-model reliability or cost per success.

Local CLI fixes now preserve input/output/cache/reasoning breakdowns, actual
budget ceilings, failure accounting and completion status, tool call IDs,
source-bounded output chunks and error evidence. Budget refusals exit 4 instead
of generic failure 1. See [the current output contract](docs/CLI_OUTPUT.md).
Eval options select one frozen task/reasoning setting. Future trials capture
redacted normalized requests and stream metadata; lexical delta bodies are
omitted. This live run preceded that capture, so its missing request evidence
cannot be reconstructed from synthetic fixtures.

Full pytest: **804 passed in 50.87s**, one existing offline Anthropic warning.
After strengthening split-fragment trace protection, focused CLI/eval checks:
**34 passed in 1.92s**. Strict mypy: **166 files passed**. Full configured Ruff
passes the six touched Python files; whole-repository Ruff still has **760
findings**, including dirty work. Formatting: **255 files passed**; whitespace
passed. No commits, pushes, historical artifact rewrites or ledger resets.

The latest user request prioritized a real trial; **categories 11–14 remain
incomplete**. Continue local TUI/terminal, complete public CLI/SDK events,
extension/schema/server validation and release/lint work. Do not silently declare
the three-session plan complete. Further live work needs a defined user scope;
carry this new ledger forward sequentially if that allowance is explicitly
resumed, using the preserved evidence if temporary files disappear.

## Three-session fix plan — latest direction, 2026-10-05

The user asked to fix the 14 categories from the comprehensive critique in three
sessions: **1–5 now**, **6–10 next session**, **11–14 in session 3**. Session 1
implementation is recorded in [the session report](docs/HARNESS_SESSION_1_2026_10_05.md).
Its numbering refers to the critique's categories, not audit findings R1–R5.
Session 2 (**6–10**) is now implemented with local contract regressions; read
[its report](docs/HARNESS_SESSION_2_2026_10_05.md). Start the next session with
**11–14** after inspecting the current dirty tree.
Do not silently start another batch in this session. Preserve the user's work.

Session 1 covers core contracts, truthful completion, durable state, authority
boundaries and credential/privacy handling. Provider identity/cache/signature
fixtures, early SDK streaming and actual model routing, fresh-schema scheduler
triggers, retry reservations, denial/grant scope, safe sequential multi-edit,
post-hook stale-write detection, committed stream progress, receipt recovery,
session ownership/workspace/accounting/projection and caller-owned verification
now have regressions. Credentials use authenticated AES-GCM with a random separate
0600 local file key; malformed storage is not silently replaced. See the report
for implementation details and acceptance limits.

New runtime modules: `runtime/journal.py`, `runtime/completion.py`,
`core/secure_io.py`. Storage migrations 3–4 add journal/checkpoint and connected
scheduler schema. New regressions are `test_harness_authority.py`,
`test_harness_recovery.py`, `test_harness_connected.py`. Existing permission/MCP,
child status and credential cache tests were adjusted to the new explicit
contracts; historical diagnostic probes/results remain unchanged.

Do not claim universal filesystem CAS, exactly-once external effects, complete
secret detection, physical macOS/Windows sandbox coverage, all scheduler policies,
or real-model reliability. Source-workspace verification excludes dependencies,
Git metadata and generated caches; it is not environment attestation. Protected
OS overlays cover existing targets, not arbitrary future filename globs. Process
host allow-list mode now denies subprocess networking until an enforcing proxy
exists. File-key encryption protects disclosure of auth.json alone, not a process
that can read both credential files.

Session 2 (6–10), local implementation recorded below: optimize cost per verified result without hidden spending caps;
improve relevant context/compaction; prevent memory from becoming user authority;
bound resource/subprocess/journal processing; qualify delegation and workspace
isolation. Session 3 (11–14): sustained terminal/TUI tests, truthful complete CLI
and SDK/event surfaces, extension/capability validation and release/evaluation
discipline including full-rule lint debt. Live benchmarks remain paused. No paid
calls, commits, pushes, historical probe overwrites or ledger resets occurred.

## Current instruction — start here next session

The user resumed local reliability fixes on 2026-10-03 and wants a **reliable harness**.
The measured real-model results are poor. Continue with diagnosis and local fixes;
do not treat feature count or passing unit tests as proof of agent reliability.
Live benchmarking is paused until the user requests another live evaluation.
Local reproductions and runtime fixes are authorized by the latest user request.
No live model calls, commits or Git pushes are authorized.

Latest user direction (2026-10-03, continued 2026-10-04): fix TUI flickering,
streaming and transcript structure, and make spending limits user-controlled.
The user then reported a mid-run `NoMatches: #transcript` crash and requested a
comparison with `https://github.com/deepseek-ai/deepseek-harness`, targeting quality
above 9.5/10. Local fixes and verification are recorded below; that numerical
quality target has not been demonstrated. Real-model quality and
cost-per-completed-task remain unmeasured; live evaluation is still paused.

## Whole-harness review — 2026-10-04

Latest user request is a comprehensive, candid critique of the whole harness and
how to improve it. Completed a review of the current dirty tree without runtime
edits, live calls, ledger resets, commits or pushes. Preserve earlier TUI/runtime
changes and failed eval evidence.

Read [the whole-harness audit](docs/HARNESS_AUDIT_2026_10_04.md) before selecting
the next fix. Source and captured results are in
`docs/evals/harness_audit_2026_10_04/`. These are 14 offline diagnostic groups, not
14 passing correctness regressions or a numeric quality certification. Capture
used fake SDK providers, temporary files/SQLite/KALASH_HOME and the supported hook
seam. An approved external temporary pytest capture wrapper passed once in 1.21s;
it checked capture completion. The prior full 731-test suite was not rerun for
this review. Audit probe F/I/UP and formatting checks pass.

New reproduced findings remain **unfixed**:

- In-memory session grants return before explicit deny evaluation.
- A pre-edit hook can change a digest-validated file, then WriteTool overwrites
  that intervening change; multi-edit validates uniqueness against the original
  while applying sequentially, and can return success for ambiguous replacements.
- Anthropic cached usage normalizes to negative input/total tokens; complete and
  stream accounting have different semantics.
- A failed stream's visible text disappears from runtime history/final response;
  missing usage is reported as zero instead of unknown.
- A primary stream that fails before its first event does not reach configured
  fallback in the reproduced gateway path.
- Late OpenAI-shaped ID/name chunks produce empty tool identities without error;
  two Google same-name calls produce duplicate IDs; Anthropic signature deltas
  are discarded.
- Environment `network=false` becomes a truthy string.
- Resume from another cwd uses that cwd, resets active spend from 110 to zero in
  the fixture, and does not restore saved compaction projection. Full old history
  still exists; do not claim all compacted facts are deleted.
- SDK streaming buffers until completion, and a per-call model label does not
  change the actual underlying provider in the fixture.
- Scheduler manager/schema use `next_run_at`; daemon queries `next_fire_at` and
  raises on the fresh schema. Manual trigger creates a running row/session ID
  without creating a session or starting an agent.

Important inspection findings include project ability to relax a user budget cap,
custom credential encryption derived from nonsecret machine ID/home path without
integrity, macOS sandbox wrapper signature mismatch, protected-file differences
between FS tools and shell, observation limits after buffering, model stop being
treated as success, weak memory provenance, extension authority/schema gaps and
public CLI accounting/event omissions. See the audit for severity, limitations
and acceptance cases. No physical macOS, production MCP, long physical-terminal
or new real-model reliability claims were established.

Recommended order: authority/data preservation -> provider/usage contracts ->
durable attempt/effect state and truthful completion -> connected SDK/CLI/scheduler
paths -> bounded context/tools/TUI -> user-authorized matched quality/cost trials.
Convert each reproduced defect into a desired-behavior regression when fixing it.
No overall 9.5/10 or comparative performance rating is supported by this review.

## Approval-modal crash follow-up — 2026-10-04

The prior 724-test pass missed an actual interaction boundary. Reproduced the
reported traceback: Textual app queries resolve against the active screen, so an
approval dialog hides the background transcript from deferred scroll callbacks
and new message mounting. Also reproduced a callback after transcript removal.

Implemented in `tui/app.py` and `tui/events.py`:

- Retain the mounted transcript reference and check attachment before delayed
  following. Clear the pending flag if callback scheduling is rejected.
- Post and update conversation status through the conversation's own screen,
  including when approval dialogs or the command palette are active.
- Let modal screens handle Escape, Tab and navigation bindings. The regression
  exposed a second crash where the app's priority Escape handler intercepted
  permission denial. Busy-state changes now also reach the background composer.
- Match tool output, completion and denial to explicit call IDs. A reproduced
  late output event from a completed call previously contaminated a different
  running tool's row. Clear settled tool timing state as well.

Regressions cover streaming behind approval dialogs, allow/deny keys, background
busy state, command-palette dismissal, detached callbacks and late tool events.
An end-to-end test uses the actual agent, file tool, permission policy and UI:
the write waits for approval, allowing creates the file, Escape denies it, and
both paths deliver the correct result to the next scripted provider turn.

Verification:

- Focused streaming/widget/TUI suite: **97 passed in 17.05s**.
- Full suite: **731 passed in 36.54s**, using the approved external test execution
  required by nested OS-sandbox integration tests.
- Strict mypy: **163 source files passed**. F/I/UP checks across `src tests evals`
  passed; all configured lint rules passed for the streaming regression file.
  Formatting: **248 files passed**. `git diff --check` passed.

See [DeepSeek comparison and release criteria](docs/HARNESS_COMPARISON_2026_10_04.md).
The upstream review is of mutable documentation, not a pinned source audit or
matched benchmark. No paid model calls, ledger resets, commits or pushes occurred.
The full-suite pass establishes local regression coverage, not a 9.5/10 harness
score. Provider identity handling, interrupted-response durability and complete
recovery workflows remain the next runtime priorities. The earlier failed
real-model results are preserved and still govern reliability claims.

## TUI and user-controlled spending — 2026-10-04

Preserved the pre-existing dirty tree, failed eval artifacts and ledger. No live
provider requests, commits or pushes were made.

Reproduced these UI defects locally before fixing them: tool spinner ticks erased
the output body, chunk boundaries could merge separate shell lines, the trailing
assistant chunk could remain unpainted during a provider pause, tool-separated
assistant responses appeared together above the tools, posting new widgets pulled
readers away from history, and stream errors replaced already received text.

Implemented in `tui/messages.py`, `events.py`, `app.py`, `transcript.py`, formatting
and theme:

- Coalesced 50 ms repaint timers flush trailing chunks and stop on completion or
  unmount. Incoming assistant text remains literal until each response boundary,
  where Markdown is rendered once; this avoids repeated incomplete-Markdown reflow.
  This is not incremental rich Markdown block rendering.
- Assistant responses and tool rows now follow execution order. Empty tool-only
  placeholders are hidden. Error messages preserve partial text, and final cleanup
  settles outstanding tool rows and timers.
- Tool progress retains output, fragmented lines assemble correctly, and compact
  gutter output replaces boxed panels. Tool failures show error/remediation text.
- Layout-aware following respects scrolling into history; End resumes following.
  The input occupies fewer rows and live layout transitions were removed.

Spending policy changes in `core/budget.py`, `core/config.py`, `runtime/request.py`
and `runtime/delegation.py`:

- Token/cost defaults are now `None` (uncapped). Usage and child accounting remain
  active. Existing explicitly configured limits and evaluation ledgers are kept.
- Requests retain the selected output allowance. An explicit cap that cannot fit
  the estimated input plus that allowance refuses before the provider call with
  the dimension, reservation, remaining amount and configuration remedy. It does
  not silently shrink generation. Provider/context limits still apply.
- Operational defaults remain: 50 iterations per send, 100 model requests,
  500 attempted tools, 3600 seconds of session execution, spawn depth 3. Nullable
  spending limits do not disable permissions, sandboxing, safe writes or these
  guards. Estimates are not exact billing caps.
- Workspace facts moved from the system prompt to the request tail in
  `runtime/context.py`; changing git status preserves the stable instruction and
  history prefix. This establishes cache eligibility, not measured savings or
  cheapest-model routing.

README documents opt-in settings, removing old ceilings with JSON `null`, restart
requirements and the streaming behavior. `/cost` renders absent caps as `no cap`.

Verification:

- Focused existing TUI/runtime/contract checks: **135 passed in 23.40s**.
- New streaming/spending regressions including narrow/wide scrolling: **18 passed
  in 2.15s**.
- Full suite: **724 passed in 31.79s**. The outer workspace sandbox blocked the
  first integration run's nested OS sandbox; the approved external rerun passed.
  An earlier restricted run was interrupted after encountering the same issue.
- Strict mypy: **163 source files passed**. F/I/UP across `src tests evals` passed;
  all configured Ruff rules passed for both new regression files and the new
  transcript module. Formatting: **248 files passed**. `git diff --check` passed.
  Existing full-rule repository lint debt remains; this is not a clean full lint.
- Scripted wide/narrow streaming/completion screenshots were exported from Textual
  and visually inspected. See [preview evidence](docs/evals/tui_2026_10_03/README.md).
  The sample cache patch/test results are synthetic UI content, not a live build.

Remaining limitations: physical-terminal behavior has not been observed on the
user's terminal; Markdown formatting settles at response boundaries; real-model
reliability and savings still require an authorized measured trial. Earlier
provider/recovery/security priorities and failed evaluation outcomes below remain
applicable. Spending opt-in changes do not authorize restarting old eval budgets.

Read AGENTS.md, inspect `git status --short` and recent commits, then read:

- [Build judgment](docs/BUILD_JUDGE.md): actual Codex artifact review and failure evidence.
- [Compact results](docs/COMPACT_EVAL_RESULTS.md): all failed repair attempts and accounting.
- [Evaluation mechanics](docs/EVALS.md): budgets, grading and known limitations.
- [Design](docs/HARNESS_DESIGN.md), [review](docs/HARNESS_REVIEW.md) and
  `coding-agent-harness-builder/SKILL.md`; load only relevant references.

The earlier handoff is preserved in
[the historical archive](docs/session_history/HANDOFF_BEFORE_RELIABILITY_PLAN_2026_10_03.md).
It records completed work; its older baseline, priorities and spending instructions
are historical. This file and the user's latest instructions govern continuation.

## Honest baseline

| Evaluation | Actual outcome |
| --- | --- |
| Four fresh repair tasks, scripted reference control | 4/4; zero API tokens; validates mechanics only |
| Sarvam-105B high reasoning, 4096 output cap | 0/4; no source patches |
| Same tasks, 16384 output cap | 0/4; no source patches |
| Decimal task after digest visibility fix | 0/1; output truncated; no patch |
| Build from scratch, low reasoning, 8192 output cap | No files or tool calls; Codex score 0/100 |
| Build from scratch, reasoning off, 6144 output cap | One triage.py write; 9/10 checks; no README/tests; Codex score 53/100, failed |

Observed failures:

- Reasoning/output requests repeatedly reached the generation cap before a
  complete response or usable mutation. Low reasoning also truncated on the build.
- Models repeatedly explored tiny workspaces rather than finishing repairs.
- The eval's byte-based reservation refused further requests despite unused
  reported-token allowance. The partial build's missing follow-up work was
  constrained by this controller; it is not proof the model could never finish.
- A real harness defect hid the source digest needed for safe writes. That is
  fixed and regression-tested; improved live repair quality is still unproved.
- The generated CLI silently treats malformed JSON/read errors as an empty store
  and can overwrite existing corrupt data on add. Independent reproduction proves
  this. This is a **candidate implementation defect**, not evidence that Kalash's
  own storage does the same thing. Do not repair the judged artifact and relabel
  its original run as successful.
- Graders initially gave a vacuous negative-check pass when the executable was
  absent, and overrestricted an unspecified disk schema. Current checks fix both;
  original frozen revisions are preserved. The partial CLI still fails on
  malformed JSON, independently of those corrections.

These are local diagnostic trials, not a calibrated judge, SWE-bench result,
statistical comparison or evidence of superiority over DeepSeek Harness.

## Start with these priorities

1. **Reproduce provider/tool-call failure without paid calls.** Inspect
   `models/providers/sarvam.py`, `openai_compatible.py`, gateway/normalization and
   runtime model-request/loop code. Add representative wire-stream tests for
   truncated reasoning, partial tool arguments, usage tails and cancellation.
   Ensure incomplete arguments never execute, errors identify the actual stop
   reason, and all observed usage is charged. Distinguish excessive generation
   from parsing/serialization defects; do not assume either without evidence.

2. **Fix budget diagnostics and unnecessary request/context growth.** Inspect
   `evals/compact_budget.py`, build/compact runners, ordinary run budgets and
   prompt/tool serialization. Identify which limit refused each request: suite,
   task, output reservation, time or turns. The current UTF-8-byte reservation
   plus 4096 framing allowance is a conservative estimate, not an exact server
   tokenizer. Improve estimation/retention using demonstrated behavior; preserve
   durable accounting of failed, missing-usage and cancelled calls. Never reset
   ledgers or bypass ceilings to make the eval pass. Preserve model constraints
   and the user's explicitly chosen settings; make policy changes visible.

3. **Prove a complete read -> write -> check workflow.** Preserve the visible
   file digest and stale-write protections. Exercise real tool schemas,
   observations, denied writes and actionable recovery, not only precomputed
   scripted arguments. Completion must be supported by the required files and
   fresh verification after the final edit. Own tests or a final-answer claim
   alone are insufficient. Keep partial artifacts and termination causes separate.

4. **Close remaining recovery/security gaps.** Preserve the already tested
   SQLite ownership and effect-boundary recovery work. Unknown mutation outcomes
   must not replay automatically. Add focused cases for interrupted provider
   streams/processes, retained file state/evidence across resume/compaction and
   secrets at actual context/event/artifact boundaries. Permission checks and
   OS sandboxing remain enforced. Do not claim exactly-once effects, universal
   redaction or adversarial isolation from the existing tests.

5. **Make the evaluation trustworthy before another live trial.** Use the same
   pinned interpreter/environment for builder and grader (the last grader used
   system Python 3.14.7 while the harness used 3.12.12). Validate positive and
   broken controls, mandatory deliverables and non-destructive failure paths.
   Freeze task/rubric/checks before generation and preserve changes transparently.
   Codex judge scores need artifact evidence; passing deterministic checks and
   complete required files remain mandatory. If the user requests live evaluation,
   agree its scope/budget from their instructions and run one small diagnostic
   first, then matched trials only after the demonstrated failure is fixed.

For each change: identify the evidence, add a meaningful regression, implement
in the relevant module, run focused checks, and then the required broader suite.
Report whether the fix proves a runtime contract or real-model performance.
Avoid adding orchestration layers, skills or features unrelated to these failures.

## Local reliability continuation — 2026-10-03

The existing dirty tree and failed eval evidence were preserved. No live requests,
ledger resets, commits or pushes were made. HEAD remains `dfdf903`.

Local reproduction uses the installed OpenAI SDK and `httpx.MockTransport` with
representative Sarvam SSE payloads; these are synthetic wire fixtures, not newly
observed service responses. Before fixes, ten initial cases failed: complete JSON
tool calls could mutate files after EOF, `length`, content filtering or an unknown
finish reason; unfinished text was marked complete; usage on a choice chunk was
ignored. Invalid JSON was already rejected by execution, but truncated responses
could still spend repeated tool attempts and requests.

Implemented contracts:

- The compatible adapter reads usage on any chunk and explicitly errors on EOF
  without a recognized finish reason. The runtime rejects unknown, capped,
  filtered and unclosed responses before dispatch, including parseable tool JSON.
  A capped reasoning-only response reports the generation limit; diagnostics no
  longer relabel it as a rejected output parameter or change requested settings.
- UsageUpdate counters are cumulative high-water marks. Gateway usage is charged
  once per stream, including failure/cancellation; runtime and eval accounting
  preserve input/cache counts across output-only updates and repeated reports.
  Earlier code double-counted snapshots in the gateway and erased earlier input
  in the runtime. Local regressions demonstrated both defects before fixing them.
- Failed/cancelled eval streams retain their reservation even after reporting
  partial usage. Observed usage remains recorded and incomplete cost stays unknown.
  Normally exhausted streams with recognized terminal reasons can settle usage;
  transport completion does not establish artifact or task completion.
- Reservation refusals record suite/task/overrun dimensions plus the wire,
  framing/output and remaining-token breakdown in a separate durable `refusals`
  list. They debit no attempt, survive sequential ledger continuation and stop
  as budget exhaustion without provider retries. The old generic refusal and
  error termination were reproduced before the fix. Limits and estimates are
  unchanged; historical refusals cannot be retroactively classified exactly
  without their missing request breakdowns.

Regression coverage is in `tests/unit/test_provider_interruptions.py` and
`tests/unit/test_budget_diagnostics.py`. It includes actual SDK stream closure on
connection failure/cancellation, no mutation replay, reasoning truncation, usage
tails and a positive fragmented-tool control with the next request's tool result.
These checks establish runtime contracts, not improved real-model repair quality.

Latest verification for this continuation:

- Focused provider/runtime/eval/diagnostic tests: **101 passed in 12.56s**.
- Full `.venv/bin/python -m pytest -q`: **703 passed in 29.76s**.
- Strict `.venv/bin/mypy src`: **162 source files passed**.
- The two new regression files, eval accounting/runners, normalized usage,
  stream handler and request module pass all configured Ruff rules.
- F/I/UP checks across `src tests evals` pass; formatting passes for 245 files;
  `git diff --check` passes. Full configured Ruff across `src tests evals` still
  fails with **712 existing findings** (672 within `src tests`). This is not full
  repository lint success.

Remaining priorities: reduce demonstrated unnecessary context/request growth
without weakening ceilings; cover provider tool identity/name fragments and
broader interrupted/resumed processes; extend the full read -> denied/stale write
-> recovered write -> independent fresh check workflow; align build/grader
interpreters. Completion verification, security and real-model reliability remain
unproved beyond the documented tests. All prior live results and cumulative
allowance totals below remain unchanged.
Unreported usage and provider-specific cached-input normalization still need
additional wire coverage; ordinary runtime estimates are not an exact billing cap.

## Work already implemented — preserve it

- Runtime/storage/memory refactor, hierarchical AGENTS.md/KALASH.md loading,
  isolated child context, charged child/compaction usage, bounded observations,
  error retention, tool-exchange closure and resume repair without effect replay.
- Owned SQLite runtime connections, serialized writers, cancellation rollback,
  atomic migrations and crash tests around mutation/receipt boundaries.
- Privacy scrubbing and retained runtime verification receipts. Known gaps remain
  for opaque/image/split content, raw logs, credential files and trusted extensions.
- Twelve packaged workflow skills and bounded references: filesystem, shell, code,
  Git, web, PDF, documents, spreadsheets, presentations, images, archive and data.
  Metadata-first discovery and lazy plugin/MCP schema activation survive compaction
  and resume. See docs/CAPABILITIES.md for actual format/dependency limits.
- Optional artifact inspection CLI; converter/vision/OCR/formula-calculation limits
  remain explicit. Do not equate a skill description with full native support.
- Sarvam V1/V2 adapter: default auto selects high for V1/max for V2; invalid effort
  combinations fail locally. Explicit V1 None disables reasoning. The saved key
  works for Sarvam-105B V1; GLM-5.3 V2 returned the per-key beta-access error.
- Read observations now include source path and SHA-256. Tool completion events
  retain zero-valued evidence and error codes/remediation.
- Fresh repair fixtures/runners, durable conservative token ledger, frozen build
  task/rubric, ten independent acceptance checks, Codex packet/finalization with
  evidence IDs and computed weights. Failed finalization exits 1.

## Working tree and verification

Last observed HEAD: **dfdf903** (`Updated readme`), following **91cbbe5**
(`Implemented:`) and **d79d09b** (runtime/memory refactor). The user made commits
concurrently. Some earlier changes are already committed; current work remains
modified/untracked. Always inspect the tree rather than resetting to this baseline.

Modified files include NEXT_SESSION.md, AGENTS.md, README.md,
`evals/compact.py`, `compact_budget.py`, `models/providers/sarvam.py`,
`runtime/toolhost.py` and `tests/unit/test_compact_evals.py`.
New files include build/judge/check runners, build task/rubric,
`tests/unit/test_build_judge.py`, eval documentation, saved evidence and the
historical handoff archive. Preserve them. No commit or push was made by this agent.

Earlier session checks, before the local continuation above:

- Full pytest: **681 passed in 30.03s**; focused eval tests: **29 passed**.
- Strict mypy: **162 source files passed** before the eval-only additions.
- New eval modules/tests pass the full configured Ruff rules; F/I/UP and
  formatting pass. Latest formatting check: 243 files. Whitespace check passes.
- Whole-repository configured Ruff has **713 existing findings**; do not describe
  selected lint rules as full lint success or disable rules broadly.
- The failed judge finalization CLI returned exit 1 as intended.
- Known-key scans were clean across the saved build artifacts; this is not a
  comprehensive secret-leak proof. No runtime tests were rerun for this docs-only
  handoff update.

SQLite/process/OS-sandbox checks may require execution outside the restricted
workspace sandbox. Use normal tool escalation if that restriction recurs.
Keep credentials in local config/environment and never print or commit them.

## Token ledger and durable evidence

All prior access probes, repair trials and both builds shared one allowance:

- Cumulative Sarvam reported usage: **203324 tokens**.
- Conservative charged usage: **219708 of 240000**; 20292 remains under that cap.
- Two HTTP 400 probes without usage each retain 8192 conservative tokens.
- Estimated cumulative reported-call cost: **INR 8.047945**, not an invoice.
- Interactive Codex judging usage/cost is unavailable to the runner, not zero.

The final live ledger is
`/tmp/kalash-build-judge-off-20261003/ledger.json`. Temporary traces/workspaces may
be removed by the OS. Durable JSON evidence, including the final ledger and
complete generated source, is in:

- `docs/evals/compact_2026_10_03.json`
- `docs/evals/build_triage_2026_10_03.json`

If the user explicitly resumes this shared allowance, carry the final ledger
forward sequentially, including unknown charges. Do not sum cumulative phase
snapshots, branch concurrent copied ledgers, silently restart an allowance or
claim an exact billing guarantee. A new separate budget requires clear scope
from the user. Live calls are currently paused.

## Suggested next-session prompt

> Read AGENTS.md and NEXT_SESSION.md, inspect the working tree, and continue
> making Kalash reliable. Start by reproducing provider truncation/tool-call
> failures and budget refusals locally. Preserve failed eval evidence and safe
> writes. Fix one demonstrated cause at a time with meaningful regressions.
> Keep live benchmarks paused until I request them, and report tested behavior
> separately from performance claims.

## Session 1 final verification and next-session append — 2026-10-05

First batch (categories **1–5**) is implemented with explicit acceptance limits;
read [the session report](docs/HARNESS_SESSION_1_2026_10_05.md). This is the latest
verification, superseding the earlier numerical baselines for the current tree:

- Full `.venv/bin/python -m pytest -q`: **770 passed in 41.96s**, one warning.
  The warning comes from a pinned offline Anthropic fixture using the adapter's
  old default model name. It is not a live service response. Check current model
  availability/defaults before any future authorized service trial; preserve
  explicit user model choices.
- Strict `.venv/bin/mypy src`: **166 source files passed**.
- F/I/UP checks across `src tests evals` passed. Ruff formatting: **254 files
  passed**. `git diff --check` passed. The three new runtime/state modules also
  pass all configured Ruff rules.
- **Full-rule repository Ruff still fails: 774 findings**, including current
  changes and private fault-injection tests. Do not call all of this historical
  debt, or describe selected lint success as a clean full lint. Triage it in the
  release-engineering batch rather than disabling rules to manufacture success.
- Final offline source distribution and wheel built successfully in
  `/tmp/kalash-session-one-build`. Checked packaged journal, completion, secure IO
  and gateway bytes against source and confirmed the cryptography dependency in
  wheel metadata. The restricted sandbox blocked uv cache access; the build used
  approved external execution. Ordinary final test execution succeeded locally.

The expanded tests include provider wire fixtures, uncertain retry accounting,
Google binary signature storage, actual early SDK delivery and cancellation,
real queued scheduler sessions, lease contention, loss of committed streamed
progress, once-grant contention, denied/stale writes, missing verification
deliverables and blocking the second mutation after a receipt failure. No paid
calls, commits, pushes, probe-result rewrites or ledger resets occurred.

**Historical session 1 boundary: 6–10 next.** Session 2 is now recorded below;
its next-session scope supersedes this paragraph. Cost per verified result, context relevance and
compaction, memory provenance/authority, bounded resource handling and delegation
isolation remain the next batch. Then **11–14** covers sustained TUI behavior,
complete CLI/SDK/event output, extension trust/capability validation, and measured
release/evaluation engineering. Use the top section's three-session plan; the
older suggested prompt immediately above is historical.

No 9.5/10 or DeepSeek-superiority claim is supported. Local contract tests do not
replace matched real-model trials, which remain paused.

## Session 2 final verification and next-session append — 2026-10-05

Categories **6–10** received local runtime fixes and regressions; read
[the report](docs/HARNESS_SESSION_2_2026_10_05.md) for the precise contracts and
remaining limits. New tests are in `tests/unit/test_harness_session_two.py`.

- Free observation reduction precedes semantic compaction. Repeated retained
  anchors and oversized summarizer requests do not buy another summary. Notes
  refresh within a turn; optional saved context shares one aggregate allowance.
  Context refusals preserve the chosen output allowance and report the breakdown.
- Unicode/media/signature estimates include more of the actual payload. They
  remain heuristics, not an exact tokenizer or billing bound. The saved offline
  fixture reduces estimated input from **62,500 to 11,661**, with one scripted
  response and no semantic-summary dispatch; it is **stopped_unverified**, not a
  real-model success or measured cost per verified result.
- Agent-written memory is low-trust inference with a source call ID. Common
  quoted/fenced/negated preference cases are excluded from automatic extraction.
  Recall carries provenance/scope/time; memory remains data, not permissions.
- Read/search/fetch/scratchpad processing now has acquisition/aggregate bounds,
  process cleanup and explicit errors. Scratchpad `grep` is now literal text.
  Damaged indexes are preserved. Fetch rejects compression and changed-origin
  redirects; it does not establish DNS/private-address confinement.
- Migration **5** adds transactional journal accounting and admission bounds:
  8 MiB per record, 128 MiB or 100,000 records per session. Existing evidence is
  not deleted. Recovery uses keyset pages and selects the active projection before
  loading messages; old interrupted text is not resurrected after compaction.
- Shared-workspace delegation is read-only and skips hooks/extensions. Durable
  parents retain child sessions/IDs, partial outcomes and usage. Writing agents
  require a caller-owned separate workspace; automatic worktrees/merges and
  parallel-team performance remain unproved.

Verification: full `.venv/bin/python -m pytest -q` **800 passed in 50.06s**, one
existing offline Anthropic deprecation warning. Focused checks **113 passed in
1.33s**; the strengthened output-allowance fixture passed afterward in **0.35s**.
Strict mypy: **166 source files passed**. F/I/UP passed; Ruff formatting **255
files passed**; whitespace passed. The new regression file and journal module
pass full configured Ruff. Full-rule repository Ruff still reports **780
findings**, including current work; do not describe it as all historical debt.
Final new-file verification, including the strengthened fixture: **30 passed in
1.11s**. HEAD remains `dfdf903`.

The first broad attempt passed 796 tests and failed two old test expectations.
The missing-blob stub was updated for its bounded-read argument; corrupt-index
behavior now requires refusal and byte preservation. The long-run test measures
actual estimated context occupancy rather than an arbitrary message count, while
retaining its constraint and tool-pair checks. Historical probes, real-model
artifacts and grader revisions remain untouched. No paid calls, ledger resets,
commits or pushes occurred.

**Next session: 11–14 only.** Sustained TUI/terminal behavior; complete truthful
CLI/SDK/event output; extension/capability trust; release/evaluation discipline
and full-rule lint. Real-model reliability remains unproved and live evaluations
remain paused until new user instructions define scope and spending authority.
