# Offline audit observations

`probes.py` captures 14 diagnostic groups against the current tree using fake
providers, temporary files and a temporary `KALASH_HOME`. It imports test helpers
and is intended to run from the repository with development dependencies installed.
It performs no live model calls, publication or real credential inspection.

Run from the repository with:

```sh
PYTHONPATH=. .venv/bin/python docs/evals/harness_audit_2026_10_04/probes.py
```

The script prints observations; it does not assert that observed defects are
correct behavior. `results.json` is the captured 2026-10-04 review output, not an
automatically passing regression suite. Turn each fixed defect into an independent
regression with the desired behavior before claiming it resolved.

This environment's restricted nested sandbox blocked SQLite worker completion.
Capture succeeded through an approved external pytest invocation using a temporary
wrapper: **1 passed in 1.21s**. The wrapper asserted 14 captured groups, not harness
correctness. No shared ledger was reset. All fixture storage was temporary.

See [the review](../../HARNESS_AUDIT_2026_10_04.md) for interpretation, source
references, controls, limitations and proposed acceptance cases.
