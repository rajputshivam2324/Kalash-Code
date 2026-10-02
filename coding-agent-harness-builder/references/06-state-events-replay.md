# 06 — Event Store, Checkpoints, Recovery, Rewind, Replay

## Contents
1. Principles
2. Event catalogue
3. Storage schema (SQLite/Postgres)
4. Writing events correctly
5. Crash recovery algorithm
6. Workspace snapshots and rewind
7. Replay modes
8. Reproducibility manifest
9. CLI semantics
10. Redaction and retention

---

## 1. Principles

- **Event sourcing.** `state = fold(events)`. Checkpoints (materialized state at seq N) exist only to make resume fast.
- **Append-only, ordered per run** (`seq` strictly increasing, no gaps). Never update or delete events (retention policy aside).
- **Write-ahead for effects.** Persist `ToolStarted` before running, `ToolCompleted/Failed` after. A `ToolStarted` without a terminal event identifies in-flight work after a crash.
- **Large payloads go to a blob store** (content-addressed, sha256), events carry references.
- **Schema-versioned** envelopes and payloads with upcasters for old versions.

## 2. Event catalogue

| Event | Payload highlights |
|---|---|
| `RunStarted` | task_id, config_fingerprint, policy_version, model+params, tool_versions, sandbox fingerprint, prompt_version, seed |
| `ContextBuilt` | token ledger by section, prefix_hash, compaction_level |
| `ContextCompacted` | level, before/after tokens, summary_blob, model used |
| `ModelRequested` | request_id, message_count, tokens_in_est, cache breakpoints |
| `ModelStreamStarted` / `ModelFirstToken` | ttft_ms |
| `ModelResponded` | usage, finish, latency_ms, response_blob, repaired_calls |
| `ModelFailed` | error_class, attempt, retryable |
| `ModelFallback` | from, to, reason |
| `ToolRequested` | call_id, name, args_blob, effect summary |
| `PolicyEvaluated` | decision, rule_id, policy_version |
| `ApprovalRequested` / `ApprovalResolved` | by, outcome, latency, scope |
| `ToolStarted` | call_id, sandbox exec id |
| `ToolCompleted` | exit/status, duration_ms, bytes, truncated, changed_paths, artifact ids |
| `ToolFailed` | error_class, message |
| `HookRan` | hook id, event, decision, duration |
| `FileSnapshot` | snapshot id, tree hash, trigger (pre-turn/pre-tool) |
| `LoopDetected` | pattern, rung |
| `GateEvaluated` | checks, pass/fail, outputs |
| `BudgetWarning` / `BudgetExceeded` | which limit, value |
| `SubagentStarted/Finished` | child_run_id, tools, budget slice, result summary |
| `UserMessage` / `UserQuestion` / `UserAnswer` | text blob |
| `CheckpointCreated` | seq, state_blob, size |
| `RunPaused` / `RunResumed` / `RunCancelled` / `RunCompleted` / `RunFailed` | classification, resumable flag |
| `SecurityAlert` | canary touched, escape attempt, injection signal |

Envelope: `{seq, event_id (ULID), run_id, parent_run_id?, ts, type, schema_version, trace_id, span_id, payload}`.

## 3. Storage schema

```sql
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, parent_run_id TEXT, task_id TEXT, status TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
  config_fingerprint TEXT NOT NULL, idempotency_key TEXT UNIQUE,
  model TEXT, outcome TEXT, cost_usd NUMERIC(10,4) DEFAULT 0, tokens_in BIGINT DEFAULT 0, tokens_out BIGINT DEFAULT 0
);

CREATE TABLE events (
  run_id TEXT NOT NULL REFERENCES runs, seq BIGINT NOT NULL,
  event_id TEXT NOT NULL UNIQUE, ts TIMESTAMPTZ NOT NULL, type TEXT NOT NULL,
  schema_version INT NOT NULL, trace_id TEXT, span_id TEXT, payload JSONB NOT NULL,
  prev_hash TEXT, hash TEXT,                       -- optional tamper-evident chain (hash = H(prev_hash || canonical(event)))
  PRIMARY KEY (run_id, seq)
);
CREATE INDEX events_type ON events (run_id, type);

CREATE TABLE checkpoints (
  run_id TEXT NOT NULL, seq BIGINT NOT NULL, state_blob TEXT NOT NULL, schema_version INT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (run_id, seq)
);

CREATE TABLE artifacts (
  artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL,
  sha256 TEXT NOT NULL, bytes BIGINT NOT NULL, created_at TIMESTAMPTZ NOT NULL, storage_uri TEXT
);

CREATE TABLE blobs (sha256 TEXT PRIMARY KEY, bytes BIGINT, storage_uri TEXT, created_at TIMESTAMPTZ);
```

SQLite: enable WAL, `synchronous=NORMAL` (FULL for crash-critical), one writer per run.

## 4. Writing events correctly

```python
class EventStore:
    async def emit(self, run, type, payload, blobs=()):
        async with run.write_lock:                       # one serial writer per run
            seq = run.next_seq(); ev = Event(seq=seq, ...)
            ev = redact(ev)                              # before persistence
            await self.db.insert(ev)                     # fsync'd transaction
            await self.bus.publish(ev)                   # stream to UI/OTel AFTER durable
        return ev
```

- Emit durable events before notifying subscribers.
- Never block the loop on slow sinks (OTel/Kafka): publish through a bounded queue and drop/spool on overflow, but **never** drop the DB write.
- Idempotency: `POST /runs` with `Idempotency-Key` returns the existing run.

