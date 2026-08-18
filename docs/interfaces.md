# Interfaces

> **Status:** normative.
>
> Kalash has four surfaces: the Textual TUI, the headless CLI (`kalash -p`), the Python SDK, and the
> optional HTTP server (`kalash serve`). This document is their contract — layout and interaction for
> the TUI, machine-readable schemas and exit codes for the CLI, the public/private boundary and
> stability policy for the SDK, and the security posture of server mode.
>
> §1 comes first because it scopes everything after it: **Kalash is a single-user, local-first
> application.** That decision removes authentication, authorization, tenancy, and quota design from
> every surface below.
>
> Inherited constraints: I-011 (sandbox fails closed and is visible), I-026 (runs bounded), I-029
> (cancellation real), I-032 (unattended runs stop at confirmation), I-033 (external content not
> authoritative), I-037 (sessions explainable). Cross-references
> [`permissions.md`](./permissions.md) §5 for what an approval prompt must contain and
> [`state-machines.md`](./state-machines.md) §5 for interrupt semantics.

---

## 1. The multi-user decision

`Scope` in the memory layer carries `user_id` and `org_id` ([`memory.md`](./memory.md) §4). Read cold,
that vocabulary suggests multi-tenancy; the rest of the system has none. The ambiguity is resolved
here, once, in one direction.

**Kalash is a single-user, local-first application.** One human, whose SQLite database lives at
`~/.kalash/kalash.db`. Every process runs with that human's OS credentials, and the OS user *is* the
security principal.

### 1.1 What `user_id` and `org_id` actually are

| Field | What it is | What it is **not** |
|---|---|---|
| `user_id` | A namespace label so one human's memory can be partitioned across projects and machines, and so adapters can map onto provider tenancy vocabularies (mem0's `user_id`, Supermemory's container tags, Zep's user objects) | An authenticated identity. Nothing verifies it. Setting it to someone else's value is a configuration mistake, not a privilege escalation, because there is no privilege to escalate |
| `org_id` | Reserved for addressing a **shared team memory namespace hosted by an external provider**. The provider performs the tenancy; Kalash passes the identifier through | A Kalash-side tenancy boundary. Kalash does not isolate orgs, does not authenticate org membership, and does not enforce org policy |

Concretely: if a team shares a mem0 organization, mem0 decides who may read it. Kalash is a client
holding a credential. Scope isolation inside Kalash (I-017) protects *project against project* on one
machine — it is not, and must not be described as, a tenancy control.

### 1.2 `kalash serve` is explicitly not multi-tenant

Server mode exists so that an editor extension, a local script, or the scheduler daemon can drive
Kalash over HTTP on the same machine. It is not a hosted product.

Ruled out, by decision:

- **No user accounts.** One bearer token, one principal. The token authenticates *a client*, not a person.
- **No authorization model.** Any authenticated client can do anything the local user can do.
- **No per-tenant isolation.** One database, one blob store, one memory namespace set.
- **No quotas or per-caller resource limits.** Budget ceilings (I-026) bound *runs*, not callers.
- **No admin roles**, no role hierarchy, no delegation.
- **No audit separation.** One audit log for the one user.

### 1.3 What multi-user would require, and why it is out of scope

Reversing this decision would require all of the following, and none of them is a small addition:

| Would be required | Why it is a subsystem, not a flag |
|---|---|
| Authentication | Credential storage, rotation, session management, a login surface |
| Authorization | A policy model layered over the whole capability vocabulary in [`capabilities.md`](./capabilities.md) |
| Tenant isolation | Partitioning across sessions, blobs, memory, checkpoints, audit, scheduler — every table |
| Project ownership | Currently "the workspace on disk"; would need ownership and sharing semantics |
| Quotas, resource limits | Per-tenant token, cost, CPU, disk, and concurrency accounting |
| Audit separation | Per-tenant audit views plus cross-tenant leakage tests |
| Encryption at rest | Currently delegated to the OS and full-disk encryption; shared storage changes that |
| Admin roles | An administrative surface, which is itself a privilege-escalation target |
| User deletion | Cascading deletion across every table and every external memory provider (I-018) |

Together they are a different product. They are out of scope, and
[`threat-model.md`](./threat-model.md) §6 lists multi-tenant isolation as an explicit non-goal for the
same reason.

The payoff of saying so plainly: the sandbox (`permissions.md` §6) and the egress gateway
(`security.md` §1) can assume one trusted principal, so they defend the *user* against untrusted
**content** rather than defending users against each other. Those are different designs and trying to
be both produces neither.

---

## 2. TUI contract (Textual)

### 2.1 Layout regions

```
┌─ header ─────────────────────────────────────────────────────────────────┐
│ kalash  acme-api  sonnet-4-5   ⛨ workspace-write · landlock · net off   │  always visible
├─ transcript ─────────────────────────────────────────────────────────────┤
│  scrollable, virtualized, focus-follows-keyboard                         │
├─ pending ────────────────────────────────────────────────────────────────┤  present only when non-empty
│  approval prompts · long-running tool progress                           │
├─ input ──────────────────────────────────────────────────────────────────┤
│  multiline, grows to 8 rows then scrolls                                 │
├─ status ─────────────────────────────────────────────────────────────────┤
│ ~118k/200k (59%) L1 · $0.42/$2.00 · 12 turns · esc cancel · esc esc end  │
└──────────────────────────────────────────────────────────────────────────┘
```

The header and status bars are never scrolled off, never collapsed, and never hidden by a modal. The
security posture (§2.9) and the cancellation affordance (§2.7) must be readable at all times, which
means they cannot live in the transcript.

### 2.2 Input behaviour

| Concern | Behaviour |
|---|---|
| Submit | `Enter` |
| Newline | `Shift+Enter` or `Alt+Enter` where the terminal distinguishes them; `Ctrl+J` **always** works and is the documented fallback, because a large minority of terminals cannot report modified `Enter` |
| Multiline | Input grows to 8 rows, then scrolls internally. No hidden text |
| Paste | Bracketed paste. **A paste containing newlines never submits** — a pasted stack trace must not fire the turn. Pastes over 4 KiB collapse to a `[pasted 214 lines, 8.1 KiB]` chip that expands in place |
| Paste of an image | Attached when the model has `vision`; otherwise refused with a one-line reason naming the model (`model-gateway.md` §4.3), never silently dropped |
| History | `Up`/`Down` at the first/last line walks per-project prompt history; `Ctrl+R` reverse-searches it. History is per project, stored locally, and excluded from egress |
| Draft persistence | Unsent input survives a session switch and a crash. Losing a half-written prompt is a small, avoidable, memorable annoyance |

