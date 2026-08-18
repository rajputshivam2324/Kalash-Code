# Observability

> **Status:** normative.

Audit, tracing, error taxonomy, logging, metrics, and diagnostics for Kalash Code.

---

## 1. Audit Event Schema

All audit events are append-only rows in `audit_log` (see `data-model.md` §2.7).

| Field | Type | Description |
|-------|------|-------------|
| `id` | TEXT | ULID |
| `timestamp` | TEXT | ISO-8601 UTC (I-038) |
| `session_id` | TEXT | Nullable; correlates to session |
| `turn_id` | TEXT | Nullable |
| `run_id` | TEXT | Agent run that produced this event |
| `parent_event_id` | TEXT | Causal parent (e.g. tool.execute → egress.request) |
| `actor` | TEXT | `user`, `agent:<name>`, `schedule:<id>`, `hook:<name>` |
| `event_type` | TEXT | See §1.1 |
| `target` | TEXT | Resource acted upon (path, URI, memory key) |
| `action` | TEXT | Verb: read, write, execute, delete, grant, deny |
| `arguments_digest` | TEXT | SHA-256 of canonical JSON arguments (I-007) |
| `result_digest` | TEXT | SHA-256 of result — **never raw payloads** (I-007) |
| `permission_decision` | TEXT | allow/deny/escalate if permission was checked |
| `capabilities_required` | TEXT | JSON array of caps needed |
| `capabilities_source` | TEXT | config/user_prompt/grant/schedule |
| `sandbox_mode` | TEXT | Effective sandbox at time of event |
| `provider` | TEXT | Model provider if applicable |
| `model` | TEXT | Model identifier |
| `tokens` | INTEGER | Total tokens (input+output) |
| `cost` | REAL | USD cost |
| `outcome` | TEXT | success/failure/timeout/denied |
| `error_code` | TEXT | KALASH_* code if failed |
| `duration_ms` | INTEGER | Wall-clock duration |
| `prev_hash` | TEXT | Hash of preceding event (chain) |
| `hash` | TEXT | SHA-256(canonical_json(event) + prev_hash) |

### 1.1 Event Types

| Event type | Emitted when |
|------------|--------------|
| `tool.execute` | Any tool invocation starts/completes |
| `egress.request` | Outbound network call leaves the process (I-003) |
| `permission.decide` | Permission decision point evaluated |
| `memory.write` | Memory ledger entry created or updated |
| `memory.forget` | Memory entry tombstoned (I-005) |
| `hook.run` | Lifecycle hook fires |
| `agent.spawn` | Subagent created |
| `schedule.fire` | Scheduled run begins |
| `plugin.load` | Plugin loaded into runtime |
| `trust.grant` | Trust grant created or modified |
| `config.change` | Configuration value altered |
| `session.open` | New session started |

### 1.2 Integrity

- **Digests not payloads** (I-007): arguments and results stored as SHA-256 digests; raw values are in session messages only.
- **Hash chain**: each event's `hash` = SHA-256(canonical JSON of event fields + `prev_hash`). Tamper detection via chain verification.
- **Append-only**: no UPDATE/DELETE on `audit_log`. Retention policy truncates from the tail only.

---

## 2. Trace Model

Hierarchy (parent → child):

```
session
 └─ turn
     ├─ model_call
     │   ├─ tool_call
     │   ├─ memory_recall
     │   │   └─ provider_fanout
     │   └─ ...
     ├─ memory_write
     └─ subagent
         └─ model_call
             └─ ...
```

| Mechanism | Detail |
|-----------|--------|
| Correlation | `contextvars` propagated; env vars `KALASH_SESSION_ID`, `KALASH_TURN_ID`, `KALASH_RUN_ID` for subprocesses |
| Storage | Local traces in `audit_log` + `tool_calls` tables |
| OpenTelemetry | Opt-in via `kalash config set observability.otel=true`; exports OTLP/gRPC |
| Viewing | `kalash logs --trace <id>` renders a span tree in terminal |
| Span attributes | tool_name, provider, model, tokens, cost, sandbox_mode, error_code |

---

## 3. Error Taxonomy

Format: `KALASH_<AREA>_<REASON>`

