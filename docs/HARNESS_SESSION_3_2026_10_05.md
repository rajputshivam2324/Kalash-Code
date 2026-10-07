# Harness session 3: local hardening and real Sarvam outcomes

Work began October 5 and continued after interruptions on October 7. Categories
11–14 now have local implementation and connected regressions. This does **not**
establish reliable real-model task completion. The first repair matrix passed 2/4;
the build and its recovery remained broken.

## Real evaluation and spending

The user explicitly authorized a new separate allowance of 200,000 conservative
tokens. Sequential phases carried prior charges rather than resetting the ledger.
Sarvam-105B used reasoning off, temperature zero, SDK retries zero and one gateway
attempt per request. Repair tasks and build acceptance checks were frozen before
calls; hidden checks ran independently in fresh network-disabled OS sandboxes.

| Phase | New charged tokens | Cumulative tokens | Observed outcome |
|---|---:|---:|---|
| Four original repair tasks | 100,383 | 100,383 | 2/4 passed: decimal-ledger, streaming-jsonl |
| Incident-triage build | 29,595 | 129,978 | 1/10 acceptance checks; candidate tests: 10 failures, 1 error |
| Recovery of copied partial build | 18,204 | 148,182 | Same broken artifact; 1/10 checks |
| TTL retry, original tools | 25,516 | 173,698 | Failed TTL behavior |
| TTL retry, three tools | 10,974 | 184,672 | Harness duplicated tool identities; failed |
| TTL retry after identity fix | 3,098 | 187,770 | Normal identities, repeated reads, no patch; failed |

The separate allowance has 12,230 tokens left by the last observed total. Earlier
allowances remain 29,953/60,000 and 219,708/240,000. The last controller estimate was
INR 5.026278, using rates recorded October 3; this is an estimate, not a current
price quote or reconciled invoice. No paid calls occurred on October 7.

The first TTL attempt created an out-of-scope test file despite a source-only
task. DAG handling still failed duplicate-dependency/cycle checks. The build's
parser consumed the command as an extra positional, breaking its documented
interface; its store loader also silently accepted malformed data. A 70,000-token
build ceiling and a 52,000-token recovery ceiling were chosen before their phases.
Conservative next-request reservations stopped partial artifacts. These are
controller refusals plus artifact failures, not completed builds or provider outages.

The three-tool trial exposed a regression introduced during this session's lint
cleanup: changing `(id or name) and not new_tool` to `id or name and not new_tool`
emitted initial identity twice. A failing provider-wire regression reproduced it;
the corrected condition and regression pass. That trial is a harness failure,
not evidence that Sarvam itself repeated raw wire identities. The final live
retry confirmed ordinary read/shell identities, but did not complete a repair.

The build and recovery were independently reviewed by Codex, separate from the
Sarvam builder, and scored 23/100 under the frozen rubric. This single reviewer
was not calibrated against human labels. The deterministic result is clearer:
only help passed; all eleven candidate tests failed. Earlier progress text said
1/12; the actual frozen acceptance rubric contains **10** checks.

## Evidence retention and limits

The `/tmp` run directories did not survive the interruption/restart. This session
failed to copy each run into durable workspace storage when it completed.
[Recovered tool-result excerpts](evals/harness_session3_2026_10_05/recovery_index.json)
preserve observed summaries, failure details, identity events and local check
outputs from the current conversation. Some excerpts were already truncated;
the index identifies them. They are not recreated raw provider traces or full
original ledgers. Per-request usage and complete failed workspaces cannot be
reconstructed from the remaining evidence. The spending record is therefore
an observed summary, **not a resumable ledger**.

The generated `triage.py` was recovered from a complete tool-output excerpt.
Its SHA-256 matches the original artifact exactly. An October 7 offline recheck
against the unchanged, hash-matched acceptance file again passed **1/10** checks
with the OS sandbox enforced. The README/test files and full original judge packet
remain missing; the candidate test suite was not rerun in that recheck.

Before the loss, normalized request and stream metadata were captured. Lexical
delta bodies were omitted to avoid exposing credentials split across fragments;
no raw-wire completeness claim was justified even then. Source changed between
phases, and the tool subsets/output limits differed. These retries do not form a
matched comparison or show a causal quality improvement. Historical reports,
graders and user commits remain untouched.

Future compact/build CLI runs default to unique directories under ignored
`.kalash/eval_runs/`, independent of the caller's working directory. Explicit
`--output` still works; choose persistent storage. Run directories refuse reuse.

## 11: terminal and presentation

Completed widgets are bounded to 200 and resident transcript text to 2 MB, with
an anonymous private scrollback archive capped at 64 MB. Export includes archived
entries, tool summaries and completed subagent status. Archive exhaustion is
explicitly marked incomplete. Active widgets remain protected from eviction.
Tool-output presentation uses an ordered bounded queue; slow UI handlers cannot
stall execution. Overflow and drain deadlines produce visible incomplete-output
errors. Permission and required-hook gates remain synchronous enforcement paths.

