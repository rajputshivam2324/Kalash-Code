# Sarvam reliability continuation — 2026-10-05

The user resumed real-output testing and explicitly selected a **new separate
60,000 conservative-token allowance for one end-to-end trial**. The earlier
240,000-token allowance and its 219,708 charges remain untouched. The dirty tree
and HEAD `dfdf903` were preserved; no commits or pushes occurred.

This continuation prioritized the live-test request and CLI/evaluation
truthfulness. It does **not** complete categories 11–14 of the three-session plan.

## Actual live outcome

| Measurement | Result |
| --- | --- |
| Task | Frozen `compact-v1` TTL/LRU repair |
| Model | Sarvam-105B, V1, reasoning explicitly off |
| Generation allowance | 6,144 output tokens per request |
| Retries | SDK disabled; one gateway attempt per request |
| Other guards | 52,000 task tokens; 180 seconds; 12 iterations; 40 tools |
| Broken baseline / scripted reference | Failed / resolved, zero API tokens |
| Live independent grading | **Failed, 0/1 verified repairs** |
| Provider requests / tool attempts | 11 / 11 |
| Source changes | None; overwrite rejected for missing digest |
| Termination | `budget_exhausted`, task reservation refusal |
| Duration | 7.708 seconds |
| Reported and conservatively charged tokens | **29,953 / 29,953** |
| Breakdown | 29,064 input; 889 output; 17,216 cached input; zero reasoning |
| New allowance remaining | **30,047**, no further trial dispatched |

Cached input is included in input, not added again. Every dispatched stream
settled with reported usage. The refused twelfth request charged no attempt.
The runner's ₹0.601016 estimate uses rates last checked on 2026-10-03; this is
not refreshed pricing or an invoice.

The model initially passed glob-style `pattern` arguments to `search`, whose
schema requires `query`. It then listed/read the source and repeatedly searched
for absent tests. Dedicated tools refused access outside the workspace. Shell
find commands were OS-sandboxed and returned only workspace data. The eventual
write omitted the digest despite the read observation contract and tool schema.
The refusal preserved the original source and explained the required digest.

The next request needed **25,103** conservative tokens: 14,863 serialized bytes,
4,096 framing tokens and 6,144 output tokens. Only **22,047** remained under the
task ceiling, although **30,047** remained in the suite allowance. This is a
controller refusal, not a provider outage or evidence that all 60,000 tokens
were consumed. No ceilings were relaxed to manufacture a pass.

Offline diagnosis copied the **rejected proposal** into a separate workspace and
ran the same independent checks. It also failed: assigning an existing key in a
Python dict does not refresh its insertion order, so successful gets did not
implement true LRU. This is a model implementation defect. The diagnostic did
not mutate or relabel the original live artifact.

This configuration avoided the earlier output-truncation failure and produced
usable tool calls. It still failed task execution. One selected task with a
different reasoning configuration is not a matched improvement benchmark,
cost-per-success result, reliability certification or competitive comparison.

## Local changes after diagnosis

- `evals.compact` accepts an explicit task and reasoning option. A configured
  credential and successful sandbox preflight permit a **metered task request**
  without a separate paid access probe. Optional supplied probe charges and
  sequential prior ledgers still count; existing output directories are refused.
- Future trials save redacted normalized request projections and stream-event
  metadata in `provider.jsonl`. Block-delta bodies are omitted and their lengths
  retained, preventing lexical credential fragments from being logged across
  records. These are not exact HTTP wire dumps. The original live run preceded
  this addition and has no provider trace; no fixture fills that evidence gap.
- CLI JSON usage now separates observed input, output, cache and reasoning
  counters for the current invocation. It reports actual session budget ceilings
  and usage instead of hardcoded caps. Unknown usage remains explicit, and cost
  stays null when rates are unavailable or usage is unknown.
- Structured failures retain accounting, completion status and budget evidence.
  A budget refusal with an error message now exits **4**, rather than generic
  failure **1**. Cancellation termination also precedes generic error handling.
- NDJSON carries tool start/denial/completion identity and completion error
  evidence. Output chunks retain their source-bounded bodies instead of silently
  losing everything after 500 characters. Zero-valued evidence survives.
- Text output prints a final reply when no deltas were streamed.

Regressions first demonstrated aggregate-input misreporting, lost long output
and the wrong budget exit code. A trace change initially broke an older
string-yielding cancellation fixture; tracing is now conditional, and that
fixture passes unchanged. No historical grader or task was modified.

## Verification and remaining work

- Full `.venv/bin/python -m pytest -q`: **804 passed in 50.87s**, one existing
  offline Anthropic deprecation warning.
- After strengthening trace protection for split credential fragments, focused
  CLI/eval/accounting checks: **34 passed in 1.92s**. No second live trial occurred.
- Strict mypy: **166 source files passed**.
- All configured Ruff rules pass for the six touched Python files. Repository
  formatting: **255 files passed**; whitespace passed.
- Full-rule Ruff still fails with **760 findings**, including existing dirty
  work. It is not a release-ready lint gate. No rules were broadly disabled.

Remaining categories 11–14 include sustained physical-terminal/TUI evidence,
complete shared CLI/SDK event identity and fallback projections, MCP schema and
server-boundary validation, release-matrix checks and the remaining lint debt.
This session does not qualify those surfaces. Provider-level request diagnostics
and model recovery after argument/digest refusals need a subsequently authorized
live scope; unused tokens do not automatically authorize another task.

[Durable evidence](evals/sarvam_reliability_2026_10_05.json) includes manifests,
ledgers, events, source, grader scripts, empty live patch, complete rejected
proposal and separate diagnostic result. Temporary originals remain under
`/tmp/kalash-reliability-live-20261005`,
`/tmp/kalash-reliability-control-20261005`, and
`/tmp/kalash-rejected-candidate-20261005`. Known-key scanning refused evidence
export if the configured Sarvam key appeared; this is not universal DLP proof.

Harness Python remains 3.12 and the independent grader uses system Python 3.14;
their environment equivalence is still unqualified. The fixture uses stdlib only.
No full source snapshot was frozen for the live runtime; recorded source/diff
hashes identify the run but cannot reconstruct its dirty tree alone.