### 2.3 Streaming output

| Concern | Behaviour and why |
|---|---|
| Repaint rate | Deltas append to a live region, coalesced to **≤ 30 fps** (4 fps under `--low-bandwidth`). Uncoalesced repaints on a fast stream starve the event loop and make `Esc` feel unresponsive, undermining I-029 in perception even where it holds in fact |
| Code fences | Rendered as plain text until the closing fence arrives, then re-rendered highlighted. Highlighting a partial fence guesses the language and flickers when the guess changes |
| Thinking blocks | Collapsed behind a `thinking · 1.2k tokens` chip, expandable. Redacted thinking shows the chip marked `redacted` with no body |
| Auto-scroll | Pinned to the bottom until the user scrolls up, then unpinned with a persistent `↓ jump to latest`. Yanking the viewport back while someone is reading is worse than a missed line |
| Retried streams | **Marked, not erased.** When a partial is discarded and re-requested (`model-gateway.md` §5.3), the text the user already saw is dimmed and labelled `discarded — retried on <model>`, and the new stream renders below. Silently deleting text a human read makes the tool look like it is hiding something |

### 2.4 Tool call display

Collapsed by default, one line per call:

```
▸ edit    src/auth/middleware.ts            +12 −3       1.2s
▸ shell   pytest tests/unit -q              exit 0      14.8s
▸ read    src/auth/session.ts               2.1 KiB      0.1s   [truncated]
```

Expanded (`Ctrl+O` on the focused call, or click) shows: the full post-normalization argv or the
**complete unified diff** for a write, resolved absolute paths, the capability that authorized it and
its source (`sandbox_default`, `grant:session:<id>`, `just_asked`), `side_effect`, timings per
lifecycle stage, and the truncation marker verbatim when output was capped
([`tools.md`](./tools.md) §4).

Errors auto-expand. A failed call the user has to click to understand is a failed call the user
misses. Calls running longer than 2s show an inline elapsed counter in the pending region with the
cancel hint attached.

### 2.5 Approval prompts

Rendered in the pending region from [`permissions.md`](./permissions.md) §5.1. **Every applicable
field is mandatory and none may be summarized.**

```
┌─ approval required ─────────────────────────── waiting 8s ─┐
│ ACTION        Force-push branch `feat/auth` to origin      │
│ COMMAND       git push --force-with-lease origin feat/auth │
│ PATHS         /home/shivam/acme-api/.git  (arg `.` → ⚠)    │
│ CAPABILITIES  git.remote.push.force  ← --force-with-lease  │
│ NETWORK       github.com → 140.82.121.4:443  (git push)    │
│ RISK          write-remote                                 │
│ REVERSIBLE    no — remote history rewritten                │
│               checkpoint ckpt_01JQ7X taken before this     │
│ ─ diff / command detail ── 1 of 1 ── scrollable, in full ─ │
│ GRANTS        s → session · A → always, this project       │
│               pattern: git.remote.push.force on            │
│                        origin/feat/auth                    │
│ [a] once [s] session [A] always [d] deny [D] deny+why      │
│ [m] modify                            [esc] cancel call    │
└────────────────────────────────────────────────────────────┘
```

Rendering rules:

| Rule | Why |
|---|---|
| The detail pane is **internally scrollable**, never elided | A 400-line diff must be inspectable. `permissions.md` §5.1 forbids showing a summary of a diff |
| Resolved paths shown alongside the argument as written whenever they differ, with the divergence flagged | Divergence is a symlink or traversal signal (I-009). Hiding it hides the attack |
| Network destinations show the **resolved IP** | Hostname-only display hides SSRF (I-015) |
| Grant scope shown per option, in the exact pattern form it would be stored | Prevents choosing `always` believing it covers one file when it covers a tree |
| The panel cannot be dismissed into nothing | `Esc` is a **decision** — it cancels the tool call — and is labelled that way, not as "close" |
| Elapsed wait time displayed and updated | The agent waits indefinitely by design (`permissions.md` §5.3); the user must be able to see how long |
| No model stream is open while pending | Waiting must not cost money or race the idle timeout (`model-gateway.md` §6) |
| Batch: one panel per item, each individually approvable and rejectable, ordered by risk with destructive last | `permissions.md` §5.4 |
| The bulk-affirmative key is **disabled until every item has been scrolled into view** | "Offered only after full itemization" enforced mechanically rather than trusted |

### 2.6 Errors and progress

**Errors:** one line of what happened, the stable `KALASH_*` code, and the remediation line when one
is knowable ([`tools.md`](./tools.md) §1.2). A `details` expander carries the attempt chain, request
digest, and turn ID. Tracebacks go to the log file with a pointer (`kalash logs --turn <id>`) and
never into the transcript — a traceback in the UI is noise to the user and a leak surface for paths
and arguments.

**Progress:** a spinner appears only where the wait is genuinely indeterminate (a model request before
first byte, a tool with no progress signal) and always alongside elapsed time, because a spinner with
no number cannot be distinguished from a hang. Operations with a known denominator — indexing N files,
summarizing k compaction windows — get a determinate bar with counts. Nothing animates for under
400 ms; a flash of spinner is worse than no spinner. Under `--reduced-motion` or `--low-bandwidth`
both degrade to static text refreshed at 1 Hz (§3.2).

### 2.7 Cancellation

Matches [`state-machines.md`](./state-machines.md) §5 and `permissions.md` §5.3 exactly, so the
gesture means the same thing whether a prompt is open or a tool is running.

