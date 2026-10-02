# 10 — Build Plan: Milestones, Acceptance Tests, Repo Layout

## Contents
1. Repo layout
2. Milestones with exit tests
3. Minimal walking skeleton (first ~600 lines)
4. Config model
5. Observability specifics
6. Deployment artifacts
7. Operations runbook skeleton
8. Final readiness review

---

## 1. Repo layout

```text
harness/
├── pyproject.toml
├── src/harness/
│   ├── core/
│   │   ├── types.py            # Message/Block/Usage/ModelResponse/ModelCaps
│   │   ├── loop.py             # state machine, execute_calls, completion gate
│   │   ├── state.py            # RunState = fold(events); transitions table
│   │   ├── budgets.py          # Budget, Limits.check, cost calc
│   │   ├── policy.py           # PolicyEngine, rules, evaluation order
│   │   ├── effects.py          # Effect dataclasses (paths, argv, hosts)
│   │   ├── detectors.py        # LoopDetector, stall detector
│   │   ├── errors.py           # failure taxonomy + recovery map
│   │   └── reminders.py
│   ├── models/
│   │   ├── base.py             # ModelAdapter protocol, StreamAccumulator, repair
│   │   ├── anthropic.py  openai_compat.py  deepseek.py  vllm.py  scripted.py
│   │   └── pricing.py          # from config, not code
│   ├── tools/
│   │   ├── registry.py  executor.py  truncate.py  filestate.py
│   │   ├── fs_read.py  fs_edit.py  fs_patch.py  fs_write.py
│   │   ├── shell.py  shellparse.py  grep.py  glob.py  todo.py  ask_user.py  subagent.py  web.py
│   │   └── mcp/ (client.py, bridge.py)
│   ├── sandbox/
│   │   ├── base.py  bwrap.py  seatbelt.py  docker.py  proxy.py  snapshot.py  reaper.py
│   ├── context/
│   │   ├── builder.py  tokens.py  compaction.py  summary.py  instructions.py  repomap.py
│   ├── storage/
│   │   ├── events.py  checkpoints.py  blobs.py  artifacts.py  migrations/
│   ├── observability/
│   │   ├── logging.py  metrics.py  tracing.py  redact.py
│   ├── security/
│   │   ├── secrets.py  provenance.py  canaries.py
│   ├── hooks/  (runner.py)
│   ├── evals/
│   │   ├── runner.py  graders.py  report.py  compare.py  tasks/  adversarial/
│   ├── api/ (server.py, worker.py, schemas.py)
│   └── cli/ (main.py)
├── tests/ (unit/, contract/, integration/, adversarial/, replay/)
├── configs/ (default.yaml, profiles.yaml, policy.default.yaml)
├── docker/ (Dockerfile.harness, Dockerfile.sandbox, seccomp.json)
├── deploy/ (compose.yaml, k8s/, migrations/)
└── docs/ (architecture.md, threat-model.md, runbook.md, adr/)
```

TypeScript variant: same module boundaries as packages (`@harness/core`, `…/models`, `…/tools`, `…/sandbox`), Zod for schemas, `execa` for processes, Ink for TUI, Vitest. Rust variant: crates `core`, `models`, `tools`, `sandbox` with `tokio`, `serde`, `landlock` crate for FS sandboxing.

## 2. Milestones with exit tests

