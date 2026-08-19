---
name: eval-harness-run
description: Comprehensive workflow to run, debug, and expand Kalash evaluation suites (capability, safety, policy, token economics) and e2e tests.
version: 1.0.0
allowed-tools: [shell, read, write, edit, glob, search]
---

# Evaluation Harness Execution & Verification Skill

Use this skill whenever running or modifying evaluation suites, verifying code changes against regressions, or adding new benchmarks.

## 1. Running Deterministic Offline Evals
Execute the main eval runner:
```bash
uv run python -m evals.run
```
Expected output:
- **capability**: 100% (all filesystem-scored tasks succeed)
- **safety**: 100% (all dangerous commands blocked)
- **policy**: 100% (all approval/risk matrices respected)
- **token efficiency**: 100% (all compression & prompt floor thresholds met)

## 2. Running Test Suites
```bash
uv run pytest -q
uv run pytest -v tests/unit/test_runtime.py
uv run pytest -v tests/integration/
```

## 3. Adding New Capability Tasks
To add a new capability benchmark:
1. Open `evals/suites.py`.
2. Add a `CAPABILITY_TASKS` entry with deterministic `ScriptedTurn` tool actions.
3. Define the filesystem assertion verifying the outcome.
4. Run `uv run python -m evals.run` to ensure zero regression.

## 4. Invariant Checking
Ensure that:
- Writes never overwrite without digest verification or atomic rename.
- Shell commands respect sandbox write roots.
- All tools wrap errors in `ToolEnvelope(success=False, error=ToolError(...))`.
