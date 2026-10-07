# Harness fixes: session 1 of 3

This session implements the first five categories from the whole-harness critique:
core contracts, truthful completion, durable state, authority boundaries, and
credential/privacy handling. The R/I identifiers below refer to the preserved
[October 4 audit](HARNESS_AUDIT_2026_10_04.md), not the user's 14-category numbering.
The initial dirty tree, historical probes, failed live runs and spending ledger
were preserved. No live model calls, commits, pushes or ledger resets occurred.

## Implemented contracts

| Category | Change | Evidence / qualification |
| --- | --- | --- |
| 1. Core contracts | Anthropic usage uses disjoint input/cache-write partitions. Google includes thoughts in normalized output and cached content in input. Separate reasoning prices replace the output rate for that partition; cache writes have an input-rate fallback. | Cached Anthropic fixture totals 1,310 tokens; complete/stream fixtures and cost partitions have regressions. Prices remain estimates, not an invoice. R4. |
| 1. Core contracts | OpenAI late ID/name fragments accumulate. Missing/duplicate tool identities stop dispatch. Google same-name calls get unique IDs and correct response names. Anthropic signatures and Google's binary signatures survive storage. | Actual installed OpenAI/Anthropic SDKs with mock HTTP streams, Google SDK objects, serialization and stream-handler tests. No service calls. R7–R8. |
| 1. Core contracts | Configured streaming fallback works before observable events, including adapter error events. Observable streams never replay. Budget refusals propagate without retry/fallback. | Connected fallback tests include actual selected pricing, retained unknown usage and refusal before another dispatch. R6. |
| 1. Core contracts | SDK streaming yields before generation ends; closing a consumer cancels and closes its producer. Per-call model options route the actual provider, tools restrict the host registry, and defaults restore afterward. | Connected SDK tests. A bounded slow-consumer queue stops explicitly instead of silently dropping output. R11. |
| 1. Core contracts | Scheduler uses the migrated schema; manual triggers create a real session and queue pending work for the daemon. Runs retain session IDs and truthful status. Lease acquisition is transactional; detected ownership loss cancels active work. | Fresh SQLite manager→daemon→agent test, competing lease test; inherited user limits cannot be raised by generated job overrides. R12. |
| 2. Stopping versus success | `stopped_unverified`, `verified_complete`, `partial`, `failed`, `cancelled` and `uncertain` are separate from termination reason. Ordinary model stops do not establish verified completion. | Missing deliverables produce partial status. Child/SDK/scheduler results expose the distinction; CLI emits completion status and fails on verification errors. I4. |
| 2. Stopping versus success | Caller-owned completion contracts specify required files and optional check commands. Checks use ordinary permissions/sandbox/tools; before/after workspace snapshots detect intervening changes. | Positive, missing-file and changed-artifact tests. Commands, exit codes and artifact freshness establish only the explicit contract, not general quality. |
| 3. Durable state | Request snapshots, dispatches, progress, settlements, tool intent/effect receipts, completion and accounting are journaled in owned SQLite transactions. Published streamed text is recorded before its callback. | Failed text is retained; hard-loss recovery restores committed visible progress. Unfinished attempts reserve unknown usage. R5, I5. |
| 3. Durable state | Transcript entries and their source receipts commit together. Recovery restores a settled response/effect that missed transcript insertion, once, without executing a tool. Persistence failure blocks dependent mutations. | Idempotent recovery and injected receipt-failure tests. This does not claim exactly-once external effects. |
| 3. Durable state | Resume selects the original workspace before constructing tools, restores cumulative reported/unknown usage, summary and saved compaction projection, and repairs unresolved tool exchanges conservatively. Every send refreshes under an OS session-writer lock. | Workspace/accounting/summary tests; projection-plus-new-turn test; competing session-owner test. R10. |
| 4. Authority | Explicit denials and protected targets precede grants. Approval signatures bind workspace, canonical targets, risk and exact arguments. Durable revocation is consulted again; once grants use compare-and-swap consumption. | Deny-after-approval, exact scope and competing-once-consumption regressions. Legacy unsigned/coarse allow grants cannot authorize new exact effects. R1. |
| 4. Authority | Settings reject malformed/non-object sections and invalid typed values. Environment booleans parse correctly. Project settings may tighten explicit user ceilings but cannot remove or relax them. | Environment `false`, project-cap and invalid-settings negative tests. R9. |
| 4. Authority | Write/edit/multi-edit validate after hooks and immediately before replacement, coordinate cooperating writers with a per-target lock, and use no-clobber creation. Multi-edit validates each replacement against the actual working copy. | Hook-induced stale read preserves the intervening change; introduced ambiguity leaves the original file intact. R2–R3. External-writer limitation below. |
| 4. Authority | Restricted Linux shell overlays hide existing protected secrets and protect Git hook directories. Read-only workspace mounts remain readable. macOS accepts cwd, writes private profiles, and adds protected rules. Generic process host allow-lists fail closed instead of granting all network access. | Real local Bubblewrap read/write/hook negative tests; macOS profile/argv code only, no physical macOS execution. I2. |
| 4. Authority | MCP names, descriptions and remote annotations cannot establish read-only authority; remote tools remain effectful until trusted enforcement exists. | Plan mode filters the remote catalog; discovery tests were updated to this stricter contract. Extension trust work continues in session 3. |
| 5. Credentials/privacy | AES-GCM uses a random separate 0600 key and binds ciphertext to provider ID. Files are private before writing, atomically replaced, and locked for updates. Existing legacy credentials migrate on an explicit save. Damaged storage is refused instead of replaced. | Authentication/tamper/provider-binding and malformed-storage preservation tests. I1. |
| 5. Credentials/privacy | Known environment/saved credential values and recognized formats are scrubbed at request, visible-stream, settlement, tool-event and configured-log boundaries. Streaming redaction carries unfinished tokens/PEM material across chunks. Credential input is masked in the TUI. | Split credential, oversized/unfinished private-key and formatted exception-log regressions. This is defense in depth, not universal DLP. I13. |

