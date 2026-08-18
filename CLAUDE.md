  # CLAUDE.md — Kalash Code

  > **Scope of this file.** This is the *contributor* guide: how to build, extend, and reason about
  > the Kalash Code codebase. It is for any coding agent (Claude Code, Codex, Kiro, Kalash itself) or
  > human working *on* this repo.
  >
  > The sibling file [`AGENT.md`](./AGENT.md) is the *runtime contract*: how the Kalash agent behaves
  > at inference time — its loop, tool semantics, memory rules, and permission protocol. When you
  > change agent behaviour, update `AGENT.md`. When you change repo structure or conventions, update
  > this file. Do not duplicate content between them; cross-reference instead.

  ---

  ## 1. What we are building

  **Kalash Code** is a terminal-native, fully agentic coding assistant written in Python. Feature
  parity target: Claude Code and OpenAI Codex CLI — read/write code, run commands, plan, delegate,
  schedule, and extend.

  CLI entry point: `kalash`. Distribution: `pip install kalash-code` / `uv tool install kalash-code`.

  ### The thesis

  Every coding agent today hardcodes its memory. Kalash Code treats **memory as infrastructure**: a
  separate, swappable, *multi-provider* layer. A user can run entirely on the built-in local store, or
  plug in mem0, Supermemory, Zep, Letta, or their own adapter — **or several at once**, with the agent
  reading across all of them and writing according to policy. No lock-in, no single point of failure,
  full export at any time.

  ### Non-negotiable product properties

  | Property | Meaning |
  |---|---|
  | **Local-first** | Works fully offline with zero external services. Sessions always live in local SQLite. Remote memory is an *enhancement*, never a dependency. |
  | **Provider-agnostic** | Memory providers and LLM providers are both pluggable. Core code must never import a vendor SDK directly. |
  | **Memory is best-effort** | A dead, slow, or misconfigured memory provider degrades recall quality. It must **never** fail a turn, corrupt a session, or block the user. |
  | **Deterministic persistence** | Every session is a replayable append-only log. Rewind restores conversation *and* working tree. |
  | **Explicit trust** | Code from plugins, hooks, and skills runs on the user's machine. Trust is granted, recorded, and revocable — never assumed. |
  | **Observable** | Every model call, tool call, hook run, memory write, and agent spawn is a queryable row with a parent link. |
  | **Portable data** | `kalash memory export` and `kalash session export` produce complete, vendor-neutral archives. Users own their data. |

  ---

  ## 2. Tech stack (decided — do not relitigate without a reason)

  | Concern | Choice | Why |
  |---|---|---|
  | Language | Python **3.12+** floor, **3.14** primary target | 3.11+ gives `asyncio.TaskGroup` and `ExceptionGroup`, which the orchestration layer depends on. 3.14 is current stable and makes free-threading viable for CPU-bound tool work. |
  | Packaging / dev | `uv` + `hatchling` | Fast, lockfile-based, handles tool install cleanly. |
  | Models & config | `pydantic` v2 | One validation story for config files, tool schemas, and provider payloads. Tool JSON Schemas are generated from Pydantic models — never hand-written. |
  | CLI | `typer` + `rich` | Large multi-command surface; typer scales. |
  | TUI | `textual` | Async-native, testable via `Pilot`, works over SSH. |
  | HTTP | `httpx` | Async, HTTP/2, sane timeouts. |
  | Concurrency | stdlib `asyncio` | `TaskGroup` for structured concurrency, cancellation that actually propagates. |
  | Storage | stdlib `sqlite3` in WAL, offloaded via `asyncio.to_thread` | Avoids an extra dependency and gives explicit control over the single-writer discipline. Optional `sqlite-vec` extension for local vectors, `FTS5` for keyword. |
  | MCP | official `mcp` Python SDK **v2** | Speaks the `2026-07-28` revision (stateless core, `server/discover`) and negotiates older revisions automatically. |
  | Logging | `structlog` (JSON to file, pretty to console) | Structured events pair with the audit tables. |
  | Tracing | `opentelemetry` — **optional extra**, opt-in | Never on by default. No telemetry without consent. |
  | Lint / format | `ruff` (both) | Single tool. |
  | Types | `mypy --strict` on `src/kalash` | Public interfaces must be fully typed. |
  | Tests | `pytest`, `pytest-asyncio`, `hypothesis`, `syrupy` | Property tests for the merge/dedupe logic, snapshots for prompt assembly. |

  **Explicitly rejected:** LangChain, LlamaIndex, or any agent framework. We own the loop. Framework
  abstractions would fight the permission model, the context budget, and the memory router.

  ---

  ## 3. Repository layout

  ```
  kalash-code/
  ├── CLAUDE.md                    # this file
  ├── AGENT.md                     # runtime agent contract
  ├── README.md
  ├── pyproject.toml
  ├── uv.lock
  ├── src/kalash/
  │   ├── __main__.py
  │   ├── core/                    # foundations, zero internal deps
  │   │   ├── config.py            # layered config resolution
  │   │   ├── paths.py             # XDG-aware path resolution
  │   │   ├── events.py            # in-process async event bus
  │   │   ├── errors.py            # exception taxonomy
  │   │   ├── ids.py               # ULID generation
  │   │   ├── redact.py            # secret/PII scrubbing (used before ANY egress)
  │   │   └── budget.py            # token accounting & cost math
  │   ├── storage/
  │   │   ├── engine.py            # connection mgmt, WAL, single-writer queue
  │   │   ├── migrations/          # 0001_init.sql, 0002_*.sql — forward-only
  │   │   ├── blobs.py             # content-addressed blob store
  │   │   └── repositories/        # sessions, messages, checkpoints, usage, audit
  │   ├── models/                  # LLM gateway (NOT data models — see core/)
  │   │   ├── gateway.py           # unified completion/stream interface
  │   │   ├── normalize.py         # tool-call & content-block normalization
  │   │   └── providers/           # anthropic, openai, google, bedrock, vertex,
  │   │                            #   ollama, openai_compatible
  │   ├── memory/                  # ★ the memory infrastructure layer
  │   │   ├── protocol.py          # MemoryProvider, Capability, records, Scope
  │   │   ├── registry.py          # discovery via entry points + config
  │   │   ├── router.py            # multi-provider read/write orchestration
  │   │   ├── ledger.py            # durable write-intent log & reconciliation
  │   │   ├── pipeline/
  │   │   │   ├── extract.py       # candidate memory extraction
  │   │   │   ├── salience.py      # keep/drop scoring
  │   │   │   ├── dedupe.py        # hash + near-dup collapse, supersede logic
  │   │   │   ├── merge.py         # cross-provider rank fusion (RRF)
  │   │   │   ├── rerank.py        # optional cross-encoder / LLM rerank
  │   │   │   └── inject.py        # budget-aware context block construction
  │   │   └── providers/
  │   │       ├── local/           # built-in: SQLite + FTS5 + sqlite-vec + edges
  │   │       ├── mem0_provider.py
  │   │       ├── supermemory_provider.py
  │   │       ├── zep_provider.py
  │   │       ├── letta_provider.py
  │   │       └── http_provider.py # generic adapter for a user's own HTTP endpoint
  │   ├── runtime/
  │   │   ├── loop.py              # the agent turn loop
  │   │   ├── context.py           # context assembly & ordering
  │   │   ├── compaction.py        # summarization when budget is exceeded
  │   │   ├── checkpoint.py        # snapshot / rewind
  │   │   └── stream.py            # incremental output, interruption
  │   ├── tools/
  │   │   ├── base.py              # Tool ABC, schema generation, result envelope
  │   │   ├── registry.py
  │   │   ├── fs.py                # read, write, edit, multi_edit, glob
  │   │   ├── shell.py             # sandboxed command execution
  │   │   ├── search.py            # ripgrep-backed content search
  │   │   ├── notebook.py          # .ipynb aware edits
  │   │   ├── web.py               # fetch + search
  │   │   ├── todo.py              # task list
  │   │   ├── memory_tools.py      # recall / remember / forget
  │   │   └── task.py              # spawn subagent
  │   ├── permissions/
  │   │   ├── policy.py            # rule evaluation
  │   │   ├── grants.py            # persisted decisions (once/session/always)
  │   │   └── prompt.py            # approval UX contract
  │   ├── sandbox/
  │   │   ├── linux.py             # Landlock + seccomp, bubblewrap fallback
  │   │   ├── macos.py             # Seatbelt (sandbox-exec)
  │   │   ├── windows.py           # AppContainer + Job objects
  │   │   └── policy.py            # writable roots, protected paths, network
  │   ├── orchestration/
  │   │   ├── subagent.py          # isolated-context child runs
  │   │   ├── team.py              # fanout / pipeline / critic / supervisor
  │   │   ├── blackboard.py        # shared state between agents
  │   │   └── budgets.py           # per-run token/turn/wallclock ceilings
  │   ├── scheduler/
  │   │   ├── cron.py              # expression parsing & next-fire math
  │   │   ├── daemon.py            # tick loop with advisory lock
  │   │   └── runs.py              # execution records, retry/backoff
  │   ├── hooks/
  │   │   ├── events.py            # event enum + payload schemas
  │   │   ├── runner.py            # command / http / python dispatch
  │   │   └── trust.py             # hash-pinned project trust
  │   ├── skills/
  │   │   ├── loader.py            # SKILL.md discovery & frontmatter parse
  │   │   └── index.py             # progressive-disclosure catalog
  │   ├── mcp/
  │   │   ├── client.py            # per-server lifecycle
  │   │   ├── manager.py           # multiplexing, namespacing, lazy connect
  │   │   ├── auth.py              # OAuth + keyring storage
  │   │   └── server.py            # expose Kalash tools AS an MCP server
  │   ├── plugins/
  │   │   ├── manifest.py
  │   │   ├── loader.py
  │   │   └── marketplace.py
  │   ├── cli/                     # typer command groups
  │   ├── tui/                     # textual app
  │   └── sdk/                     # public Python API for embedding
  ├── tests/
  │   ├── unit/
  │   ├── integration/
  │   ├── e2e/                     # scripted TUI sessions via textual Pilot
  │   └── fixtures/
  └── docs/
  ```

  ### Import discipline

  Dependencies point **downward only**:

  ```
  cli / tui / sdk
        ↓
  orchestration · scheduler
        ↓
  runtime
        ↓
  tools · memory · models · hooks · skills · mcp · plugins · permissions · sandbox
        ↓
  storage
        ↓
  core
  ```

  `core` imports nothing from `kalash`. `storage` imports only `core`. Sideways imports between peer
  modules go through `core.events` or an explicit protocol, never a direct import. Vendor SDKs
  (`anthropic`, `mem0ai`, `supermemory`, …) may only be imported inside
  `models/providers/*` or `memory/providers/*`, and always lazily inside the function or `__init__`
  that needs them so an uninstalled optional extra can't break startup.

  ---

  ## 4. The memory infrastructure layer

  This is the differentiating subsystem. Read this section before touching anything under
  `src/kalash/memory/`.

  ### 4.1 Memory kinds

  The layer is not a single blob store. Records are typed, because retrieval and routing differ per
  kind.

  | Kind | Content | Lifetime | Where it lives |
  |---|---|---|---|
  | `SESSION` | Verbatim transcript | Forever until deleted | **Local SQLite only.** Never sent to a memory provider. Source of truth for resume/rewind. |
  | `WORKING` | Compacted live context | Per session | Local, derived, regenerable. |
  | `SEMANTIC` | Facts, preferences, decisions ("uses pnpm", "prefers early returns") | Long-lived, supersedable | Provider-backed |
  | `EPISODIC` | Narratives of past work ("migrated auth to JWT on 2026-04-02, reverted") | Long-lived, decaying relevance | Provider-backed |
  | `PROCEDURAL` | Repo playbooks, how-to sequences | Long-lived | Provider-backed **and** file-backed (`KALASH.md`, skills) |
  | `ENTITY` | Graph of symbols, services, people, deps | Long-lived | Provider-backed if provider has `GRAPH` capability, else local edge table |
  | `ARTIFACT` | Code/doc chunks for retrieval | Rebuildable index | Local by default (source code egress is opt-in) |

  **Rule:** `SESSION` never leaves the device via the memory layer. `ARTIFACT` egress requires
  explicit `memory.egress.artifacts: true`. Shipping a user's source code to a third party by default
  is unacceptable.

  ### 4.2 Provider protocol

  ```python
  # src/kalash/memory/protocol.py  (shape, not final code)

  class Capability(StrEnum):
      SEMANTIC_SEARCH = "semantic_search"
      KEYWORD_SEARCH  = "keyword_search"
      HYBRID_SEARCH   = "hybrid_search"
      GRAPH           = "graph"
      LLM_EXTRACTION  = "llm_extraction"   # provider extracts facts server-side
      TIME_DECAY      = "time_decay"
      NAMESPACES      = "namespaces"
      HISTORY         = "history"          # audit trail of record changes
      TTL             = "ttl"
      BULK_EXPORT     = "bulk_export"
      RERANK          = "rerank"
      ATTACHMENTS     = "attachments"

  @dataclass(frozen=True, slots=True)
  class Scope:
      user_id: str
      project_id: str
      org_id: str | None = None
      repo_id: str | None = None
      branch: str | None = None
      session_id: str | None = None
      agent_id: str | None = None
      visibility: Visibility = Visibility.PROJECT   # GLOBAL | PROJECT | SESSION | AGENT

  class MemoryProvider(Protocol):
      name: str
      capabilities: frozenset[Capability]

      async def health(self) -> ProviderHealth: ...
      async def write(self, records: Sequence[MemoryWrite], scope: Scope) -> WriteReceipt: ...
      async def recall(self, query: RecallQuery, scope: Scope) -> list[MemoryHit]: ...
      async def get(self, ids: Sequence[str]) -> list[MemoryRecord]: ...
      async def update(self, edits: Sequence[MemoryEdit]) -> WriteReceipt: ...
      async def forget(self, selector: ForgetSelector) -> ForgetReceipt: ...
      async def history(self, record_id: str) -> list[MemoryRevision]: ...
      async def export(self, scope: Scope) -> AsyncIterator[MemoryRecord]: ...
      async def stats(self, scope: Scope) -> ProviderStats: ...
  ```

  **Capability gating is mandatory.** The router inspects `capabilities` and adapts. Never assume a
  provider can do graph traversal, keep history, or extract facts. When a requested feature is
  unavailable, the router either compensates locally or records an explicit degradation in the ledger —
  it never silently returns worse results without a trace.

  Methods beyond `health`/`write`/`recall` may raise `UnsupportedCapability`; the router treats that as
  a normal, non-fatal outcome.

  ### 4.3 Scope mapping to real providers

  Providers have their own tenancy vocabularies. Adapters own the translation:

  - **mem0** — `Scope.user_id → user_id`, `Scope.agent_id → agent_id`, `Scope.session_id → run_id`,
    `project_id` into `metadata` + filters. mem0 has `LLM_EXTRACTION`, so pass raw message lists with
    `infer=True` and let it extract; do not pre-extract, that's double work and worse results.
  - **Supermemory** — project/session identity goes into container tags; use hybrid search mode.
    Supports self-hosting via `base_url`, so the adapter must not hardcode the cloud endpoint.
  - **local** — scope columns are indexed directly; `visibility` becomes a `WHERE` clause.

  Every adapter needs a scope round-trip test: write under scope A, confirm it is **not** visible under
  scope B. Cross-project leakage is the highest-severity bug class in this subsystem.

  ### 4.4 The router

  `MemoryRouter` is the *only* thing the runtime talks to. It never exposes a provider handle.

  **Write policies**

  | Policy | Behaviour |
  |---|---|
  | `primary` | Write to the primary provider only. |
  | `mirror_all` | Write to every enabled provider. Full redundancy, higher cost. |
  | `by_kind` | Route per memory kind, e.g. `SEMANTIC → mem0`, `ARTIFACT → local`, `ENTITY → <GRAPH provider>`. |
  | `sticky` | New records go to primary; **updates go to whichever provider owns the record.** |

  **Read policies**

  | Policy | Behaviour |
  |---|---|
  | `primary_only` | Cheapest, lowest latency. |
  | `fanout_merge` | Query all healthy providers concurrently, then fuse. The flagship mode. |
  | `cascade` | Try primary; fall through to the next on error or empty result. |

  **Fusion, when fanning out.** Raw similarity scores from different providers are not comparable —
  different embedding models, different metrics, different normalization. Do **not** compare them
  numerically. Use **Reciprocal Rank Fusion** over each provider's ranking, with configurable
  per-provider weights. Then: dedupe by content hash, collapse near-duplicates above an embedding
  similarity threshold (preferring the record with richer provenance), optionally rerank, then fit to
  the token budget.

  **Resilience contract**

  - Per-provider timeout, default 1500 ms for recall. Late results are dropped, not awaited.
  - Circuit breaker per provider: N consecutive failures → open for a backoff window → half-open probe.
  - All fanout via `asyncio.TaskGroup` with `return_exceptions` semantics; one provider raising must not
    cancel the others.
  - If **every** provider fails, `recall()` returns `[]` and logs a degradation. The turn proceeds.

  ### 4.5 The ledger — why local durability matters

  `memory_ledger` records every write *intent* before it is dispatched, plus a per-provider receipt.

  This buys three things that are hard to retrofit:

  1. **Offline writes.** No network? The intent is durable; a background reconciler flushes it later.
  2. **Reconciliation.** Mirroring is eventually consistent; the ledger is how we detect and repair
    divergence.
  3. **Migration.** `kalash memory migrate --from mem0 --to local` replays the ledger and provider
    exports into a new backend. Users can leave any provider, including ours.

  ### 4.6 Write pipeline

  Off the critical path, always. A background queue drains it.

  ```
  turn ends
    → extract candidates      (rules first, then batched LLM extraction — skip if provider has LLM_EXTRACTION)
    → salience score          (drop trivia, ephemera, and anything already known)
    → redact                  (core.redact: secrets, tokens, keys, .kalashignore paths, PII patterns)
    → dedupe / supersede      (contradiction → supersede old record, keep history)
    → router.write()          (per write policy)
    → ledger receipts
  ```

  The user's turn **never** waits on this. If the process exits mid-drain, the ledger replays on next
  start.

  ### 4.7 Recall pipeline

  Triggered at turn start (budgeted), by explicit `recall` tool call, or by a `PreRecall` hook.

  ```
  query synthesis (current message + recent turns + open files)
    → router.recall() fanout
    → RRF merge → dedupe → rerank
    → drop anything already verbatim in context
    → fit to memory token budget (default: 8% of context window)
    → render <memory> block with record IDs + provenance + age
  ```

  Injected records **must** carry their ID and source provider so the agent can cite them and so
  `forget` can target them. See `AGENT.md` §Memory for the agent-side contract.

  ### 4.8 Egress and privacy

  Anything leaving the machine passes `core.redact` first. Non-negotiable.

  ```toml
  [memory.egress]
  mode      = "redacted"   # none | redacted | full
  artifacts = false        # send source-code chunks to remote providers?
  audit     = true         # log every outbound record to audit_log
  ```

  `mode = "none"` forces local-only regardless of configured providers — the setting that makes Kalash
  usable inside an air-gapped or compliance-bound environment. `kalash memory audit` shows exactly what
  was sent, when, and to whom.

  ### 4.9 The built-in provider must be genuinely good

  If the local provider is a toy, "bring your own provider" becomes "you must bring a provider," and we
  have failed the local-first property. Target: SQLite + `FTS5` keyword + `sqlite-vec` vectors + an
  edge table for graph queries + a pluggable embedder that includes a **local ONNX/GGUF option** so it
  works with no API key and no network.

  ---

  ## 5. Sessions & SQLite

  Location: `~/.kalash/kalash.db` (override `KALASH_HOME`). Per-project caches under
  `.kalash/cache/`, never committed.

  ### Table groups

  - **Sessions** — `sessions`, `turns`, `messages`, `content_blocks`, `tool_calls`
  - **Recovery** — `checkpoints`, `file_snapshots`
  - **Memory (local provider)** — `memory_records`, `memory_vectors`, `memory_edges`
  - **Memory (layer-wide)** — `memory_ledger`, `provider_state`
  - **Orchestration** — `agent_runs`, `blackboard`
  - **Scheduling** — `schedules`, `schedule_runs`
  - **Extensibility** — `mcp_servers`, `plugins`, `trust_grants`, `hook_runs`
  - **Governance** — `permission_grants`, `usage_events`, `audit_log`
  - **Meta** — `schema_migrations`

  ### Rules

  1. **Append-only message log.** Never `UPDATE` a message. Corrections are new rows. This is what
    makes replay and rewind correct.
  2. **Monotonic `seq` per session** on every message. Ordering is explicit, never inferred from
    timestamps.
  3. **WAL mode**, `busy_timeout=5000`, `foreign_keys=ON`, `synchronous=NORMAL`.
  4. **Single writer.** Multiple `kalash` processes plus the scheduler daemon share one DB. All writes
    funnel through one serialized queue in `storage/engine.py`. Reads may go wide.
  5. **Blobs out of the DB.** Payloads over 64 KiB go to `~/.kalash/blobs/<sha256[:2]>/<sha256>`; the
    row stores the digest. Keeps the DB small and dedupes identical file snapshots for free.
  6. **Forward-only migrations.** Numbered SQL files. No down-migrations — ship a fix-forward instead.
    Every migration gets a test that runs it against a fixture DB from the previous version.
  7. **Provider-specific data goes in JSON columns**, never new columns per provider.

  ### Rewind

  A checkpoint pairs a message `seq` with a content-addressed manifest of touched files. `kalash rewind`
  restores both. Conversation-only rewind and files-only rewind are separate flags. Because snapshots
  are content-addressed, taking one is cheap and idempotent.

  ---

  ## 6. Runtime: the agent loop

  Normative behaviour lives in `AGENT.md`. Implementation notes:

  **Context assembly order** (`runtime/context.py`) — order is load-bearing for prompt caching; append
  volatile content last:

  ```
  1. system identity + operating contract        (static → cacheable)
  2. tool schemas                                (static per session)
  3. skills catalog: name + description only     (progressive disclosure)
  4. environment: cwd, git status, platform
  5. project instructions: KALASH.md hierarchy   (user → project → nearest subdir)
  6. <memory> block from the router
  7. compacted history summary
  8. recent verbatim turns
  9. current user message
  ```

  Prompt-cache breakpoints go after (3) and (5). Anything that changes every turn must not appear
  earlier, or we invalidate the cache and pay for it on every request.

  **Turn loop** (`runtime/loop.py`):

  ```
  assemble → stream model output
    → parse tool calls
    → for each: PreToolUse hooks → permission gate → execute → PostToolUse hooks
    → append results
    → repeat until: no tool calls | budget exhausted | user interrupt | Stop hook blocks
  ```

  - Read-only tools execute **concurrently**; writes and shell commands **serialize**. Tools declare
    `side_effect: none | read | write | exec` in `tools/base.py` and the loop schedules from that.
  - Interruption is real cancellation via `TaskGroup`, not a flag polled between steps. A cancelled
    tool must leave no partial write — writes are atomic (temp file + `os.replace`).
  - Compaction triggers at a configurable fraction of the context window (default 0.82). The
    summarizer must preserve: file paths touched, decisions made, open TODOs, failed approaches, and
    user constraints. Everything else is fair game. Emit `PreCompact`/`PostCompact` hooks.

  ---

  ## 7. Permissions & sandboxing

  Two independent axes, following the model Codex uses (sandbox = capability boundary, approvals = when
  to pause). Conflating them produces either a nag-fest or a footgun.

  **Sandbox modes:** `read-only` · `workspace-write` · `danger-full-access`
  **Approval policies:** `untrusted` · `on-request` · `on-failure` · `never`

  **Defaults:** `workspace-write` + `on-request`, **network disabled inside the sandbox**.

  Platform backends: Linux → Landlock LSM + seccomp, with bubblewrap fallback on older kernels; macOS →
  Seatbelt via `sandbox-exec`; Windows → AppContainer + Job objects. `kalash doctor` must report which
  backend is active and **loudly** warn when no enforcement is available — a sandbox that silently
  degrades to no sandbox is worse than none, because the user thinks they're protected.

  **Protected paths**, denied even under `workspace-write`: `.git/config`, `.git/hooks/`, `.env*`,
  `~/.ssh/`, `~/.aws/`, `~/.kalash/credentials*`, keyring stores. Overridable only by explicit config,
  with a warning.

  Grants persist in `permission_grants` scoped `once | session | always`, keyed by
  (project, tool, normalized argument pattern). Deny rules always beat allow rules.

  > Implementation note for whoever builds this: the sandbox is a **security boundary**, not a config
  > flag. It needs adversarial tests — symlink escapes, `..` traversal, `/proc/self` tricks, subprocess
  > re-exec, `LD_PRELOAD`. If those tests don't exist, the feature isn't done.

  ---

  ## 8. Orchestration: subagents & multi-agent

  ### Subagents

  Definition file: `.kalash/agents/<name>.md` (or `~/.kalash/agents/` for user-global).

  ```markdown
  ---
  name: test-writer
  description: Writes and repairs tests. Use when coverage gaps or failing tests are found.
  tools: [read, write, edit, search, shell]
  model: sonnet
  memory: { read: project, write: none }
  max_turns: 25
  max_tokens: 150000
  ---

  You write tests that fail for the right reason before they pass...
  ```

  Contract: a subagent runs in an **isolated context**. The parent sees only the structured result
  envelope, not the child's transcript. This is the whole point — delegation as context compression.
  Every run is a row in `agent_runs` with `parent_run_id`, so the full trace is a tree.

  ### Multi-agent patterns (`orchestration/team.py`)

  | Pattern | Shape | Use |
  |---|---|---|
  | `fanout` | N independent agents, results merged | Search across many files, evaluate many candidates |
  | `pipeline` | Sequential with typed handoff | research → design → implement → review |
  | `critic` | Proposer + reviewer loop until accept or max rounds | High-stakes changes |
  | `supervisor` | A lead agent dynamically delegates | Open-ended tasks |

  Shared state through `blackboard` (a table, not shared memory objects). Hard requirements: global
  concurrency cap, per-run token/turn/wallclock ceilings from `orchestration/budgets.py`, cycle
  detection on the spawn graph, and a max spawn depth. An agent that can spawn agents can spend money
  exponentially — the ceilings are not optional.

  ---

  ## 9. Built-in cron scheduler

  Durable, SQLite-backed. Not APScheduler in memory — schedules must survive restarts.

  ```
  schedules(id, name, spec, spec_kind, timezone, agent, prompt, sandbox_mode,
            approval_policy, tools_allow, catchup_policy, overlap_policy,
            jitter_s, max_runs, enabled, next_run_at, ...)
  ```

  - `spec_kind`: `cron` | `interval` | `once` | `event`
  - `catchup_policy`: `fire_missed` | `skip_missed` — machine was asleep; what now?
  - `overlap_policy`: `skip` | `queue` | `parallel` — previous run still going; what now?
  - Timezone-aware, DST-correct. Compute next fire in local wall time, store UTC.
  - One runner fires a given schedule: `kalash serve` takes an advisory lock row with a lease and
    heartbeat, so a crashed daemon's schedules recover instead of stalling forever.
  - Runs are headless agent sessions. Output lands in a normal session row plus a `Notification` hook.
  - Failures retry with exponential backoff and a cap; repeated failure auto-disables and notifies.

  **Safety default:** scheduled runs default to `read-only` sandbox and `approval_policy: never` (which
  in read-only mode means "cannot do damage, no one is watching"). Granting a *scheduled, unattended*
  agent write access requires an explicit opt-in flag per schedule. Nobody should discover at 3am that
  a cron agent force-pushed.

  ---

  ## 10. Hooks

  Deterministic control points — where users encode policy that the model cannot talk its way out of.

  **Events:** `SessionStart` `SessionEnd` `UserPromptSubmit` `PreToolUse` `PostToolUse` `PreFileEdit`
  `PostFileEdit` `PreCompact` `PostCompact` `PreRecall` `PostRecall` `PreMemoryWrite` `SubagentStart`
  `SubagentStop` `PreTaskExec` `PostTaskExec` `ScheduleTrigger` `Notification` `Stop` `PluginLoad`

  **Handler types:** `command` (JSON on stdin), `http` (JSON POST), `python` (in-process entry point,
  fastest, no subprocess cost).

  **Exit-code contract** for `command` handlers — deliberately the same shape Claude Code uses, so
  existing hook scripts port over:

  - `0` → proceed; stdout is fed back as context for `SessionStart`, `UserPromptSubmit`, `PreToolUse`
  - `2` → **block**; stderr is returned to the agent as correctable feedback
  - anything else → non-blocking error, logged to `hook_runs`, execution continues

  Structured decisions on stdout for richer control:

  ```json
  {"hookSpecificOutput": {"permissionDecision": "ask",
                          "permissionDecisionReason": "touches migrations/"}}
  ```

  **Trust model.** Hooks are executable code loaded from project files — a `git clone` should not be
  able to run arbitrary commands on your machine. First load of a project's hooks prompts for trust and
  records a **content hash** in `trust_grants`. Any edit invalidates the grant and re-prompts. Same
  discipline for plugins and skill scripts.

  **Loop protection.** A hook that triggers the tool it guards will recurse. The runner tracks hook
  depth per event chain and refuses to re-enter beyond depth 1, logging a cycle warning.

  ---

  ## 11. Skills

  Follow the open **Agent Skills** spec so skills from the wider ecosystem work unmodified.

  ```
  skills/my-skill/
  ├── SKILL.md          # required: YAML frontmatter + markdown body
  ├── references/       # loaded on demand, not up front
  ├── scripts/          # executable helpers
  └── assets/           # templates, data
  ```

  Frontmatter: `name` (1–64 chars, `[a-z0-9-]`, must match directory), `description` (1–1024 chars,
  include trigger phrases — this is what the model matches against), plus optional `version`,
  `allowed-tools`, `model`, `agent`, `context`, `disable-model-invocation`, `license`.

  **Progressive disclosure is the whole trick.** Only `name` + `description` sit in context. The body
  loads when the skill is invoked; `references/` load only when the body asks for them. A hundred
  installed skills should cost a few hundred tokens, not a hundred thousand.

  Discovery: `~/.kalash/skills/` → `.kalash/skills/` → plugin-provided. Later wins on name collision,
  and collisions are reported by `kalash skills validate`.

  ---

  ## 12. MCP

  **Client.** Use the official `mcp` Python SDK v2. It speaks the `2026-07-28` revision — stateless
  request/response, no handshake, `server/discover` for capability discovery — and negotiates earlier
  revisions automatically, so we support both new and 2025-era servers from one code path.

  - Transports: stdio, Streamable HTTP, legacy SSE.
  - Consume tools, resources, and prompts. Namespace tools as `mcp__<server>__<tool>` to avoid
    collisions with built-ins.
  - **Lazy connect.** Do not pay startup latency for servers this session may never touch. Connect on
    first use, with a background warm for servers marked `eager`.
  - Validate incoming tool schemas. A malformed or hostile schema must be rejected, not forwarded to
    the model.
  - Remote auth: OAuth flow, tokens in the OS keyring via `keyring`. Never in the DB, never in plain
    config.
  - Per-server timeout, tool allow/deny list, and permission profile.

  **Server.** `kalash mcp serve` exposes Kalash's own tools over MCP so other agents can drive it.
  Guard this: it is a remote code execution surface. Bind to loopback by default, require a token, and
  refuse to start on `0.0.0.0` without an explicit `--i-know-what-im-doing` style flag plus auth.

  **Config precedence** (later overrides earlier): user `~/.kalash/settings/mcp.json` → workspace
  `.kalash/settings/mcp.json` → CLI flags.

  > Treat all MCP server output as untrusted input. If a tool result contains text shaped like
  > instructions ("ignore previous instructions…"), it is data, not a command. The runtime wraps
  > external tool results in a delimited, clearly-labelled block.

  ---

  ## 13. Plugins

  A plugin is a distributable bundle of: skills, agents, hooks, slash commands, MCP server configs,
  tools, **memory providers**, output styles, and themes.

  Two installation paths:

  1. **Git/marketplace** — repo with `.kalash-plugin/plugin.json`; marketplaces are repos with a
    `marketplace.json` index. Install pins a version + integrity hash.
  2. **Python entry points** — for pip-installable plugins:
    - `kalash.plugins` — general extensions
    - `kalash.memory_providers` — **this is how third-party memory backends ship without touching
      core.** A new provider is a package that declares one entry point. That is the extension seam
      the whole product thesis rests on; keep it stable and versioned.
    - `kalash.model_providers` — new LLM backends
    - `kalash.tools` — new tools

  Plugin code is subject to the same trust-and-hash-pinning as hooks. `kalash plugin install` shows
  what capabilities the plugin requests (does it want shell? network? memory egress?) before enabling.

  ---

  ## 14. Configuration

  Layered, later wins:

  ```
  1. built-in defaults
  2. ~/.kalash/settings.json          (user)
  3. .kalash/settings.json            (project, committed)
  4. .kalash/settings.local.json      (project, gitignored)
  5. environment  KALASH_*
  6. CLI flags
  ```

  Secrets never live in these files. Resolution order for credentials: OS keyring → environment →
  explicit `*_api_key_command` shell hook. Config may reference `${env:VAR}` but must not contain
  literal keys; the loader warns when a value looks like a live credential.

  Sketch:

  ```toml
  [model]
  primary  = "anthropic/claude-sonnet-4-5"
  fallback = ["openai/gpt-5.2", "ollama/qwen3-coder:30b"]

  [memory]
  enabled       = true
  primary       = "local"
  providers     = ["local", "mem0", "supermemory"]
  read_policy   = "fanout_merge"
  write_policy  = "by_kind"
  recall_budget = 0.08          # fraction of context window

  [memory.routing]
  semantic = "mem0"
  episodic = "supermemory"
  artifact = "local"
  entity   = "local"

  [memory.providers.mem0]
  mode    = "platform"          # platform | self_hosted
  api_key = "${env:MEM0_API_KEY}"

  [memory.providers.supermemory]
  api_key  = "${env:SUPERMEMORY_API_KEY}"
  base_url = "http://localhost:6767"   # self-hosted

  [permissions]
  sandbox  = "workspace-write"
  approval = "on-request"
  network  = false
  ```

  ---

  ## 15. CLI surface

  ```
  kalash                              interactive TUI
  kalash -p "<prompt>"                headless / pipeable
  kalash resume [id] | rewind

  kalash session   ls | show | export | rm
  kalash memory    add | search | ls | forget | export | import | migrate | audit | doctor
  kalash provider  ls | test | bench
  kalash agents    ls | run | new
  kalash skills    ls | new | validate
  kalash mcp       add | ls | test | serve | auth
  kalash hooks     ls | test | trust
  kalash plugin    install | ls | update | remove | marketplace
  kalash cron      add | ls | rm | enable | disable | run | logs
  kalash config    get | set | edit | show
  kalash doctor                       environment + sandbox + provider health
  kalash serve                        scheduler daemon + optional HTTP API
  ```

  `kalash -p` must be composable: read stdin, respect `--output-format json`, exit non-zero on failure.
  It is the CI/scripting interface, so its contract is as important as the TUI's.

  ---

  ## 16. Development workflow

  ```bash
  uv sync --all-extras                 # install
  uv run kalash                        # run from source
  uv run ruff format . && uv run ruff check --fix .
  uv run mypy src/kalash
  uv run pytest -q                     # unit + integration
  uv run pytest -m e2e                 # scripted TUI sessions
  uv run pytest --cov=kalash --cov-report=term-missing
  ```

  Never start watch-mode processes in an agent session. Use `--run`-style single-shot invocations.

  ### Conventions

  - **Async by default** for anything touching I/O. No sync-over-async, no `asyncio.run` outside entry
    points.
  - **Typed public interfaces.** `mypy --strict` on `src/kalash`. Protocols over ABCs for extension
    seams (providers, tools, hooks) so third parties don't inherit from us.
  - **Errors** derive from `KalashError` with a stable `code`. Never swallow an exception without
    logging it with context. Never `except Exception: pass`.
  - **No print.** `structlog` for logs, `rich` only in `cli/` and `tui/`.
  - **Tool results are envelopes**, never bare strings: `{ok, content, metadata, truncated, error}`.
  - **Every write to the filesystem is atomic**: temp file in the same directory + `os.replace`.
  - **Docstrings** on public functions: what, and *why* if non-obvious. Skip restating the signature.
  - **Commits**: conventional commits. Small, reviewable. Never commit generated lockfile churn
    unrelated to your change.

  ### Testing strategy

  | Layer | Approach |
  |---|---|
  | Memory router | Property tests (`hypothesis`) on merge/dedupe/RRF: fusion must be deterministic and order-independent given the same inputs. |
  | Memory providers | One shared conformance suite every provider must pass, run against fakes in CI and real backends behind an opt-in marker. Includes the scope-isolation test. |
  | Storage | Migration tests from each prior schema version. Concurrency test: N writers, one DB, zero corruption. |
  | Agent loop | Recorded model fixtures (VCR-style). No live API calls in default test runs. |
  | Prompt assembly | `syrupy` snapshots so context-order regressions are visible in diffs. |
  | Sandbox | Adversarial escape tests per platform. |
  | TUI | `textual` `Pilot` scripted sessions. |
  | Hooks/plugins | Trust-invalidation tests: mutate a hook file, assert re-prompt. |

  ### Definition of done

  A change is done when: types pass, lint passes, tests cover the new behaviour *and* its failure mode,
  docs updated (`AGENT.md` if runtime behaviour changed, this file if structure changed), no new
  required dependency without justification, and — for anything touching memory, permissions, sandbox,
  or egress — a note in the PR describing the security implication.

  ---

  ## 17. Build order

  Each milestone ends with something a user can actually run.

  | # | Milestone | Ships |
  |---|---|---|
  | **M0** | Foundations | `core`, `storage` + migrations, config layering, `kalash doctor` |
  | **M1** | Single-agent loop | Model gateway (Anthropic + OpenAI), fs/shell/search tools, permission gate, TUI, session persist/resume |
  | **M2** | **Memory infra** | Protocol, registry, router, ledger, local provider (FTS5 + sqlite-vec), recall/write pipelines, `kalash memory *` |
  | **M3** | Remote providers | mem0 + Supermemory adapters, conformance suite, fanout merge, redaction + egress controls, `migrate` |
  | **M4** | Determinism | Hooks (all events, 3 handler types, trust model), skills loader with progressive disclosure |
  | **M5** | MCP | Client (v2 SDK, all transports), OAuth, `kalash mcp serve` |
  | **M6** | Orchestration | Subagents, the four team patterns, blackboard, budgets, trace tree |
  | **M7** | Scheduler | Durable cron, daemon with lease, catch-up/overlap policies, notifications |
  | **M8** | Ecosystem | Plugins, marketplace, entry-point providers |
  | **M9** | Hardening | Real sandboxes per platform + escape test suite, rewind/checkpoint, compaction quality work |
  | **M10** | Surfaces | Python SDK, HTTP server mode, IDE bridge, `--output-format json` stability |

  Memory lands at M2/M3 — before hooks, MCP, and orchestration. It is the differentiator; it should not
  be the thing we bolt on at the end.

  ---

  ## 18. Traps worth naming up front

  - **Silent memory degradation.** A provider quietly returning nothing looks identical to "no relevant
    memories." Always surface degradation in the ledger and in `kalash memory doctor`.
  - **Cross-scope leakage.** Project A's secrets surfacing in Project B. Test it explicitly, per
    provider.
  - **Score comparison across providers.** Tempting, wrong. Rank fusion only.
  - **Blocking the turn on memory writes.** Feels correct, destroys latency. Background queue, always.
  - **Prompt-cache invalidation.** One volatile token early in the prompt costs full price on every
    request. Guard assembly order with snapshot tests.
  - **Unbounded subagent spawning.** Exponential cost. Depth caps and budgets from day one.
  - **Sandbox that silently no-ops.** Worse than no sandbox, because the user trusts it.
  - **Hooks as an RCE vector.** `git clone` must never equal code execution. Hash-pinned trust.
  - **Prompt injection via tool results.** MCP servers, fetched web pages, and file contents are
    untrusted data. Delimit and label them.
  - **Migrations without tests.** A corrupted session DB is unrecoverable user data loss.

  ---