| Gesture | Effect |
|---|---|
| `Esc` or `Ctrl+C`, once | Cancels the **in-flight tool call** (or closes a pending approval as cancelled). The turn continues and the model may adapt |
| Second within 2s | **Ends the turn.** `INTERRUPTED`, cancellation propagates through the task group (I-029) |
| Either, while idle | Nothing destructive. A hint: `nothing running — Ctrl+D or /exit to leave` |
| `Ctrl+D` on empty input | Exit, `SessionEnd` hooks, lease released |

Two intents need two gestures. The status bar carries the affordance whenever a turn is active, so it
never has to be remembered.

### 2.8 Navigation, discovery, and scrollback

| Surface | Binding | Behaviour |
|---|---|---|
| Command palette | `Ctrl+P` | Fuzzy over slash commands, sessions, config keys, skills, agents. Keyboard-only, no mouse path required |
| Slash commands | `/` at column 0 | Autocomplete listing name, one-line description, and source (built-in · plugin · project). `Tab` accepts |
| File paths | `@` | Autocomplete over the workspace, honouring `.kalashignore` and **never listing a path outside the read roots** — offering a path the agent cannot read is both a small disclosure and a large confusion |
| Session switcher | `Ctrl+S` | Lists id, title, project, last activity (UTC), state including `ORPHANED`. Switching commits the current session (append-only, nothing is lost) and loads the other. **Refused while a turn is `TOOL_EXECUTING`**, with the reason — switching would imply cancellation, and that should be an explicit gesture |
| Scrollback | `PgUp`/`PgDn`, `Home`/`End` | Render buffer bounded at 10,000 rendered lines; older turns re-render lazily from SQLite. The transcript is durable, the widget is a window over it |
| Redraw | `Ctrl+L` | For terminals that get corrupted by a background process writing to the tty |

### 2.9 Security posture indicator

Always visible in the header, per [`permissions.md`](./permissions.md) §6.2:

```
⛨ workspace-write · landlock · net off        normal
⛨ workspace-write · bubblewrap · net off      degraded backend, named
⚠ danger-full-access · no sandbox · net ON    inverse video, persists for the session
```

`danger-full-access` renders in inverse video with a text label, not colour alone, and cannot be
dismissed. Threat T-J is "set once and forgotten"; a posture indicator that can be hidden does not
mitigate it. The token/cost/context display shares the status bar:
`~118k/200k (59%) L1 · $0.42/$2.00 · 12 turns`, where `~` marks an estimated token count
([`context-budget.md`](./context-budget.md) §4.1) and `L1` is the current pressure-ladder step. Amber
at the budget warning threshold, inverse at the soft limit.

Pending approvals remain visible even when the transcript is scrolled: a persistent
`1 approval waiting (8s)` marker in the status bar, because an agent silently blocked on a prompt the
user scrolled past is indistinguishable from a hang.

### 2.10 Terminals, SSH, and capability detection

| Concern | Behaviour |
|---|---|
| Colour depth | `COLORTERM=truecolor` → 24-bit; `TERM` containing `256color` → 256; otherwise 16; monochrome per §3.2; `TERM=dumb` → refuse the TUI and point at `-p` |
| SSH | The normal case, not an edge case. Repaints coalesced, no per-frame full redraws, no reliance on terminal queries that may not answer |
| Low bandwidth | `--low-bandwidth` / `KALASH_LOW_BANDWIDTH=1`: 4 fps, no spinners, no mid-stream highlighting, ASCII box drawing. **Not auto-detected** — bandwidth is not observable, and a wrong guess degrades everyone |
| Mouse | Optional and never required. Every mouse affordance has a keyboard equivalent, asserted by test (§8) |
| Resize | Reflow on resize. Below 60 columns: narrow layout, unified diffs without gutters. Below 40 columns or 12 rows: refuse the TUI with the `-p` suggestion rather than render something unusable |

---

## 3. Accessibility

### 3.1 Screen readers — the hard part, stated honestly

A streaming transcript is hostile to screen readers by default: re-render the region and the reader
re-announces the whole buffer; announce raw deltas and it announces token fragments. The approach:

- A dedicated **announcement region** receives *completed semantic units* — a finished sentence, a
  closed code block, a tool-result summary, a state change — and is **replaced** on each update,
  never appended to, so the reader announces the replacement only. Raw token deltas are never
  announced; streaming text is readable in the transcript, not narrated character by character.
- `--announce=off|units|verbose`. `units` is the default under screen-reader mode; `verbose` adds
  tool lifecycle transitions.
- Screen-reader mode is **explicit**: `--screen-reader`, `KALASH_SCREEN_READER=1`, or config. Not
  auto-detected — there is no reliable cross-platform detection and both error directions are bad. A
  false positive degrades rendering for everyone; a false negative silently excludes.

Automatable: the *content* of the announcement region. Tests assert each unit is announced exactly
once, in transcript order, with no repeats, and that a retried stream announces the retry rather than
re-announcing discarded text.

Not automatable: whether NVDA, JAWS, VoiceOver, or Orca actually reads it usefully. Textual's
screen-reader support is limited and platform-dependent, and this design has not been validated
against real assistive technology. **Full WCAG-style validation requires manual testing with real
assistive technology and expert accessibility review.** Until that has happened no conformance is
claimed — only the specific, tested behaviours listed here.

### 3.2 The rest

| Concern | Behaviour | Automatable? |
|---|---|---|
| `NO_COLOR` / `--no-color` | Honoured for any value of `NO_COLOR`. Every state that was colour-coded also carries a text label or symbol, because colour was never permitted to be the only channel | Yes — snapshot the mono render and assert no state is colour-only |
| High contrast | `--theme=high-contrast`, targeting ≥ 7:1 on text and ≥ 3:1 on non-text indicators | Yes — contrast ratios computed from the theme palette in CI |
| Keyboard-only | Every action reachable without a mouse; no mouse-only handlers | Yes — assert no widget exposes a mouse-only action |
| Unicode fallback | `--ascii` / `KALASH_ASCII=1`, and automatic when the terminal encoding is not UTF-8: box drawing degrades to plus, hyphen, and vertical bar; `▸` → `>`; the spinner becomes a rotating slash; `⛨` → `[S]` | Yes — snapshot the ASCII render |
| Reduced motion | `--reduced-motion` / `KALASH_REDUCED_MOTION=1`: spinners, progress animation, and the streaming cursor replaced by static text refreshed at 1 Hz | Yes |
| Error readability | Text-first. Never colour-only, never icon-only. The `KALASH_*` code is always present so it is searchable | Yes |
| Non-interactive fallback | See §3.3. A first-class mode, not a consolation prize | Yes |

