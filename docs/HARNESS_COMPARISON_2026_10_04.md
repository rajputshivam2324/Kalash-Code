# Kalash and DeepSeek Harness: evidence and release criteria

Assessment date: 2026-10-04. Comparison target confirmed by the user:
[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness).
Upstream `master` documentation was inspected on this date; it is mutable and
was not pinned or executed. This is a contract and evidence review, not a matched
benchmark, source audit, security certification, or numerical quality rating.

## Current judgment

Kalash has not demonstrated the reliability required for a 9.5/10 release claim.
Its prior Sarvam repair trials produced no verified repairs; the best recorded
build was incomplete and scored 53/100 on that artifact's rubric. Those results
measure a model/configuration/harness combination, not an isolated harness score.
The latest user-reported UI crash also shows that the earlier passing local suite
missed a real interaction boundary. See [repair evidence](COMPACT_EVAL_RESULTS.md)
and [build evidence](BUILD_JUDGE.md).

DeepSeek's README labels its project a developer preview. Its published safety
notice says it has not undergone a security audit and should not be treated as
production-ready. That prevents treating it as an automatic 10/10 reference.
[README](https://github.com/deepseek-ai/deepseek-harness),
[safety notice](https://github.com/deepseek-ai/deepseek-harness/blob/master/SAFETY.md).

## Relevant differences

| Area | DeepSeek's documented contract | Kalash evidence / remaining work |
| --- | --- | --- |
| Architecture | Cordis plugins can replace the loop, providers, tools and session services; multiple application profiles share services. | Python runtime modules with CLI/TUI/SDK, model adapters, hooks and MCP. More extensibility alone would not repair the demonstrated failures. |
| Requests and history | Session events support reconstruction of model-visible requests; failed attempts and successful messages have distinct records. | Durable SQLite turns and mutation receipts exist. Exact failed-request reconstruction and interrupted-stream persistence still need a focused audit. |
| Interruption | Cancellation preserves delivered text in an interrupted history entry; missing tool outcomes are classified without automatic replay. | Unknown tool effects receive conservative recovery results. Partial text now survives in the live TUI, but that does not prove the same text survives process loss/resume. |
| Scheduling | Parallel-safe tools overlap; exclusive operations form barriers. | Independent reads overlap and writes serialize. Local tests cover ordering and receipt failure; broader provider/process trials remain needed. |
| UI lifecycle | Upstream exposes live stream events separately from durable settlement. | The reported approval-modal crash and competing keybindings are locally reproduced and fixed. Background updates now target the mounted conversation, not whichever modal is active. |

Sources for the upstream contracts:
[architecture](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md),
[agent loop](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/agent-loop/README.md).
These are upstream claims, not behaviors independently verified in this session.

## Improvements made after the crash report

1. Keep an owned reference to the mounted transcript. Approval dialogs no longer
   hide it from delayed scroll callbacks or new transcript entries.
2. Ignore delayed follow callbacks after transcript removal/shutdown, and clear
   the pending flag if the message pump rejects callback scheduling.
3. Let modal bindings own Escape, arrows and Tab. Escape denies permission instead
   of invoking the conversation's picker handler and crashing.
4. Update conversation status and busy state through its screen while a modal is
   open. A completed run no longer leaves the composer saying it is streaming.
5. Correlate tool output/denial/completion by the supplied call ID. Late events
   from an ended or unknown call must not alter another running tool's row.

Tests reproduce the original traceback, cover modal allow/deny, command-palette
streaming, unmount callbacks, late tool events and an actual temporary file write
through the TUI worker and permission policy. A scripted provider supplies that
workflow; no paid API call or comparative solve-rate claim is involved.

## What a 9.5 target would need to mean

A proposed release rubric is 35 points for independently verified task completion,
20 for recovery, 20 for permission/sandbox/data-integrity behavior, 15 for measured
cost and latency, and 10 for interaction stability. Define scoring functions and
freeze cases before execution. A total above 95 must not compensate for a safety
failure, silent data loss, false completion or a reproducible blocking crash.
This rubric is a proposal, not a score already awarded.

Required evidence before claiming parity or superiority:

- Pin both harness revisions and use the same provider/model, reasoning/output
  settings, initial repositories, independent checks and explicitly agreed limits.
- Repeat representative repair and build tasks; report sample sizes, uncertainty,
  completed artifacts and costs per verified completion, including failed calls.
- Inject permission denials, stale writes, provider disconnects, cancellation,
  process loss and resume. Confirm no unauthorized or duplicated mutations.
- Exercise the real TUI with approval dialogs, long streams, terminal resizing,
  history navigation, cancellation and a subsequent successful turn.
- Classify model, provider, harness, grader and infrastructure failures separately.

The next runtime priorities remain tool identity/name fragmentation, interrupted
response durability, and complete read → denied/stale write → recovered write →
fresh independent verification. Live evaluation remains paused until the user
requests it with a budget/scope. The previous evaluation ledger is not reset.