## 19. Specification tree

The detailed specifications live under `docs/`. This file is the blueprint; the specs below are the
contracts. Reading order for someone new:

| # | Document | Covers |
|---|---|---|
| 1 | [`docs/invariants.md`](docs/invariants.md) | 40 hard requirements (I-001..I-040) that must hold always |
| 2 | [`docs/capabilities.md`](docs/capabilities.md) | The unified authority vocabulary used by every subsystem |
| 3 | [`docs/threat-model.md`](docs/threat-model.md) | 10 threat actors, 4 attack chains, residual risk |
| 4 | [`docs/security.md`](docs/security.md) | Egress gateway, network policy, secrets, redaction, supply chain |
| 5 | [`docs/model-gateway.md`](docs/model-gateway.md) | Provider normalization, streaming, errors, fallback, cost |
| 6 | [`docs/model-providers.md`](docs/model-providers.md) | Provider catalog, routing strategies, connection flow |
| 7 | [`docs/tools.md`](docs/tools.md) | 14-stage tool lifecycle, filesystem, shell, git, web |
| 8 | [`docs/memory.md`](docs/memory.md) | KME native engine, federation, recall/write, deletion, poisoning |
| 9 | [`docs/permissions.md`](docs/permissions.md) | Decision algorithm, confirmation class, grants, approval UX |
| 10 | [`docs/state-machines.md`](docs/state-machines.md) | 11 formal machines, crash recovery, concurrent sessions |
| 11 | [`docs/context-budget.md`](docs/context-budget.md) | Assembly order, allocation, compaction, cost ceilings |
| 12 | [`docs/data-model.md`](docs/data-model.md) | SQLite schema, checkpoints, backup, export, GC |
| 13 | [`docs/observability.md`](docs/observability.md) | Audit schema, traces, error taxonomy, logging, metrics |
| 14 | [`docs/interfaces.md`](docs/interfaces.md) | TUI, CLI output, SDK, HTTP server, accessibility |
| 15 | [`docs/scheduler.md`](docs/scheduler.md) | Cron, events, webhooks, safety defaults, notifications |
| 16 | [`docs/platform.md`](docs/platform.md) | Cross-platform matrix, install lifecycle, config, time, i18n |
| 17 | [`docs/evaluation.md`](docs/evaluation.md) | Test taxonomy, security corpora, evals, CI pipeline |
| 18 | [`docs/operations.md`](docs/operations.md) | SLOs, release engineering, versioning, compatibility |