## 5. Crash recovery algorithm

On `resume(run_id)`:

1. Acquire a run lease (advisory lock / row lock with heartbeat) so two workers don't resume the same run.
2. Load latest checkpoint ≤ last seq; fold remaining events.
3. Detect **in-flight** items:
   - `ModelRequested` without `ModelResponded`: safe, re-issue the request (billed twice at worst; record `ModelRetriedAfterCrash`).
   - `ToolStarted` without terminal event:
     - tool `idempotent` or `read_only`: re-run.
     - mutating: inspect via sandbox `diff(since=pre-tool snapshot)`. If the workspace shows the effect, synthesize `ToolResult(is_error=false, "completed before crash (inferred); verify state)"`; if not, re-run once; if ambiguous, return an error result: *"Outcome unknown after a harness crash; re-read affected files and verify before continuing."* The model then verifies.
4. Ensure **every `ToolUse` has a `ToolResult`** (fabricate error results for unmatched calls) so the provider accepts the history.
5. Re-provision the sandbox from the run's last snapshot if the original is gone.
6. Emit `RunResumed{reason, inferred_results}` and continue the loop.

Test by killing the process at every event boundary (fault injection hook `HARNESS_CRASH_AT=ToolStarted:3`) and asserting final workspace + history validity.

## 6. Workspace snapshots and rewind

Use a **shadow git repository** (separate `GIT_DIR` outside the workspace) so snapshots don't touch the user's `.git`:

```bash
export GIT_DIR=$RUN_DIR/shadow.git GIT_WORK_TREE=$WORKSPACE
git add -A --force && TREE=$(git write-tree) && COMMIT=$(git commit-tree $TREE -p $PREV -m "pre-turn 7")
```

- Snapshot **before each user turn** and **before each mutating tool batch** (cheap: content-addressed, incremental).
- `rewind <snapshot>`: restore files (`git read-tree`/`checkout-index`) and truncate or fork conversation history to the matching event seq. Prefer *forking* (new run linked via `parent_run_id`) so the original remains auditable.
- Honor ignore rules but also snapshot untracked files that the agent created; exclude huge dirs (`node_modules`, build outputs) via configurable ignores.
- Be explicit in the UI that shell-side effects outside the workspace (DB writes, remote pushes) can't be rewound.

## 7. Replay modes

| Mode | Model | Tools | Purpose |
|---|---|---|---|
| **Inspect** | – | – | Render timeline, diffs, costs from events |
| **Stub replay** | recorded responses fed in order | recorded results fed in order | Regression-test the *runtime* (loop, policy, context, state) deterministically, with zero cost |
| **Tool-live replay** | recorded responses | re-executed in fresh sandbox | Verify environment reproducibility, tool changes |
| **Model-live replay** | live model, same prompts | recorded or live | Compare models/prompt versions on the same trajectories |
| **Counterfactual** | recorded | recorded | Re-evaluate with a **different policy version** to see which calls would be denied/asked (policy change impact analysis) |

Replay verifies determinism by comparing event type sequence and tool args; mismatches are reported as `ReplayDivergence{seq, expected, actual}`. Mark inherently nondeterministic components (network, time, model sampling) in the manifest.

## 8. Reproducibility manifest (stored with `RunStarted`)

```json
{
  "harness_version": "1.4.2+git.ab12cd3",
  "config_fingerprint": "sha256:…",
  "prompt_versions": {"system": "v17", "summarizer": "v4", "tools": "sha256:…"},
  "model": {"provider": "…", "name": "…", "params": {"temperature": 0, "max_output": 8192, "reasoning_effort": "medium"}},
  "policy_version": "sha256:…",
  "sandbox": {"backend": "docker", "image_digest": "sha256:…", "limits": {"mem": "4g", "cpus": 2, "net": "none"}},
  "task": {"id": "repo_fix_001", "version": 3, "repo": "…", "commit": "…"},
  "instruction_files": [{"path": "AGENTS.md", "sha256": "…"}],
  "seeds": {"harness": 1234, "model": null},
  "host": {"os": "…", "kernel": "…", "python": "3.12.x"},
  "nondeterministic": ["model_sampling", "network", "wall_clock"]
}
```

## 9. CLI semantics

```bash
harness run task.yaml [--profile coding] [--mode auto] [--budget-usd 2.5] [--resume-on-crash]
harness resume RUN_ID                       # lease → recover (§5) → continue
harness inspect RUN_ID [--events|--cost|--diff|--timeline|--tools --failed]
harness replay RUN_ID --mode stub|tool-live|model-live|counterfactual [--policy new.yaml]
harness rewind RUN_ID --to SNAPSHOT_OR_SEQ [--fork]
harness export RUN_ID --format jsonl|otlp|html|bundle.tar.zst     # redacted by default
harness list-runs [--status failed --since 7d]
harness evaluate suite.yaml [--k 3] [--compare baseline_run_set]
harness doctor [--fix]                      # runs escape tests, checks creds, db, sandbox, tool registry, telemetry
```

## 10. Redaction and retention

- Redact **before** persisting: known secret patterns (cloud keys, tokens, private keys, JWTs), exact values of any secret the broker issued, `.env` contents, high-entropy strings next to keywords like `key|token|secret|password`.
- Keep a `redaction_count` metric; a spike means something is leaking into tool output.
- Retention policy per deployment: events 90 days, blobs 30 days, artifacts per contract; support run deletion for privacy requests (tombstone event + blob removal).
- Export bundles are redacted by default; unredacted export requires an explicit privileged flag and is itself audit-logged.
