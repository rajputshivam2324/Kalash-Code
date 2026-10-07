# Compact evaluation results — 2026-10-03

Sarvam-105B did not solve any of the four fresh tasks under the tested harness
configuration. The benchmark is complete, with all access probes and diagnostic
phases accounted for below. These results do not establish competitive parity.

## Access diagnosis

The saved key returned HTTP 400 with the beta-access message for GLM-5.3 at
`/v2/chat/completions`. Sarvam documents V2 access as a per-key entitlement;
changing the model setting cannot grant it. The same key successfully called
Sarvam-105B at `/v1/chat/completions`.

V1 also rejected `reasoning_effort=max`, specifying `low`, `medium`, `high`.
The adapter now selects `high` for V1 and `max` for V2 and rejects unsupported
explicit combinations locally.

[GLM-5.3 access requirements](https://docs.sarvam.ai/api/getting-started/models/openweight/glm-5-3),
[Sarvam-105B](https://docs.sarvam.ai/api/getting-started/models/sarvam-105b).

## Independent grading

The tasks cover TTL/LRU caching, DAG scheduling, Decimal CSV reconciliation and
streaming UTF-8 JSONL. All four broken baselines fail their hidden checks. All
four reference repairs pass them in the zero-API control. References and hidden
checks are withheld from the agent workspace; grading happens after execution
in a fresh, network-disabled copy. See [runner documentation](EVALS.md).

| Phase | Requested output per call, including reasoning | Verified repairs | Cumulative reported tokens | Cumulative conservative charge |
| --- | ---: | ---: | ---: | ---: |
| Reference control | Scripted | 4/4 | 0 | 0 |
| Live initial | 4,096 | 0/4 | 82,857 | 99,241 |
| Live larger output | 16,384 | 0/4 | 163,556 | 179,940 |
| After digest fix, Decimal task only | 16,384 | 0/1 | 188,685 | 205,069 |

The live phases used Sarvam-105B, high reasoning, temperature zero, one attempt
per task per phase, no SDK/gateway retries, and a shared 240,000-token allowance.
Each task had a 52,000-token ceiling, 180-second runtime allowance, 12 turns and
40 tool calls. Extensions, memory, delegation and agent network access were
disabled. The targeted phase is a selected diagnostic, not a rerun of the full
four-task suite. None of the nine live task attempts produced a source patch.

The final reported total includes the successful 141-token access probe. Two
HTTP 400 probes supplied no usage and retain 8,192 conservative tokens each.
The difference between reported usage and charged usage is therefore 16,384.
All later phases carry the previous ledger; their cumulative totals must not be
added together. The byte-based reservation is deliberately conservative, but
is not an exact provider tokenizer or a mathematical billing guarantee.

Estimated cost of calls with reported usage is **₹7.239202**, using Sarvam's
published INR rates. This estimate assumes rejected HTTP requests incurred no
generation charge; their usage remains unknown in the ledger. It is not an
invoice. [Pricing](https://docs.sarvam.ai/api/getting-started/pricing).

## What failed and what changed

All initial attempts ended with model output truncation. The larger-output
phase exposed repeated exploration, a run-budget stop, and conservative
reservation refusals. A reservation can refuse a request before the task's
reported usage reaches 52,000: the pending request must fit with its full output
allowance and the serialized request byte reservation.

The larger-output DAG and Decimal attempts also tried writes without the
required source digest. Investigation found a harness defect: the read tool
stored SHA-256 in internal metadata but omitted it from the observation given
to the model. Reads now include the source path and SHA-256 in visible output.
Stale-write protection remains enforced. Tool completion events also retain
error codes/remediation and zero-valued evidence.

A regression test proves an actual read exposes the correct digest, a write
without it fails, and a write with it succeeds. The selected live Decimal task
after this fix still ended with output truncation after four exploration calls,
without attempting a write. It therefore does **not** prove improved live repair
quality. The full regression suite passed **669 tests in 35.15s**; strict mypy
passed 162 source files. New evaluation modules/tests pass the complete Ruff
rules. The repository still has its pre-existing full-lint backlog.

## Reproducibility and limits

[Machine-readable results](evals/compact_2026_10_03.json) preserve each phase's
manifest, result summary, access diagnosis and final shared ledger. Manifests
record task/source/diff hashes, commit, dirty state, Python and platform.
Raw traces and grading workspaces remain in these local directories:

- `/tmp/kalash-compact-control-20261003`
- `/tmp/kalash-compact-live-20261003`
- `/tmp/kalash-compact-expanded-20261003`
- `/tmp/kalash-compact-fixed-20261003`

Temporary directories may be removed by the operating system; the saved report
does not depend on their survival. Four local tasks and one selected diagnostic
do not yield a stable statistical ranking. No competing harness or public
benchmark ran. These failures identify work to do; they do not support claims
that Kalash outperforms other harnesses.