**Rule:** a change touching a subsystem updates its spec in the same PR. Specs are living documents
that drift when this rule is not enforced.

---

## 20. Non-goals

Stating these prevents scope explosion.

Kalash is **not**:

- A general-purpose autonomous computer-control agent (it works in codebases, not GUIs)
- A cloud-hosted multi-tenant coding platform (single-user, local-first)
- A replacement for GitHub or any git hosting (it uses them, it is not one)
- A general workflow automation platform (the scheduler is for agent tasks, not arbitrary jobs)
- A container runtime or orchestrator
- An IDE (it is a terminal companion to one)
- A model marketplace or billing aggregator (it connects to providers the user already has)
- A training or fine-tuning framework

Kalash **is** a memory provider (the Kalash Memory Engine) *and* a memory federator (the router).
It is not one posing as the other.

---

## 21. External referenceses

  Design informed by the following (content paraphrased and summarized for licensing compliance):

  - [MCP `2026-07-28` specification](https://modelcontextprotocol.io/specification/2026-07-28) and
    [MCP Python SDK v2](https://py.sdk.modelcontextprotocol.io/v2/whats-new/) — stateless core,
    `server/discover`, automatic revision negotiation
  - [mem0 LLM.md / API reference](https://github.com/mem0ai/mem0/blob/main/LLM.md) — `Memory` vs
    `MemoryClient`, `user_id`/`agent_id`/`run_id` scoping, server-side extraction via `infer`
  - [Supermemory Python SDK](https://supermemory.ai/docs/memory-api/sdks/python) and
    [self-hosting notes](https://supermemory.ai/docs/integrations/supermemory-sdk) — `base_url`
    override, hybrid search, retry/error taxonomy
  - [Claude Code hooks reference](https://code.claude.com/docs/en/Hooks) and
    [plugins reference](https://docs.claude.com/en/docs/claude-code/plugins-reference) — event set,
    exit-code semantics, plugin/marketplace layout
  - [Agent Skills standard](https://github.com/DiversioTeam/agent-skills-marketplace) — `SKILL.md`
    structure and frontmatter constraints
  - [Codex sandboxing](https://developers.openai.com/codex/concepts/sandboxing/) and
    [approvals & security](https://developers.openai.com/codex/agent-approvals-security/) — the
    two-axis sandbox × approval model, per-OS enforcement backends
  - [Python 3.14 release notes](https://docs.python.org/3/whatsnew/3.14.html) — current stable line,
    free-threading supported status