### 3.3 Non-TTY behaviour

| stdin | stdout | Behaviour |
|---|---|---|
| TTY | TTY | Full TUI |
| pipe | TTY | Read the prompt from stdin, run headless, render results to the terminal |
| TTY | pipe | Headless. `--output-format` respected; no TUI, no ANSI |
| pipe | pipe | Headless. The CI case |
| — | not a TTY, `kalash` with no `-p` | **Refuse** with a one-line explanation and the `-p` form. Starting a TUI into a pipe produces escape-sequence garbage that looks like a crash |

`kalash -p --output-format text` is the accessibility escape hatch that always works: linear output,
no live regions, no cursor addressing, no colour. It is documented as a supported way to use Kalash,
not a degraded mode.

---

## 4. CLI output contract

`kalash -p` is the CI and scripting interface. Its contract carries the same weight as the TUI's.

### 4.1 Rules

| Rule | Detail |
|---|---|
| `--output-format` | `text` (default, human) · `json` (one object on completion) · `stream-json` (NDJSON events) |
| **stdout is data, stderr is diagnostics** | Progress, warnings, and log lines go to stderr, always. `kalash -p "…" --output-format json \| jq` must work unconditionally. A single diagnostic byte on stdout breaks every consumer |
| Schema versioning | Every object carries `schema_version` (`MAJOR.MINOR`). **Adding a field is MINOR. Removing or renaming a field, or changing a field's type or meaning, is MAJOR.** Consumers must ignore unknown fields and unknown event types |
| stdin | Accepted as prompt input when stdin is not a TTY. `--` ends flag parsing |
| Timestamps | RFC 3339, UTC, `Z`-suffixed (I-038) |
| Money | Decimal **strings**, never JSON numbers (`"cost_usd": "0.4213"`). A float loses cents at scale and `Decimal` is the internal type ([`model-gateway.md`](./model-gateway.md) §8.2). `null` means pricing is unknown, never zero |
| Secrets | Never present. Referenced by key name only (I-036) |
| Exit code | Always set from §4.9, including when `--output-format json` also reports the error in the body |

Comments in the examples below are annotation only. **The wire format is strict JSON with no
comments.**

### 4.2 Success result — `--output-format json`

```jsonc
{
  "schema_version": "1.0", "ok": true,
  "session_id": "ses_01JQ7XW3N4K5Z8ABCDEF",    // ULID; resumable with `kalash resume`
  "turn_id": "trn_01JQ7XW9QK…", "run_id": "run_01JQ7XWB8R…",
  "result": "Added session-fixation tests to tests/unit/test_auth.py.",
  "stop_reason": "end_turn",                   // normalized, model-gateway.md §3.1
  "model": {"requested": "anthropic/claude-sonnet-4-5",
            "used": "anthropic/claude-sonnet-4-5",
            "fallback_chain": []},             // non-empty ⇒ a switch happened (I-037)
  "usage": {"input_tokens": 118420, "output_tokens": 2841,
            "cache_read_tokens": 96110, "cache_write_tokens": 0, "reasoning_tokens": 0,
            "source": "provider_reported",     // or "estimated" — never presented as exact
            "cost_usd": "0.4213",              // Decimal string, or null
            "pricing_version": "2026.02"},
  "context": {"window": 200000, "peak_input_tokens": 118420,
              "estimated": false, "ladder_step": "L1", "compactions": 0},
  "budget": {"tokens": {"used": 121261, "ceiling": 500000},
             "cost_usd": {"used": "0.4213", "ceiling": "2.00"},
             "turns": {"used": 3, "ceiling": 100},
             "wallclock_s": {"used": 41.8, "ceiling": 3600},
             "state": "ok"},                   // ok | warning | soft_limit | exceeded
  "files_changed": [{"path": "tests/unit/test_auth.py", "change": "modified",
                     "lines_added": 44, "lines_removed": 0, "digest": "sha256:9f2c…"}],
  "checkpoint_id": "ckpt_01JQ7XWD…",           // rewind target
  "started_at": "2026-02-14T09:12:04.118Z", "finished_at": "2026-02-14T09:12:45.902Z"
}
```

| Field | Type | Notes |
|---|---|---|
| `ok` | bool | `true` only on exit code 0 |
| `session_id`, `turn_id`, `run_id` | ULID | Correlate with every log line, audit row, and event |
| `stop_reason` | enum | Normalized `StopReason`, lowercased |
| `model.fallback_chain` | array | Entries `{model, error_class, reason}`. Non-empty ⇒ the model changed mid-run |
| `context.ladder_step` | enum | `L0`–`L6`, the highest pressure step reached |
| `files_changed[].digest` | string | Post-write digest — the same value I-012 checks |
| `checkpoint_id` | ULID \| null | `null` when nothing was written |

### 4.3 Error result

```jsonc
{
  "schema_version": "1.0",
  "ok": false,
  "session_id": "ses_01JQ7XW3N4K5Z8ABCDEF",
  "turn_id":    "trn_01JQ7XWQ…",
  "error": {
    "code": "KALASH_APPROVAL_REQUIRED",        // stable, greppable, never renumbered
    "class": "permission",                     // permission|budget|context|provider|sandbox|
                                               //   config|trust|tool|state|internal
    "message": "Approval required for git.remote.push.force; unattended runs stop (I-032).",
    "remediation": "Re-run interactively, or grant the capability with `kalash trust` / config.",
    "retryable": false,
    "invariant": "I-032",                      // when the refusal enforces one
    "details": {"capability": "git.remote.push.force", "target": "origin/feat/auth",
                "request_id": "prq_01JQ7XWR…"}
  },
  "partial_result": "Rebased 3 commits; stopped before pushing.",
  "usage": {"input_tokens": 41200, "output_tokens": 903, "cost_usd": "0.1418",
            "source": "provider_reported"},
  "finished_at": "2026-02-14T09:14:02.551Z"
}
```