## Public completion example

```python
from kalash.runtime.completion import CompletionContract

response = await session.complete(
    "Implement the requested repair",
    completion_contract=CompletionContract(
        required_files=("app.py", "tests/test_app.py"),
        checks=("python -m pytest -q tests/test_app.py",),
    ),
)
print(response.completion_status, response.error, response.usage_unknown)
```

The user/caller owns the checks. A model-authored claim or an empty contract cannot
produce `verified_complete`. Python/check dependencies must exist in the sandbox.
Snapshot bounds are 10,000 ordinary files and 128 MiB. Git metadata, dependency
directories and generated caches are excluded; this is a source-workspace
freshness check, not attestation of the interpreter or dependency environment.

## Limits that remain explicit

- Conditional replacement has no universal filesystem compare-and-swap primitive.
  The lock coordinates Kalash writers. A non-cooperating external writer can still
  race the last digest check and rename. No lost-update guarantee is claimed for
  every possible editor/process/filesystem.
- Existing protected files/directories are OS-overlay targets. A generic process
  sandbox does not enforce arbitrary filename globs against every new file a
  command might create. Filesystem-tool checks cover those paths; broader shell
  policy needs an enforcing design, not shell-string parsing.
- macOS profile generation and wrapper compatibility were checked locally; real
  Seatbelt behavior remains unproved without a macOS host. Windows confinement was
  not established. Process host allow-lists need an enforcing proxy to support
  network use; current behavior denies that subprocess networking.
- Credentials are encrypted with a separate local file key, not an OS keychain.
  Reading both files as the same OS user permits decryption. Legacy enc1 values
  lacked authentication; migration cannot prove their historical integrity.
- Redaction recognizes known values and patterns. Opaque reasoning/signatures,
  image bytes, arbitrary encodings, plugin-owned logs and malicious installed
  Python extensions are not comprehensively isolated or inspected.
- Journal storage needs retention/size controls and sustained-load measurements.
  An interrupted mutation without a settled effect is uncertain; recovery never
  automatically retries it. Verification and task completion remain distinct from
  the uncertainty of provider billing.
- Scheduler interval/event/catch-up/overlap policies need broader connected
  qualification. Expired leases alone are not universal fencing for arbitrary
  external effects, and cancellation does not roll back effects already made.
- These regressions establish local runtime behavior. They do not demonstrate
  real-model repair/build quality, lower cost per completed task, superiority over
  DeepSeek Harness, or a 9.5/10 score. Live evaluation stays paused.

Google usage partition handling was checked against the official
[GenerateContent usage metadata reference](https://ai.google.dev/api/generate-content).

## Remaining sessions

Session 2 is **6–10**: cost optimization, context selection/compaction, memory
authority/provenance, bounded resource processing, and delegation/workspace
isolation. Session 3 is **11–14**: sustained TUI behavior, complete machine-readable
product surfaces, extension/capability trust, and release/evaluation engineering.
Keep the earlier failed live artifacts and accounting; do not start paid trials
without new user instructions defining scope and spending authority.

Final commands/results are recorded in [NEXT_SESSION.md](../NEXT_SESSION.md).

Final verification: **770 tests passed in 41.96s**, one offline SDK deprecation
warning; strict mypy passed for **166 source files**; selected F/I/UP lint,
**254-file** formatting and whitespace checks passed. The final wheel/source
archive built and packaged source bytes were checked. Full configured Ruff still
reports **774 findings**, including current work; that is remaining release debt.