| M | Deliverable | Exit test (must be automated) |
|---|---|---|
| **M0** | Types, event store (SQLite), structured logging, `ScriptedModel`, config layering | Scripted run emits ordered events; `inspect` renders them; config precedence tests |
| **M1** | Loop + budgets + cancellation + loop detector + failure taxonomy | Always-tool-calling model stops at limit; cancel at every state leaves valid history; orphan-free property test |
| **M2** | `read_file`, `grep`, `glob`, `bash` (no sandbox yet, dev only, flagged) + truncation + artifacts | Timeout kills grandchildren; 10 MB output capped; stdin closed; env scrubbed |
| **M3** | First real adapter (streaming, usage, errors, retries) + contract tests | Adapter contract suite green on recorded fixtures + one live nightly |
| **M4** | `edit_file`, `apply_patch`, `write_file`, file-state cache, completion gate, final report | 5 seeded bug-fix tasks resolved; edit error-path tests; "tests pass" claim verification |
| **M5** | Policy engine + shell parser + path resolver + approvals + sandbox backend(s) | Adversarial suite: 0 escapes; shell table ≥100, path table ≥50; `doctor` runs escape tests |
| **M6** | Context builder, caching layout, instruction files, compaction L0–L3, reminders | 300-step run in 32k window passes; cache hit ≥80%; pair-integrity property test |
| **M7** | Checkpoints, resume, rewind (shadow git), replay (stub/tool-live/counterfactual) | Crash injection at every event boundary → resume correct; stub replay identical |
| **M8** | Subagents, hooks, MCP, plan mode, 2nd & 3rd provider (DeepSeek, vLLM), routing/fallback | Same smoke suite passes on ≥2 providers; hook deny cannot be overridden; MCP output labelled untrusted |
| **M9** | Eval runner (setup/agent/validate phases), graders, compare+stats, CI gates, API, Docker, health checks, dashboards | PR with seeded regression fails CI; `/readyz` reflects sandbox/db/model; load test 20 concurrent runs stable |

Do not start M(n+1) with M(n)'s exit test red. If time-boxed, cut breadth (providers, subagents, MCP) before cutting M5, M7 or M9.

## 3. Minimal walking skeleton (first ~600 lines)

Implement in this order so you have something runnable early without sacrificing structure:

1. `types.py` (§4 of SKILL.md), `errors.py`
2. `storage/events.py` (SQLite, `emit`, `fold`), `observability/logging.py` (JSON logs with `run_id`)
3. `models/scripted.py` returns a queue of `ModelResponse`
4. `tools/registry.py`, `executor.py` (validate → effect → policy(stub allow) → run → truncate → ToolResult)
5. `core/loop.py` with `Budget`, `can_continue`, cancellation token, one-result-per-call guarantee
6. `tools/fs_read.py`, `tools/grep.py`, `tools/shell.py` (process group + timeout)
7. `cli/main.py` with `run`, `inspect`
8. Tests: scripted multi-tool run, timeout, cancel, orphan property

Then swap `ScriptedModel` for a real adapter (M3).

## 4. Config model

Layered (low → high precedence): defaults → profile (`frontier|mid_tier|local_small`) → `/etc/harness/managed.yaml` (policy floor, org-owned) → user `~/.harness/config.yaml` → project `.harness/config.yaml` (**narrow-only**) → task spec → CLI/env overrides. Validate with Pydantic; unknown keys are errors; print the effective config with provenance (`harness config show --origin`). Secrets only via env/secret manager references (`${secret:NAME}`), never literals; `doctor` fails if a literal secret pattern is found in config.

```yaml
runtime: {max_iterations: 60, max_wall_seconds: 1800, max_parallel_tools: 4}
budget:  {max_cost_usd: 5.0, max_tool_calls: 200}
model:
  roles:
    executor:   {provider: anthropic, name: "${MODEL_EXECUTOR}", profile: frontier}
    summarizer: {provider: openai_compat, base_url: "${SUMMARIZER_URL}", name: "${MODEL_SUMMARIZER}", profile: mid_tier}
  fallback: [executor→${MODEL_FALLBACK}]
  pricing_file: configs/pricing.yaml
sandbox: {backend: auto, network: none, workspace: rw, protected: [".git", ".harness"]}
policy:  {file: configs/policy.default.yaml, mode: auto}
context: {l1: 0.60, l2: 0.80, l3: 0.92, tool_result_max_bytes: 30000}
gate:    {commands: [{name: tests, cmd: "${TEST_CMD}", must_pass: true}]}
observability: {tracing: otlp, metrics: prometheus, log_level: info, redact: true}
storage: {events: "sqlite:///~/.harness/runs.db", blobs: "~/.harness/blobs"}
```