Usage is always reported on failure. Tokens spent before an error were still billed, and an error
object that hides the cost makes spend impossible to reconcile. `partial_result` carries work already
done — a failure is not a reason to discard it.

### 4.4 Streaming envelope — `--output-format stream-json`

NDJSON: exactly one JSON object per line, flushed per event, on stdout. `seq` is monotonic from 0.
**Exactly one terminal event (`result` or `error`) is emitted and it is always last.**

```jsonc
{"schema_version":"1.0","seq":0,"ts":"2026-02-14T09:12:04.118Z",
 "event":"session.started","session_id":"ses_01JQ7X…","data":{"model":"anthropic/claude-sonnet-4-5","sandbox":"workspace-write"}}
{"schema_version":"1.0","seq":7,"ts":"2026-02-14T09:12:06.402Z",
 "event":"message.delta","session_id":"ses_01JQ7X…","turn_id":"trn_01JQ7X…",
 "data":{"block_index":0,"text":"I'll add the fixation test first."}}
```

| Envelope field | Type | Notes |
|---|---|---|
| `schema_version` | string | On every event, so a stream can be consumed without prior negotiation |
| `seq` | int | Monotonic from 0 per invocation. Gaps mean dropped output, which is a bug |
| `ts` | RFC 3339 UTC | |
| `event` | string | Dotted taxonomy below. **Unknown types must be ignored**, not treated as errors |
| `session_id` / `turn_id` / `run_id` | ULID | `run_id` distinguishes subagent events from the parent's |
| `data` | object | Event-specific. Never a bare string |

Taxonomy:

| Group | Events |
|---|---|
| Session · turn | `session.started` `session.ended` · `turn.started` `turn.state` (any state-machine transition, including `COMPACTING`) `turn.completed` |
| Content | `message.delta` · `thinking.delta` (omitted unless `--include-thinking`) |
| Tools · permission | `tool.requested` `tool.denied` `tool.started` `tool.output` `tool.completed` · `permission.requested` (§4.6) |
| Memory · context | `memory.recalled` (record IDs, providers, token cost — never record bodies) · `compaction.started` `compaction.completed` `context.pressure` (ladder step change) |
| Budget · model | `budget.warning` `budget.soft_limit` `budget.exceeded` · `model.fallback` (from, to, error class) `usage.updated` |
| Agents | `agent.spawned` `agent.completed` — envelope only; child transcripts never appear (I-028) |
| Terminal | `result` · `error` — exactly one, always last |

`thinking.delta` is opt-in because reasoning text is the content most likely to be logged verbatim by
a naive CI consumer, and the content least intended for it.

### 4.5 Tool execution event

```jsonc
{"schema_version":"1.0","seq":12,"ts":"2026-02-14T09:12:09.881Z",
 "event":"tool.completed","session_id":"ses_01JQ7X…","turn_id":"trn_01JQ7X…",
 "data":{"call_id":"tc_01JQ7XWF…",            // Kalash ULID, stable across a fallback
   "provider_call_id":"toolu_01A…",           // provider's, wire-only
   "name":"edit","side_effect":"write","capability":"filesystem.write",
   "capability_source":"sandbox_default:workspace-write",
   "args_digest":"sha256:71bd…",              // digest, not args — args can hold file content
   "paths":["/home/shivam/acme-api/tests/unit/test_auth.py"],   // resolved, absolute
   "ok":true,"is_error":false,"truncated":false,"output_bytes":312,"duration_ms":184,
   "lifecycle":{"validated_ms":1,"normalized_ms":2,"authorized_ms":1,
                "admitted_ms":1,"hooked_ms":6,"executing_ms":173}}}
```

`args_digest` rather than `args`: tool arguments routinely contain file bodies and command lines, and
a CI log is not a place to mirror them. Full arguments are available from the session
(`kalash session show --tool-calls`) where access is the local user's own.

### 4.6 Permission request event — what a script receives

A non-interactive run **stops and reports** (I-032). It never auto-approves, and no timeout makes
silence mean yes. The event carries everything an interactive prompt would have shown, so a human can
reproduce the decision.

```jsonc
{"schema_version":"1.0","seq":9,"ts":"2026-02-14T09:13:58.220Z",
 "event":"permission.requested","session_id":"ses_01JQ7X…","turn_id":"trn_01JQ7X…",
 "data":{"request_id":"prq_01JQ7XWR…",
   "outcome":"auto_denied",                   // I-032: unattended never reaches `asked`
   "code":"KALASH_APPROVAL_REQUIRED",
   "action":"Force-push branch `feat/auth` to origin",
   "command":["git","push","--force-with-lease","origin","feat/auth"],
   "paths":[{"as_written":".","resolved":"/home/shivam/acme-api/.git","diverged":true}],
   "capabilities":[{"capability":"git.remote.push.force","implied_by":"--force-with-lease"}],
   "network":[{"host":"github.com","ip":"140.82.121.4","port":443,"purpose":"git push"}],
   "risk_class":"write-remote","reversible":false,"checkpoint_id":"ckpt_01JQ7XWD…",
   "diff":null,                               // full unified diff for write actions
   "grant_options":[{"key":"s","lifetime":"session",
                     "pattern":"git.remote.push.force on origin/feat/auth"},
                    {"key":"A","lifetime":"always",
                     "pattern":"git.remote.push.force on origin/feat/auth"}],
   "rerun_hint":"kalash resume ses_01JQ7XW3N4K5Z8ABCDEF"}}
```

The run then emits `error` with `KALASH_APPROVAL_REQUIRED` and exits **3**, which is a distinct exit
code precisely so CI can branch on "needs a human" without parsing a message.

### 4.7 Session, memory, and scheduler results