| Code | Meaning | Retry | Fallback | User-actionable | Fatal | Safe for model | Safe to log |
|------|---------|:-----:|:--------:|:---------------:|:-----:|:--------------:|:-----------:|
| **CONFIG** | | | | | | | |
| `KALASH_CONFIG_INVALID` | Config fails schema validation | No | No | Yes | Yes | Yes | Yes |
| `KALASH_CONFIG_MISSING` | Required key absent | No | No | Yes | Yes | Yes | Yes |
| `KALASH_CONFIG_DEPRECATED` | Deprecated key used | No | No | Yes | No | Yes | Yes |
| `KALASH_CONFIG_CONFLICT` | Mutually exclusive keys | No | No | Yes | Yes | Yes | Yes |
| **MODEL** | | | | | | | |
| `KALASH_MODEL_TIMEOUT` | Provider did not respond | Yes | Yes | No | No | Yes | Yes |
| `KALASH_MODEL_RATE_LIMIT` | 429 from provider | Yes | Yes | No | No | Yes | Yes |
| `KALASH_MODEL_CONTEXT_OVERFLOW` | Input exceeds model window | No | Yes | No | No | Yes | Yes |
| `KALASH_MODEL_CONTENT_FILTER` | Provider refused content | No | No | Yes | No | No | Yes |
| `KALASH_MODEL_AUTH` | Invalid/expired API key | No | No | Yes | No | No | No |
| `KALASH_MODEL_UNAVAILABLE` | Provider 5xx or unreachable | Yes | Yes | No | No | Yes | Yes |
| `KALASH_MODEL_PARSE` | Response not parseable | Yes | No | No | No | Yes | Yes |
| **TOOL** | | | | | | | |
| `KALASH_TOOL_NOT_FOUND` | Tool name unregistered | No | No | No | No | Yes | Yes |
| `KALASH_TOOL_TIMEOUT` | Tool exceeded time limit | No | No | No | No | Yes | Yes |
| `KALASH_TOOL_SANDBOX_DENY` | Sandbox blocked operation | No | No | No | No | Yes | Yes |
| `KALASH_TOOL_EXEC_FAIL` | Tool returned non-zero / threw | No | No | No | No | Yes | Yes |
| `KALASH_TOOL_INVALID_ARGS` | Schema validation of args failed | No | No | No | No | Yes | Yes |
| **PERMISSION** | | | | | | | |
| `KALASH_PERMISSION_DENIED` | User denied capability request | No | No | Yes | No | Yes | Yes |
| `KALASH_PERMISSION_ESCALATION` | Attempted capability not in grant | No | No | No | No | Yes | Yes |
| `KALASH_PERMISSION_EXPIRED` | Grant TTL elapsed | No | No | Yes | No | Yes | Yes |
| **SANDBOX** | | | | | | | |
| `KALASH_SANDBOX_INIT_FAIL` | Could not establish sandbox | No | No | Yes | Yes | Yes | Yes |
| `KALASH_SANDBOX_ESCAPE` | Sandbox boundary violated (T-7) | No | No | No | Yes | Yes | Yes |
| `KALASH_SANDBOX_RESOURCE` | Resource limit hit (OOM, disk) | No | No | Yes | No | Yes | Yes |
| **MEMORY** | | | | | | | |
| `KALASH_MEMORY_PROVIDER_FAIL` | Federation source unreachable | Yes | Yes | No | No | Yes | Yes |
| `KALASH_MEMORY_CORRUPT` | Integrity check failed (I-016) | No | No | Yes | No | Yes | Yes |
| `KALASH_MEMORY_QUOTA` | Memory store size exceeded | No | No | Yes | No | Yes | Yes |
| `KALASH_MEMORY_POISONING` | Provenance validation failed (T-4) | No | No | No | No | No | Yes |
| **STORAGE** | | | | | | | |
| `KALASH_STORAGE_LOCKED` | DB write lock held by another | Yes | No | No | No | Yes | Yes |
| `KALASH_STORAGE_CORRUPT` | SQLite integrity_check failed | No | No | Yes | Yes | Yes | Yes |
| `KALASH_STORAGE_FULL` | Disk space exhausted | No | No | Yes | Yes | Yes | Yes |
| `KALASH_STORAGE_MIGRATION` | Migration failed | No | No | Yes | Yes | Yes | Yes |
| **MCP** | | | | | | | |
| `KALASH_MCP_CONNECT` | Cannot reach MCP server | Yes | No | Yes | No | Yes | Yes |
| `KALASH_MCP_PROTOCOL` | Protocol violation from server | No | No | Yes | No | Yes | Yes |
| `KALASH_MCP_TRUST` | Server not trusted for operation | No | No | Yes | No | Yes | Yes |
| **PLUGIN** | | | | | | | |
| `KALASH_PLUGIN_LOAD` | Plugin failed to initialize | No | No | Yes | No | Yes | Yes |
| `KALASH_PLUGIN_CRASH` | Plugin raised unhandled exception | No | No | Yes | No | Yes | Yes |
| **HOOK** | | | | | | | |
| `KALASH_HOOK_TIMEOUT` | Hook exceeded time limit | No | No | Yes | No | Yes | Yes |
| `KALASH_HOOK_FAIL` | Hook returned non-zero | No | No | Yes | No | Yes | Yes |
| **SCHEDULER** | | | | | | | |
| `KALASH_SCHEDULER_LEASE_LOST` | Heartbeat expired; lease stolen | No | No | No | No | Yes | Yes |
| `KALASH_SCHEDULER_BUDGET` | Token/cost budget exceeded | No | No | Yes | No | Yes | Yes |
| **SESSION** | | | | | | | |
| `KALASH_SESSION_CONFLICT` | Concurrent modification (I-023) | No | No | Yes | No | Yes | Yes |
| `KALASH_SESSION_EXPIRED` | Session archived or TTL passed | No | No | Yes | No | Yes | Yes |
| **AGENT** | | | | | | | |
| `KALASH_AGENT_LOOP` | Agent exceeded max iterations | No | No | No | Yes | Yes | Yes |
| `KALASH_AGENT_DEPTH` | Subagent nesting too deep | No | No | No | Yes | Yes | Yes |
| **EGRESS** | | | | | | | |
| `KALASH_EGRESS_BLOCKED` | Network call denied by policy (I-003) | No | No | No | No | Yes | Yes |
| `KALASH_EGRESS_TIMEOUT` | Outbound request timed out | Yes | No | No | No | Yes | Yes |
| **SECRET** | | | | | | | |
| `KALASH_SECRET_NOT_FOUND` | Keyring entry missing (I-035) | No | No | Yes | No | No | No |
| `KALASH_SECRET_ACCESS` | Keyring access denied by OS | No | No | Yes | Yes | No | No |
| **STATE** | | | | | | | |
| `KALASH_STATE_INVALID` | State machine transition illegal | No | No | No | Yes | Yes | Yes |
| `KALASH_STATE_RECOVERY` | Crash recovery activated | No | No | No | No | Yes | Yes |

