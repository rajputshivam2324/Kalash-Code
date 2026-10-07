# Compact evaluation suite

The user explicitly resumed live benchmarking on 2026-10-03 with a total budget
below 250,000 tokens and authorized Sarvam-105B when GLM-5.3 is beta-gated.
See [the recorded results](COMPACT_EVAL_RESULTS.md) for measured outcomes,
shared token totals and the digest visibility defect found during execution.
For a build-from-scratch task judged by Codex, see [BUILD_JUDGE.md](BUILD_JUDGE.md).

## Four fresh tasks

`evals/tasks/compact_v1.json` defines TTL/LRU expiration and eviction, deterministic
DAG scheduling, Decimal CSV reconciliation and lazy UTF-8 JSONL parsing. Each has
a broken source baseline, seeded independent assertions/property checks and a
reference repair used only for control verification. Model workspaces contain
source and the task prompt, not hidden checks or reference repairs. A fresh copy
receives the checks after the agent stops; process exit zero alone is insufficient.

Run the zero-API control first:

```bash
.venv/bin/python -m evals.compact --scripted --output /tmp/kalash-compact-control
```

Control success validates the fixtures and mechanics. It is not a model score.
The evaluation requires a working OS shell sandbox. Extensions, memory, network
and delegation are disabled in the agent workspace. Hidden graders use stdlib and
an isolated Python interpreter; out-of-scope files and symlinks are rejected.

## Live accounting

The live runner is `evals/compact.py`, with `evals/compact_budget.py` wrapping every
provider attempt, including summary requests. Its default suite allowance is
240,000 tokens, not 240,000 per task. A task has a 52,000-token ceiling; output is
requested at 4,096 including reasoning, high effort, 180 seconds and 12 turns.
Gateway retries and SDK retries are disabled. There is one live attempt per task
in each explicitly recorded phase.

Before sending, the ledger saves a reservation: serialized request UTF-8 bytes
plus 4,096 framing tokens and the requested output maximum. Reported usage from
a normally exhausted stream with a recognized finish reason replaces this
reservation; failed, cancelled or missing-usage attempts keep the conservative
charge, even when they supplied partial usage. A capped generation can finish
its transport and settle usage while still failing the task. Cache reads are
already included in prompt tokens and reasoning is already included in completion
tokens. Neither is added twice.
Cache writes are added only when separately reported. Cumulative per-counter
reports retain their observed high-water marks; repeated snapshots are not summed.
Incomplete reports retain their counters as evidence but do not imply known cost.

Controller refusals are saved separately in `ledger.json` under `refusals` and
do not debit an attempt. They identify suite tokens, task tokens or a prior
reservation overrun and record wire bytes, framing/output allowance and remaining
ceilings. The runtime reports these as budget exhaustion without provider retry.
Sequential continuations preserve this refusal history alongside prior attempts.
The byte estimate and all ceilings remain unchanged; this adds diagnostics, not
an exact tokenizer or new spending authority.

This exceeds the harness's ordinary character estimate by a large margin, but
Sarvam exposes no exact server tokenizer here. It is not a mathematical bound
on server-side billing. A reported overrun stops the suite and marks its allowance
violated. Both reported usage and conservative charges remain in the report;
unknown usage is never labelled zero. Access probe charges also share the ledger.
An existing output directory is rejected to prevent overwriting an earlier run.
A sequential diagnostic phase can use `--prior-ledger` and `--output-limit`; it
carries every earlier charge into the same allowance. Do not branch or run these
phases concurrently: a copied prior ledger is a sequential accounting mechanism,
not a distributed quota service.

An access log records sanitized endpoint results, generation caps and usage,
without credentials. The live runner requires a configured Sarvam credential
and working OS sandbox. The task's metered request establishes service access;
a separate probe is optional. Supplied probe charges still enter the ledger:

```bash
.venv/bin/python -m evals.compact \
  --output /tmp/kalash-compact-live \
  --access-log /tmp/kalash-access-check.json \
  --total-tokens 240000
```

For a bounded diagnostic, `--task ttl-lru --reasoning off` selects one frozen
task and disables V1 reasoning explicitly. Defaults remain the full four-task
suite with high reasoning. Any live run requires the user's explicit allowance
and scope. Future live runs retain redacted normalized request projections and
stream metadata in `provider.jsonl`; raw block-delta bodies are omitted to avoid
logging split credential fragments. These are not exact HTTP wire dumps.

The user-authorized [2026-10-05 trial](SARVAM_RELIABILITY_2026_10_05.md) failed
0/1 with 29,953 charges under a new separate 60,000-token allowance. It did not
resume or reset the historical allowance. The original run preceded provider
trace capture; its evidence remains unchanged.

## Provider diagnosis

The live key returned HTTP 400 for GLM-5.3 V2 beta access, matching the documented
per-key restriction. Sarvam's V1 service also rejected `reasoning_effort=max` and
specified `low`, `medium`, `high`. A corrected V1 high-effort probe succeeded.
The adapter now chooses high for V1 and max for V2, rejects invalid explicit
combinations locally, and leaves explicit V1 None as disabled reasoning.
The published V1 schema's enum and prose are inconsistent with each other; the
observed service validation is recorded in the access artifact.

[GLM-5.3 access](https://docs.sarvam.ai/api/getting-started/models/openweight/glm-5-3),
[V1 reference](https://docs.sarvam.ai/api-reference/chat/chat-completions-v1).

Estimated generation cost is reported in INR using ₹29.28 input, ₹10.98 cached
input, ₹73.2 output per million tokens, checked 2026-10-03. No exchange-rate
assumption is needed. Runtime USD pricing is unknown for this compatible provider;
its dollar ceiling is not the benchmark's control. The shared token ledger is.
Estimates are not an invoice; unknown successful/partial usage makes cost unknown.
[Sarvam pricing](https://docs.sarvam.ai/api/getting-started/pricing).

## Artifacts and interpretation

The output contains manifest/ledger/access/summary JSON and, per task, scrubbed
events, final answer, candidate patch, result and fresh grading workspace.
The manifest records task hash, Python/platform, source tree/diff hashes, commit
and dirty state. Preserve these when comparing a future change.

Report artifact verification separately from termination: a budget-stopped task
can still produce a correct artifact, and a model claiming completion can fail
its grader. Provider errors and timeouts remain visible. Four tasks with
separately labelled diagnostic phases
are a local smoke evaluation, not SWE-bench, a stable statistical comparison or
proof of performance against another harness. No competitor ran in this suite.