```jsonc
// kalash session show <id> --output-format json
{"schema_version":"1.0","ok":true,
 "session":{"id":"ses_01JQ7X…","title":"auth fixation tests","project":"acme-api",
   "state":"idle","created_at":"2026-02-14T09:12:04.118Z","updated_at":"2026-02-14T09:45:11.004Z",
   "turns":14,"messages":96,"compactions":2,
   "usage":{"input_tokens":1841003,"output_tokens":24118,"cost_usd":"3.8841"},
   "usage_by_agent":[{"run_id":"run_01JQ…","agent":"test-writer","cost_usd":"0.9120"}],
   "models_used":["anthropic/claude-sonnet-4-5","openai/gpt-5.2"],
   "checkpoints":[{"id":"ckpt_01JQ…","seq":42,"files":3,"created_at":"…Z"}],
   "sandbox":"workspace-write","sandbox_backend":"landlock"}}

// kalash memory search "package manager" --output-format json
{"schema_version":"1.0","ok":true,
 "query":"package manager","providers_queried":["kme","mem0"],
 "providers_degraded":[{"provider":"mem0","reason":"timeout_1500ms"}],   // never silent (I-005)
 "hits":[{"id":"mem_01JQ7X…","provider":"kme","kind":"semantic","source":"user-stated",
          "trust":"high","confidence":0.95,"age_days":12,"fused_rank":1,
          "content":"Uses npm as the package manager for this repo.",
          "supersedes":["mem_01JP2K…"]}],
 "truncated":false}

// kalash cron ls --output-format json
{"schema_version":"1.0","ok":true,
 "schedules":[{"id":"sch_01JQ…","name":"nightly-deps","spec":"0 3 * * *","spec_kind":"cron",
   "timezone":"Asia/Kolkata","enabled":true,
   "next_run_at":"2026-02-14T21:30:00Z",         // stored UTC, computed in `timezone` (I-038)
   "sandbox_mode":"read-only","approval_policy":"never",
   "catchup_policy":"skip_missed","overlap_policy":"skip",
   "budget":{"max_cost_usd":"0.50","max_wallclock_s":900},
   "last_run":{"id":"shr_01JQ…","state":"succeeded",
     "started_at":"2026-02-13T21:30:00Z","duration_s":184,"cost_usd":"0.0821"},
   "consecutive_failures":0}]}
```

Provider similarity scores are deliberately absent from the memory result: they are not comparable
across providers ([`memory.md`](./memory.md) §5.3), so publishing them invites consumers to compare
them. `fused_rank` is the only ordering signal exposed.

### 4.8 Compatibility

| Change | Impact |
|---|---|
| New field on an existing object · new exit code from the reserved range | MINOR |
| New `event` type · new enum member | MINOR — consumers must ignore unknown types and tolerate unknown members |
| Removing or renaming a field | **MAJOR** |
| Changing a field's type or meaning, or an existing exit code's meaning | **MAJOR** |

The reserved-range rule is what lets exit codes grow without breaking scripts: a consumer that treats
"not 0 and not 3" as failure keeps working.

### 4.9 Exit codes

| Code | Name | When |
|---|---|---|
| `0` | OK | Completed |
| `1` | FAILURE | General runtime failure |
| `2` | USAGE | Bad flags or arguments. Nothing was executed |
| `3` | APPROVAL_REQUIRED | Stopped at a confirmation-class action (I-032) |
| `4` | BUDGET_EXCEEDED | A ceiling was reached (I-026) |
| `5` | CANCELLED | Interrupt handled cleanly (I-029) |
| `6` | CONTEXT_UNSATISFIABLE | Allocator floor reached ([`context-budget.md`](./context-budget.md) §3, §5.3) |
| `7` | SANDBOX_UNAVAILABLE | Sandbox failed closed (I-011) |
| `8` | CONFIG_INVALID | Config or credential resolution failed |
| `9` | TRUST_REQUIRED | Untrusted hook, skill, or plugin (I-031) |
| `10` | PROVIDER_UNAVAILABLE | Every model candidate failed |
| `11` | STATE_CONFLICT | Session closed, lease held, or stale read (I-012) |
| `12`–`63` | reserved | Never reused; new codes are additive |
| `≥ 64` | not used | Left to the shell. `130` appears when the shell reports `SIGINT` rather than Kalash exiting `5` itself |

---

## 5. CLI command surface

```
kalash                         interactive TUI
kalash -p "<prompt>"           headless; stdin accepted; --output-format text|json|stream-json
kalash resume [id]             re-attach, including ORPHANED sessions
kalash rewind [--to <seq>]     restore conversation and/or working tree

kalash init                    scaffold .kalash/, KALASH.md, settings, .kalashignore
kalash status                  this project + session: model, sandbox, budget, pending, posture
kalash doctor                  environment, sandbox backend, provider health, pricing gaps
kalash diff [--session <id>]   working-tree diff attributable to agent activity
kalash logs [--turn <id>]      structured log tail with filters
kalash trust    ls | grant | revoke | verify
kalash backup / restore        explicit DB + blob archive, and its inverse
kalash gc [--purge]            dereference blobs, prune sessions, vacuum
kalash version [--check]       version, build, protocol versions; --check reports availability only
kalash completions <shell>     bash | zsh | fish

kalash session  ls | show | export | rm
kalash memory   add | search | ls | forget | export | import | migrate | audit | doctor
kalash provider ls | test | bench
kalash agents   ls | run | new
kalash skills   ls | new | validate
kalash mcp      add | ls | test | serve | auth
kalash hooks    ls | test | trust
kalash plugin   install | ls | update | remove | marketplace
kalash cron     add | ls | rm | enable | disable | run | logs
kalash config   get | set | edit | show
kalash serve    scheduler daemon + optional HTTP API
```

### 5.1 Decisions on the contested commands