---

## 4. Logging

| Property | Value |
|----------|-------|
| Library | `structlog` (structured, composable processors) |
| Redaction | Filter runs early in processor chain, before any handler (I-007) |
| Console | `rich` renderer; respects `NO_COLOR`, `TERM` |
| File | JSON lines to `~/.kalash/logs/kalash-<date>.jsonl`, mode `0600` |
| Rotation | Daily; 7 files retained (configurable) |

### 4.1 Log Tiers

(Defined in `security.md` §6; referenced here for completeness.)

| Tier | Content | Default sink |
|------|---------|-------------|
| AUDIT | Security-relevant events; hash-chained | `audit_log` table |
| INFO | User-visible operations, model calls | Console + file |
| DEBUG | Internal state, tool args (redacted) | File only |
| TRACE | Wire-level protocol, raw tokens | File only; opt-in |

---

## 5. Metrics

Collected in-process; exportable via OTEL or `kalash metrics` CLI.

| Metric | Type | Labels |
|--------|------|--------|
| `kalash_turn_duration_ms` | Histogram | session, agent |
| `kalash_ttft_ms` | Histogram | provider, model |
| `kalash_tool_duration_ms` | Histogram | tool_name, outcome |
| `kalash_recall_latency_ms` | Histogram | provider |
| `kalash_cache_hit_rate` | Gauge | cache_name |
| `kalash_tokens_total` | Counter | provider, model, direction |
| `kalash_cost_usd_total` | Counter | provider, model |
| `kalash_errors_total` | Counter | error_code, area |
| `kalash_circuit_breaker_state` | Gauge | provider — 0=closed, 1=half, 2=open |
| `kalash_sessions_active` | Gauge | — |
| `kalash_memory_entries` | Gauge | source, namespace |
| `kalash_gc_reclaimed_bytes` | Counter | class |

---

## 6. `kalash doctor`

Diagnostic command output sections:

| Section | Checks |
|---------|--------|
| Platform | OS, arch, Python version, virtualenv |
| Sandbox | Backend detected (Landlock/bubblewrap/Seatbelt/AppContainer), effective level |
| Providers | Each configured provider: auth valid, model reachable, latency |
| Model registry | Local models present, ONNX runtime available, sqlite-vec loaded |
| Memory | KME index integrity, federation sources reachable |
| MCP | Each registered server: transport test, protocol version |
| Database | `PRAGMA integrity_check`, WAL status, migration version |
| Disk | `~/.kalash` size, blob count, available space |
| Config | Warnings for deprecated keys, conflicts, missing recommended |
| Secrets | Keyring backend, entries present (names only, never values) |

Exit codes: `0` = healthy, `1` = degraded (non-fatal warnings), `2` = unhealthy (fatal issues found).

---

*See also:* [`security.md`](./security.md) §6 (log tiers, redaction), [`data-model.md`](./data-model.md) §2.7 (audit_log DDL), [`state-machines.md`](./state-machines.md) (state transition errors), [`invariants.md`](./invariants.md) I-007 (no secrets in logs).
