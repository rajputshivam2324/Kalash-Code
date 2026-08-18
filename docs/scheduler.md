# Scheduler

> **Status:** normative.

Durable cron scheduler for Kalash Code. A schedule is **deferred, unattended authority** — the danger is not cron but that the agent acts with nobody watching.

---

## 1. Schedule Model

```sql
-- Full DDL in data-model.md §2.5; key fields summarized here.
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | ULID | Primary key |
| `name` | TEXT | Human-readable unique name |
| `spec` | TEXT | Cron expression, interval, ISO timestamp, or event pattern |
| `spec_kind` | ENUM | `cron` \| `interval` \| `once` \| `event` |
| `timezone` | TEXT | IANA zone for interpretation (stored fire times are UTC, I-038) |
| `agent` | TEXT | Agent profile to execute |
| `prompt` | TEXT | Instruction given to agent at fire time |
| `sandbox_mode` | TEXT | Default: `read-only` |
| `approval_policy` | TEXT | `never` \| `always` \| `destructive_only` |
| `capabilities` | JSON | Attenuated from creating principal — cannot exceed creator's caps |
| `tools_allow` | JSON | Allowlist of tool names; null = all permitted by caps |
| `catchup_policy` | TEXT | `fire_missed` \| `skip_missed` |
| `overlap_policy` | TEXT | `skip` \| `queue` \| `parallel` |
| `jitter_s` | INT | Random delay 0..N seconds to prevent synchronized load |
| `max_runs` | INT | Total lifetime runs; null = unlimited |
| `max_consecutive_failures` | INT | Auto-disable threshold (default 3) |
| `enabled` | BOOL | Master switch |
| `next_run_at` | TEXT | Pre-computed next UTC fire time |
| `budget_tokens` | INT | Max tokens per run |
| `budget_cost_usd` | REAL | Max cost per run |

---

## 2. Spec Kinds

### 2.1 Cron

Standard 5-field: `minute hour day-of-month month day-of-week`.

- Interpreted in the schedule's `timezone`.
- `next_run_at` stored as UTC (I-038).
- **DST spring-forward**: 2:00 AM that doesn't exist → skip or fire at next valid instant (per `catchup_policy`).
- **DST fall-back**: 2:00 AM that occurs twice → fire once at first occurrence only.

### 2.2 Interval

`<integer><unit>` — units: `s`, `m`, `h`, `d`. Example: `30m`.

- First fire at `created_at + interval`.
- Drift-resistant: next fire = last actual fire + interval (not last completion).

### 2.3 Once

ISO-8601 timestamp. Fires exactly once. Record retained per retention policy (90 days).

### 2.4 Event

Sources:

| Source | Trigger | Debounce |
|--------|---------|----------|
| Filesystem change | inotify/FSEvents/ReadDirectoryChanges (debounced 2s default) | Configurable |
| Git post-commit hook | Symlinked hook fires `kalash cron trigger --event=git:post-commit` | Per-commit |
| Git post-merge hook | Same mechanism | Per-merge |
| Manual | `kalash cron trigger <name> [--payload=JSON]` | None |
| HTTP webhook | `kalash serve` endpoint: `POST /webhook/<schedule-name>` | Rate-limited |

**Webhook authentication:**
- HMAC-SHA256 signature in `X-Kalash-Signature` header
- Timestamp in `X-Kalash-Timestamp`; reject if |now - ts| > 300s
- Nonce in `X-Kalash-Nonce`; replay detection via sliding window (1h)
- Events are **untrusted input** (I-033): validated, schema-checked, never interpolated into shell

**Rate limiting:** max 60 events/min per schedule (configurable). Excess events queued or dropped per `overlap_policy`.

**Debounce/coalesce:** filesystem events within debounce window merged into single fire; payload contains list of changed paths.

---

## 3. Execution Model

Matches `state-machines.md` §8 exactly.

| Component | Behaviour |
|-----------|-----------|
| Daemon | `kalash serve` — polls `schedules` for `next_run_at ≤ now` |
| Leasing | Acquires row in `schedule_leases`; heartbeat every 30s; expires at 90s |
| Headless session | Creates a session with `actor=schedule:<id>` |
| Capability attenuation | Run's caps ⊆ schedule's caps ⊆ creator's caps at creation time |
| Concurrency | Global limit (default 4) + per-schedule via `overlap_policy` |
| Budget enforcement | Tokens/cost tracked per-run; exceeded → `KALASH_SCHEDULER_BUDGET`, run terminated |
| Completion | `schedule_runs` row updated; `next_run_at` recomputed |

---

## 4. Safety Defaults

| Setting | Default | Rationale |
|---------|---------|-----------|
| `sandbox_mode` | `read-only` | Cannot damage filesystem |
| `approval_policy` | `never` | In read-only, nothing dangerous to approve |
| Write access | Requires `kalash cron add --unattended-write` | Explicit opt-in; user warned |
| Confirmation-class actions (I-032) | **STOP** run; report to notification channels | No unattended confirmation |
| Auto-disable | After `max_consecutive_failures` (default 3) | Prevent runaway failures |
| Review reminder | Long-lived write schedules prompt review every 30 days | Stale authority is dangerous |
| Network egress | Passes egress gateway (I-003, I-006) | Outbound calls logged and filtered |

### 4.1 Escalation Ladder

```
read-only (default)
  → read-only + network (add network cap)
    → write (--unattended-write; user must acknowledge risk)
      → write + network (maximum unattended authority)