| Command | Decision | Reason |
|---|---|---|
| `init` | **Include** | Scaffolding is otherwise copy-paste from docs. It writes only files it creates, and it **does not grant trust** to pre-existing hooks, skills, or plugins it finds — creating a project is not the same as trusting one (I-031) |
| `status` | **Include** | Non-overlapping with `doctor`: `doctor` diagnoses the *installation*, `status` reports the current *project and session* — model, sandbox posture, budget spend, pending approvals, orphaned sessions |
| `run` | **Omit** | Redundant with `-p` and `agents run`. Two spellings of one operation diverge in flags and then in behaviour |
| `exec` | **Omit** | Same reason, and the name invites the reading "run a shell command", which is a tool, not a CLI verb |
| `diff` | **Include** | Distinct from `git diff`: scopes to what the agent touched, using the checkpoint manifest. Answers "what did it change" without staging noise |
| `logs` | **Include** | `structlog` already writes JSON to file. Without a reader, that file is a grep exercise |
| `backup` / `restore` | **Include** | I-022 requires a pre-migration backup; users need to take one deliberately and restore it deliberately. The DB is the user's entire history with the tool |
| `gc` | **Include** | Blob dereferencing, session pruning, and `VACUUM` are real maintenance. `--purge` removes all Kalash data, which is the part of "uninstall" we actually own |
| `update` | **Omit — delegate** | The install method decides. `kalash version --check` reports that a newer version exists and prints the exact command for the detected install (`uv tool upgrade kalash-code`, `pip install -U kalash-code`). A self-updating binary is a supply-chain surface (T-D, T-G) needing signature verification and a rollback path we would rather not own, to replace one command the user already has |
| `uninstall` | **Omit — delegate** | Same reason. `kalash gc --purge` handles data removal and prints the package-manager command for the binary |
| `version` | **Include** | Also prints protocol versions (§6.2), which is what a plugin author actually needs |
| `completions` | **Include** | Generated by `typer`. Cheap, and a large command surface is unusable without it |
| `trust` | **Include** | Trust is otherwise mutable only through interactive prompts. A user must be able to list what they have trusted, revoke it, and verify that hashes still match (I-031) |

---

## 6. Python SDK

### 6.1 The public boundary

**`kalash.sdk` is the only public module.** Everything else — `kalash.core`, `kalash.runtime`,
`kalash.memory`, `kalash.models`, `kalash.tools`, `kalash.storage`, `kalash.cli`, `kalash.tui` — is
private, and importing it is unsupported: it may change or disappear in a patch release with no
deprecation cycle.

The boundary is enforced, not documented. `kalash.sdk.__all__` is explicit, private modules are not
re-exported, and a test asserts that no name reachable from `kalash.sdk` exposes a private module
object (§8). Extension authors use the protocol seams (`kalash.plugins` entry points), versioned
separately in §6.2.

```python
import asyncio
from decimal import Decimal
from kalash.sdk import Kalash, RunOptions, SandboxMode

async def main() -> None:
    async with Kalash.open(project=".") as k:        # opens the local DB, resolves config
        session = await k.sessions.create(title="triage")
        run = await session.run(
            "Find why test_login is flaky and propose a fix.",
            options=RunOptions(sandbox=SandboxMode.READ_ONLY,
                               max_cost=Decimal("0.25"),   # Decimal, never float
                               max_turns=10))
        async for event in run.events():             # same taxonomy as stream-json §4.4
            if event.type == "message.delta":
                print(event.data.text, end="", flush=True)
        result = await run.result()
        print(f"\n{result.stop_reason}  {result.usage.cost_usd}")

asyncio.run(main())
```

`Kalash.open` is an async context manager because it owns a database handle, a writer queue, and
provider clients. No module-level singleton, no implicit global state — an embedding host may hold
several instances against different projects.

### 6.2 Versioning and stability

The distribution follows semantic versioning. The protocols that third parties build against are
versioned **independently**, because a change to the plugin API should not force a major bump on the
memory provider protocol, and vice versa.

| Protocol | Current | Governs |
|---|---|---|
| `SDK_API_VERSION` | `1.0` | `kalash.sdk` public surface |
| `PLUGIN_API_VERSION` | `1.0` | Plugin manifest, entry points, capability declaration |
| `MEMORY_PROVIDER_PROTOCOL` | `1.0` | `MemoryProvider`, `Scope`, record types, `ProviderCapability` |
| `MODEL_PROVIDER_PROTOCOL` | `1.0` | `ModelProvider`, `NormalizedRequest`, `StreamEvent`, `ModelCapabilities` |
| `TOOL_PROTOCOL` | `1.0` | `Tool`, schema generation, `ToolEnvelope` |
| `HOOK_PAYLOAD_SCHEMA` | `1.0` | Hook event payloads and the exit-code contract |
| `CLI_SCHEMA_VERSION` | `1.0` | §4 JSON objects and event envelope |

All seven are reported by `kalash version --output-format json` and checked at load: a plugin
declaring an incompatible major refuses to load with a message naming both versions rather than
failing later inside a call. The cross-release compatibility matrix lives in
[`operations.md`](./operations.md).

**Deprecation policy.** A deprecated public API emits a `DeprecationWarning` naming its replacement,
keeps working for **at least two minor releases**, and appears in the changelog under a stable
`Deprecated` heading. Removal happens only in a major release, and a protocol version bump follows the
same rule for its own number. Nothing public is removed without a prior release that warned about it.

---

## 7. HTTP server (`kalash serve`)

**This is a remote code execution surface.** An authenticated caller can run shell commands and write
files as the local user. That is the feature. Every default below exists because of it.

### 7.1 Binding and authentication

| Default | Behaviour |
|---|---|
| Bind | `127.0.0.1` only |
| Auth | Bearer token **required**, always. Generated on first start, stored in the OS keyring, printed once. No anonymous mode, not even on loopback — any local process would otherwise reach it |
| Non-loopback bind | Refused unless **both** an explicit `--bind <addr> --i-understand-this-is-rce` flag **and** a configured token are present. One without the other is refused, not warned about |
| TLS | Not terminated in-process. Guidance: run behind a reverse proxy that owns certificates. We would rather not own cert rotation, and loopback traffic does not need it |
| CORS | **Deny by default.** No origins allowed. A browser page must not be able to reach a local RCE endpoint. Allowed origins are explicit, exact, and never wildcards |
| Max request body | 8 MiB, then `413`. Streaming bodies are not accepted |
| Rate limit | Token bucket per token, default 60 req/min with a burst of 20, `429` with `Retry-After`. Not a security control — a stop on runaway clients |
| Request IDs | `X-Request-Id` echoed, or a ULID generated. Present on every event, log line, and audit row for that request |