A Linux PTY trial passed 240 transcript entries, 90 Unicode/Markdown stream lines,
resizing behind approval modals, actual allow/deny keys and complete export.
This is local Linux PTY evidence, not physical macOS/Windows, SSH or terminal-emulator
certification. The initial terminal capture was lost; the result excerpt was recovered.
The October 7 repeat also passed, and its result and terminal capture are now
durable in [the terminal evidence directory](evals/harness_session3_2026_10_05/terminal_2026_10_07/result.json).

## 12: public CLI and SDK

Events now carry task-local session/run identity, sequence and timestamp. Provider
events identify logical attempts, physical dispatches, fallback transitions and
settled input/output/cache/reasoning usage. CLI and SDK share the normalized event
projection. CLI transport sequence differs from runtime sequence because it also
emits text and lifecycle envelopes. Verification receipts and file-operation
metadata reach terminal results. Failed usage remains explicitly unknown.
Final review added regressions for failure envelopes losing run identity,
verification and completed file operations; both JSON formats now retain them.

SDK completion returns tool attempts, events, actual model, run ID, verification,
usage and truthful termination/completion status. Streaming preserves early text;
`include_events=True` adds public events. Bounded collectors/queues stop explicitly
on exhaustion and generator closure cancels/joins the producer. Observational
subscribers cannot authorize tools. See [CLI output](CLI_OUTPUT.md) and [SDK](SDK.md).
SDK accounting now reads gateway usage totals independently of its bounded event
collector; a collector-overflow regression prevents observed usage becoming zero.

## 13: extensions

MCP arguments are checked against full nested JSON Schema before remote dispatch,
including types, required fields, enums, arrays and additional-property rules.
Validation does not coerce strings to integers or discard allowed fields. Schema
and argument sizes/depth are bounded; external schema retrieval is disabled.
This does not isolate pathological schema/regex CPU work in a subprocess.

The local stateless MCP HTTP endpoint authenticates before reading the body,
bounds headers/bodies/connections, validates JSON-RPC envelopes and notifications,
and applies handler/request deadlines. A real local MCP peer exercises handshake,
discovery, schema rejection and cleanup. Protocol support is pinned to 2025-06-18;
this is not a claim of complete production MCP or current-draft support. OAuth
uses form encoding, HTTPS or loopback HTTP, bounded responses and no redirects.

Required pre-effect hooks validate their config, enforce deadlines and fail closed
on nonzero exit, untrusted/changed authorized files or runner initialization failure.
Missing previously authorized hook files in project hook directories also block
startup; `kalash hooks untrust PATH` explicitly revokes their authorization.
The registry rejects malformed/oversized data, preserves it for repair, and uses
private atomic writes and cooperating-process locks. Filesystem tools and existing
OS overlays protect the registry from rewrites when its home is inside a workspace.
Fresh-schema hook result storage now works. Post-effect hook errors are reported
without claiming that a completed write was undone. Trusted Python callbacks
have cooperative deadlines; blocking native/Python code is not forcibly isolated.
Never-authorized malformed optional files retain warning-and-skip behavior.
There is no external attestation of privileged deletion of the entire authorization
registry, or inventory of symlink aliases pointing outside project hook directories.
Do not claim those broader authority boundaries are solved.

## 14: release and evaluation discipline

CI now configures Linux Python 3.12/3.13/3.14, all configured Ruff rules, formatting,
strict mypy, the full suite, package build and a base-wheel smoke outside the source
tree. Remote CI has not run in this session. Python 3.13 has not been locally tested.
The October 5 final suites passed **824 tests on Python 3.12 and 3.14**; mypy passed
169 files. Python 3.14 initially stalled thread shutdown inside the outer Codex
sandbox; focused and full checks passed outside it with Kalash's own gates intact.
The 3.14 run reported dependency deprecation warnings, not ignored test failures.

Full lint debt was addressed with code fixes plus explicit line-level exceptions
for callback signatures, internal/fault-injection access, protocol labels,
synthetic credentials, reviewed parameterized SQL and operational output. This is
not a claim that every warning was eliminated without exceptions. Frozen
`evals/golden_kvstore` bytes were preserved and explicitly excluded; historical
embedded reference/prompt strings in `evals/project_build.py` retain a scoped E501
exception. No grader was weakened to make a model pass. The review also caught and
fixed unsafe mechanical changes to SQLite-row access and provider boolean precedence.

The October 7 final Python 3.12 suite passed **837 tests in 50.74 seconds**;
strict mypy passed 169 source files; all configured Ruff rules plus RUF100 passed;
258 Python files passed formatting; whitespace checks passed. The additional
October 7 regressions have not been rerun on Python 3.14.

The final wheel and source distribution build passed. A fresh base installation
outside the source tree passed SDK/import, local schema, CLI help and all **19**
bundled resource hash comparisons. Every packaged source file also matches the
current workspace bytes. See [final verification](evals/harness_session3_2026_10_05/verification_final_2026_10_07.json),
[the installed-wheel record](evals/harness_session3_2026_10_05/wheel_final_2026_10_07.json),
and [offline build recheck](evals/harness_session3_2026_10_05/recovered_build_acceptance_2026_10_07.json).

No Git commits or pushes were made.
The remaining reliability priority is real task completion and cost per verified
artifact under intact, matched trials—not a universal reliability or 9.5/10 claim.
