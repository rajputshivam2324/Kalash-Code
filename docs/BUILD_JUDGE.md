# Build evaluation with Codex as judge

This evaluation asks Kalash to build an offline incident-triage CLI from an empty
workspace. A fixed rubric, independent acceptance checks and an actual Codex
artifact review evaluate what it produced. The judge reviews source and behavior;
the builder's explanation is not evidence of correctness.

## Task and grading

The [task](../evals/tasks/build_triage.json) requires add/list/close/summary commands,
JSON persistence, severity ordering, filtering, Unicode, non-destructive errors,
atomic saves, a README and at least six candidate unittest tests.
The [rubric](../evals/tasks/build_triage_rubric.json) is frozen before generation:

| Dimension | Weight |
| --- | ---: |
| Functionality | 45% |
| Robustness | 20% |
| Maintainability | 15% |
| Usability | 10% |
| Testing | 10% |

Codex assigns each dimension 0–5 with a rationale and evidence IDs. The controller
computes the weighted score out of 100. Passing requires 75 points, every required
deliverable, all ten acceptance checks, and six or more passing candidate tests.
A high subjective score cannot override failed checks or missing files.

## Recorded results, 2026-10-03

| Attempt | Source produced | Independent behavior | Codex judgment |
| --- | --- | --- | ---: |
| Low reasoning, 8,192 output cap | None | No executable | 0/100, failed |
| Reasoning disabled, 6,144 output cap | `triage.py` | 9/10 acceptance checks; zero candidate tests | 53/100, failed |

The low-reasoning call exhausted its output cap before making any tool calls.
The second call created a useful CLI with one write. Its next request was refused
by the shared conservative reservation; README and tests were never generated.
Do not attribute that missing work solely to model ability: the controller stopped
the run before another model request. Artifact quality and termination are reported
separately. No candidate files were repaired by the judge.

| Dimension | Score for the partial CLI |
| --- | ---: |
| Functionality | 4/5 |
| Robustness | 1/5 |
| Maintainability | 3/5 |
| Usability | 2/5 |
| Testing | 0/5 |

The CLI handles normal persistence, Unicode, filters, sorted lists, closure and
summaries correctly. Its main defect is data loss: `load_incidents` returns an empty
list on malformed JSON/read errors instead of reporting failure. An independent
reproduction started with `{broken`, called add, and observed exit zero and a store
replaced with the new incident. Record schemas are not validated either. Readable
functions and argparse help earn partial credit; missing README/tests and generic
error messages lower the score.

The original negative-only corruption check passed vacuously in the first attempt
because the executable was missing. The original packet is preserved and that
result earns no partial credit. The current checker explicitly requires a program;
a regression test proves an absent program fails all ten checks. Review also found
an overrestrictive `[]` invalid-schema case: the task allows a list-based store.
The current checker removes it. The partial CLI fails on malformed JSON before
that case, so its verdict and score are unaffected. Frozen checker hashes make
both revisions visible.

## Accounting and evidence

The two new build calls consumed **14,639 reported Sarvam tokens**. Both retain all
prior benchmark charges, resulting in **203,324 cumulative reported tokens** and
**219,708 cumulative conservative charges**, within the shared 240,000 allowance.
Estimated cumulative reported-call cost is ₹8.047945, about ₹0.808743 added by
these builds. The existing accounting limitations in [EVALS.md](EVALS.md) apply.
Interactive Codex judgment tokens/cost are not exposed to this runner and are
not included or assumed zero; these totals measure the Sarvam builder calls.

[Saved evidence](evals/build_triage_2026_10_03.json) contains both frozen packets,
scorecards, source hashes, the complete generated source, the independent
data-loss reproduction, and the final shared ledger. Raw artifacts are in
`/tmp/kalash-build-judge-20261003` and `/tmp/kalash-build-judge-off-20261003`.
Temporary directories may disappear; the saved evidence preserves the source.

The harness interpreter was Python 3.12.12; acceptance checks and the independent
reproduction used system Python 3.14.7. This is one build per configuration and
one uncalibrated interactive Codex reviewer. Neither the scores nor the differing
reasoning settings establish statistical superiority or a market ranking.

## Reuse

Run from the repository root with a working OS sandbox and Sarvam credentials:

```bash
.venv/bin/python -m evals.build_trial \
  --output /tmp/kalash-build-new \
  --prior-ledger /tmp/kalash-build-judge-off-20261003/ledger.json \
  --reasoning off --output-limit 6144
```

This command carries prior usage into the same ledger and rejects existing output
directories. The remaining allowance may refuse another request; do not reset it
to evade the agreed budget. Sequential ledger copies are not a concurrent quota
service. The task, rubric, checks and source manifest are saved before model work.
The agent has no extensions, memory, delegation or network tools and only accesses
its build workspace. Verification copies its deliverables into a fresh,
network-disabled OS sandbox.

After execution, give `judge_packet.json` to Codex with its embedded judge
instructions. Save the actual review to a judgment JSON containing `judge: Codex`,
the packet SHA-256, dimensions with score/rationale/evidence, and a summary.
Examples are included in the saved evidence. Finalize without another API call:

```bash
.venv/bin/python -m evals.build_judge /tmp/kalash-build-new \
  --judgment /path/to/codex_judgment.json
```

The controller rejects invalid scores, duplicate/missing dimensions, unknown
evidence IDs or a mismatched packet hash. It writes `judge_report.json` and exits
nonzero when the result fails. Judgment is supplied by the reviewing Codex session;
the runner does not pretend a regex heuristic is an LLM judge. These functional
checks are not an adversarial sandbox audit.

Verification of the evaluation controller: **681 repository tests passed in
30.03s**; all four new modules/test files pass the complete configured Ruff rules.
The finalization CLI was exercised and returned exit 1 for the failed 53-point
scorecard. Existing repository lint debt remains separate from these checks.