### 7.2 Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness, version, protocol versions. The only unauthenticated endpoint, and it reveals nothing beyond version |
| `GET` `POST` | `/v1/sessions` | List, create |
| `GET` `DELETE` | `/v1/sessions/{id}` | Show, delete |
| `POST` | `/v1/sessions/{id}/turns` | Submit a prompt; returns a turn ID immediately |
| `GET` | `/v1/sessions/{id}/events` | **SSE** stream, the §4.4 event taxonomy verbatim |
| `POST` | `/v1/sessions/{id}/interrupt` | Cancellation (I-029): one call cancels the tool, two the turn |
| `GET` `POST` | `/v1/permissions/{request_id}` | Read a pending request, submit a decision (§7.4) |
| `GET` `POST` `DELETE` | `/v1/memory` | Search, add, forget. Router-mediated only |
| `GET` `POST` `PATCH` `DELETE` | `/v1/schedules` | Scheduler CRUD; the daemon is in this process |
| `GET` | `/v1/usage` | Session and run usage rollups |

### 7.3 Streaming: SSE, not WebSocket

The need is unidirectional server-to-client streaming plus a low-frequency client-to-server channel.
SSE is exactly that shape, and wins on four specifics:

- **One protocol.** Ordinary HTTP responses — no upgrade handshake, no frame types, no ping/pong
  liveness lifecycle to get wrong.
- **Auth stays in a header.** Browsers cannot set headers on a WebSocket handshake, which pushes
  tokens into query strings or subprotocol fields, where they land in proxy logs. A bearer token in an
  `Authorization` header on a `GET` is the boring, correct thing.
- **Reconnection maps onto the model.** `Last-Event-ID` *is* the `seq` from §4.4, so a dropped
  connection resumes at the next event with no session-affinity machinery.
- **Proxies and `curl -N` handle it.** Which matters more than it sounds when someone is diagnosing a
  stalled turn.

WebSocket would add a second transport, a second auth path, and bidirectional framing for a direction
carrying a handful of small requests per turn.

### 7.4 Interactivity and I-032

Server mode is **interactive only while a client is attached to the event stream and has declared it
can render approvals** (`X-Kalash-Client-Capabilities: approvals`). Under those conditions a
`permission.requested` event is delivered and the run waits for a decision on
`/v1/permissions/{id}`, exactly as the TUI waits.

With no such client attached, unattended rules apply in full: the run stops and reports, I-032 holds,
and no timeout ever produces an approval. Declaring the capability and then not answering is
indistinguishable from a hang, so the request is visible in `GET /v1/permissions` and in
`kalash status`.

### 7.5 Deliberately absent

Multi-user support and tenancy (§1). An admin API. Remote shutdown or restart — a caller who can stop
the daemon can also disable the scheduler, and that belongs to whoever controls the machine. Remote
configuration writes: an endpoint that could set `permissions.sandbox = "danger-full-access"` would
turn a bounded RCE surface into an unbounded one, so security-relevant configuration stays user-scope
file and environment only ([`permissions.md`](./permissions.md) §8). Plugin installation. Arbitrary
SQL.

---

## 8. Testing

| Concern | Approach |
|---|---|
| TUI flows | Textual `Pilot` scripted sessions: submit, stream, expand a tool call, approve, deny with reason, modify, cancel once, cancel twice, switch session, resize |
| Approval rendering | Assert **every** `permissions.md` §5.1 field is present for each risk class; assert a 400-line diff is scrollable and never summarized; assert the bulk-affirmative key stays disabled until every batch item has been scrolled into view |
| Cancellation semantics | Assert one `Esc` cancels the tool and two end the turn, in both the streaming and pending-approval states; assert bounded acknowledgement (I-029) |
| Posture indicator | Assert visible at every layout width and never obscured; assert `danger-full-access` renders with a text label under `--no-color` |
| JSON schemas | `syrupy` snapshots of every §4 object; assert `schema_version` present; assert added fields are MINOR and removals fail the snapshot review |
| stdout purity | Assert nothing but NDJSON/JSON reaches stdout for every command and every failure path; pipe through `jq` in CI |
| Exit codes | One test per code in §4.9, each triggering the real condition — not a mocked exit |
| Rendering matrix | `NO_COLOR`, `--ascii`, `--reduced-motion`, `--theme=high-contrast`, 40/60/80/200 columns, 12/24 rows, every non-TTY stdin/stdout combination |
| Screen reader | Assert each semantic unit reaches the announcement region exactly once, in transcript order, with no re-announcement of prior content and an explicit retry announcement |
| Contrast · keyboard | Contrast ratios computed from every theme palette (≥ 7:1 text, ≥ 3:1 non-text); assert no widget exposes a mouse-only action |
| SDK boundary | Import `kalash.sdk` and assert no private module is reachable through any exported name; assert `__all__` matches the documented surface and that protocol version constants are exported |
| Deprecation | Assert a deprecated symbol emits `DeprecationWarning` and still works, and that the changelog entry exists |
| HTTP auth | Assert every endpoint except `/health` refuses an unauthenticated request with `401`; assert a non-loopback bind without **both** the flag and a token refuses to start; assert CORS denies by default |
| HTTP streaming | Assert SSE reconnect with `Last-Event-ID` resumes at the next `seq` with no duplicates and no gaps |
| Server unattended | With no approvals-capable client attached, assert a confirmation-class action stops and reports (I-032) and never waits for an approval that cannot arrive |

---

*See also: [`permissions.md`](./permissions.md) §5 for the approval fields the TUI renders and §6.2
for the posture indicator; [`state-machines.md`](./state-machines.md) §5 for interrupt and no-answer
semantics; [`context-budget.md`](./context-budget.md) for the budget and context figures shown in the
status bar and the JSON `budget` object; [`memory.md`](./memory.md) §4 for `Scope`;
[`capabilities.md`](./capabilities.md) for the capability vocabulary in permission events;
[`threat-model.md`](./threat-model.md) §6 for multi-tenant isolation as an explicit non-goal;
[`operations.md`](./operations.md) for the release compatibility matrix;
[`invariants.md`](./invariants.md) I-011, I-012, I-026, I-028, I-029, I-031, I-032, I-036, I-038.*