## 5. Observability specifics

Metrics (Prometheus/OTel names):

```text
harness_model_requests_total{provider,model,status}
harness_model_tokens_total{provider,model,kind=input|output|cache_read|cache_write|reasoning}
harness_model_latency_seconds / harness_model_ttft_seconds (histograms)
harness_model_retries_total{class}   harness_circuit_state{provider}
harness_tool_calls_total{tool,decision,status}   harness_tool_duration_seconds{tool}
harness_tool_timeouts_total{tool}   harness_edit_apply_failures_total{kind}
harness_policy_decisions_total{decision,rule_id}   harness_security_alerts_total{kind}
harness_run_duration_seconds{outcome}   harness_run_cost_usd{outcome}   harness_runs_active
harness_compactions_total{level}   harness_cache_hit_ratio
harness_sandbox_provision_seconds{backend}   harness_sandbox_oom_total   harness_sandbox_orphans
```

Traces: root span per run; child spans `model.call`, `tool.exec`, `policy.eval`, `sandbox.exec`, `context.compact`; attributes include `run_id`, token counts, tool name, decision, **not** raw prompts (store those as redacted blobs). Link subagent runs as child traces.

Logs: JSON, one event per line, fields `ts, level, service, run_id, trace_id, event, …`; never log secrets or full file contents.

Dashboards/alerts: success rate by task type, cost per run p50/p95, tool error rate by tool, edit failure rate, cache hit ratio, compaction frequency, queue depth, orphan sandboxes > 0, any `SecurityAlert`, provider error rate, circuit open.

## 6. Deployment artifacts

- `Dockerfile.harness`: slim base, non-root user, pinned deps, healthcheck, only what the control plane needs (no compilers).
- `Dockerfile.sandbox`: language toolchains + ripgrep + git, non-root `agent` user (uid 1000), no sudo, no package manager caches with credentials, minimal setuid binaries removed.
- `compose.yaml` for local service: api, worker, postgres, redis (if needed), otel-collector, egress-proxy.
- Kubernetes: worker Deployment (HPA on queue depth), sandboxes as Jobs/Pods with `runtimeClassName: gvisor` (or Kata), NetworkPolicy default-deny with proxy egress, ResourceQuota/LimitRange per tenant namespace, PodSecurity `restricted`.
- DB migrations (Alembic/Atlas) run as a job; **backward-compatible** migrations (expand/contract).
- Health: `/healthz` liveness (process alive), `/readyz` readiness (db, queue, sandbox backend smoke exec, model credential check with cached TTL).
- Graceful shutdown: stop accepting runs, checkpoint in-flight runs, release leases, then exit (SIGTERM handler).

## 7. Operations runbook skeleton

- **Runaway cost**: query `runs` by cost; cancel via API; tighten budget; replay trajectory to find cause.
- **Provider outage**: breaker opens → fallback chain; if none, queue pauses, runs checkpointed; resume after recovery (`harness resume --all-failed --class TransientModelError`).
- **Sandbox escape suspected**: kill switch, quarantine tenant, snapshot node, rotate credentials, replay under counterfactual policy, add regression test.
- **Bad release**: roll back image; runs are resumable across versions only if checkpoint `schema_version` is compatible; upcasters tested.
- **Orphan sandboxes**: reaper job by label/TTL; alert if count > 0 for >10 min.
- **Data deletion request**: tombstone events, purge blobs/artifacts, verify export bundles.

## 8. Final readiness review

Before saying "production-ready":

1. Run the §8 gate table from SKILL.md and attach the **actual outputs** (pytest summary, adversarial results, crash-injection matrix, eval report with CIs, cost/latency distributions).
2. Present the **threat model** and which risks are accepted vs mitigated.
3. List **known limitations** with what's missing, why it matters, safe boundary until fixed.
4. Show an end-to-end demo run's `inspect` output: timeline, policy decisions, costs, verification evidence.
5. Provide the rollback procedure and have tested it once.