```

Each step requires explicit user action at creation time. Cannot be widened after creation without re-creating the schedule.

---

## 5. Notifications

### 5.1 Channels

| Channel | Backend | Config key |
|---------|---------|------------|
| Terminal bell | `\a` to controlling TTY | `notify.terminal_bell` |
| Desktop | `notify-send` / `osascript` / Windows toast | `notify.desktop` |
| Webhook | POST JSON to URL | `notify.webhook.url` |
| Email/SMTP | `smtplib` with STARTTLS | `notify.email.*` |
| Stdout | Print to daemon stdout/journal | Always on |
| TUI | Badge/indicator in running TUI | Automatic if TUI attached |
| `Notification` hook | Custom hook receives structured event | `hooks.notification` |

### 5.2 Behaviour

| Aspect | Rule |
|--------|------|
| Egress | Notifications leaving the machine pass egress gateway (I-003, I-006) |
| Retry | 3 attempts with exponential backoff (30s, 60s, 120s) |
| Dedup/coalesce | Multiple failures within 5min → single summary notification |
| Quiet hours | Configurable window; queue notifications for delivery after |
| Failure isolation | Notification failure never fails the run itself |

### 5.3 Events That Notify

| Event | Default channels |
|-------|-----------------|
| Run completed (success) | Stdout only |
| Run failed | Desktop + webhook |
| Schedule auto-disabled | Desktop + webhook + email |
| Confirmation-class action hit | Desktop + webhook |
| Budget exceeded | Desktop + webhook |
| Review reminder | Desktop |

---

## 6. CLI

### 6.1 Commands

| Command | Description |
|---------|-------------|
| `kalash cron add` | Create a new schedule (interactive or flags) |
| `kalash cron ls` | List all schedules |
| `kalash cron show <name>` | Detail view of one schedule |
| `kalash cron rm <name>` | Delete schedule (requires `--confirm`) |
| `kalash cron enable <name>` | Re-enable a disabled schedule |
| `kalash cron disable <name>` | Disable without deleting |
| `kalash cron run <name>` | Execute immediately (ad-hoc, respects caps) |
| `kalash cron trigger <name>` | Fire event-based schedule manually |
| `kalash cron logs <name>` | Show recent run history |
| `kalash cron next [name]` | Show next fire times |

### 6.2 `kalash cron ls` Output

```
NAME              KIND     SPEC          NEXT FIRE (local)       STATUS    RUNS  FAILS
────────────────  ───────  ────────────  ──────────────────────  ────────  ────  ─────
daily-summary     cron     0 9 * * *     2025-07-16 09:00 IST    enabled     42      0
code-review       event    git:post-commit  (awaiting event)     enabled     17      1
weekly-cleanup    cron     0 3 * * 1     2025-07-21 03:00 IST    enabled      8      0
backup-check      interval 6h           2025-07-15 18:30 IST    enabled    120      2
one-time-migrate  once     2025-08-01T00:00Z  2025-08-01 05:30 IST  enabled   0      0
stale-watcher     cron     */15 * * * *  2025-07-15 12:45 IST    disabled    —      3
```

### 6.3 `kalash cron next` Output

```
SCHEDULE          NEXT FIRE (UTC)            LOCAL                   IN
────────────────  ─────────────────────────  ──────────────────────  ──────────
daily-summary     2025-07-16T03:30:00Z       2025-07-16 09:00 IST    20h 30m
backup-check      2025-07-15T13:00:00Z       2025-07-15 18:30 IST    5h 30m
weekly-cleanup    2025-07-20T21:30:00Z       2025-07-21 03:00 IST    5d 9h
```

---

## 7. Operational Concerns

| Concern | Handling |
|---------|----------|
| Daemon crash | Leases expire; next `kalash serve` start picks up overdue schedules |
| Clock skew | Monotonic clock for heartbeats; UTC comparison for fire times; NTP drift ≤1s acceptable |
| Concurrent daemons | Lease uniqueness prevents double-fire; second daemon waits |
| Stale schedules | `enabled=0` after `max_consecutive_failures`; manual re-enable required |
| Orphaned runs | Runs with expired leases and no heartbeat → marked `KALASH_SCHEDULER_LEASE_LOST` |

---

*See also:* [`state-machines.md`](./state-machines.md) §8 (schedule execution state machine), [`permissions.md`](./permissions.md) §3 (capability attenuation), [`security.md`](./security.md) (egress gateway I-003/I-006), [`data-model.md`](./data-model.md) §2.5 (schema), [`platform.md`](./platform.md) §5 (DST handling).
