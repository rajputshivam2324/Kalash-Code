# Operations

> **Status:** normative.
>
> This document owns the numbers and the process: performance SLOs, system-wide resource limits, prompt
> versioning, what a turn must record to be explainable (I-037), the version compatibility matrix that
> [`capabilities.md`](./capabilities.md) §9 and [`memory.md`](./memory.md) §20 defer to, release
> engineering including signing and SBOM, the plugin-signing design deferred here by
> [`security.md`](./security.md) §5.2, documentation requirements, the Definition of Done gates, and the
> I-040 enforcement audit performed at each release.
>
> How these numbers are measured, and every test suite referenced below, lives in
> [`evaluation.md`](./evaluation.md).

---

## 1. Performance targets (SLOs)

An unmeasurable target is not a target. Every row has a number, a measurement method, and a regression
threshold that fails CI. Percentiles are over the load and benchmark suites in `evaluation.md` §6,
recorded per run against a tracked baseline.

| # | Metric | Target | Measured by | Regression gate |
|---|---|---|---|---|
| 1 | **Cold start to interactive** | **< 500 ms** p95, excluding model initialization | `hyperfine` on `kalash --startup-probe`, which exits at first paint | **> 10%** vs baseline |
| 2 | Warm start (caches populated, same session dir) | < 250 ms p95 | Same harness, second run onward | > 10% |
| 3 | Time to first token | < 1.5 s p95 end-to-end; **< 120 ms** Kalash-side (submit → request on the wire) | Two spans: `turn.submit→egress.dispatch` and `egress.dispatch→first delta` | > 15% on the Kalash-side span only — the provider half is not ours to gate |
| 4 | **TUI input latency** | **< 50 ms** p99, keypress → rendered frame | `textual` Pilot harness with a synthetic 10 kHz key source | **> 50 ms absolute**, not relative |
| 5 | KME recall p95 | **< 100 ms** local, at 100k records | Load suite, indices built cold | > 20% (matches `memory.md` §17.4) |
| 6 | Memory write queue drain | < 5 s p95 after turn end for a 3–8 candidate batch; depth alarm at 100 intents | Ledger timestamps: `intent.created_at → receipt.acked_at` | > 25%, or any sustained depth > 100 |
| 7 | SQLite point read | < 1 ms p95 | Instrumented repository layer | > 25% |
| 8 | SQLite write transaction | < 10 ms p95 (uncontended); contention converts to latency via `busy_timeout`, never to an error | Same | > 25%, or any `SQLITE_BUSY` reaching a tool envelope |
| 9 | Tool dispatch overhead | < 5 ms p95 for stages 2–9 of the lifecycle, excluding `execute()` | Lifecycle span, `tools.md` §2 | > 30% |
| 10 | Scheduler tick | < 50 ms p95 with 1000 schedules, 100 due; fire drift < 1 s | Daemon instrumentation | > 25% or drift > 1 s |
| 11 | MCP connection establishment | stdio < 500 ms; remote < 1 s p95 | Per-server connect span | > 30% |
| 12 | Session resume | < 400 ms p95 for a 5000-turn session | `kalash resume` to first paint | > 15% |
| 13 | Large-repo initial index | 50k eligible files < 10 min, fully background, never blocking a turn | Load suite | > 25%, or any turn blocked on indexing |
| 14 | Incremental index after a file save | < 2 s p95 | Watcher-to-queryable span | > 30% |
| 15 | Compaction | < 8 s p95 (one model call plus assembly) | Turn instrumentation | > 25% |
| 16 | Interrupt acknowledgement | < 200 ms p99 from `SIGINT` to all in-flight work cancelled (I-029) | Cancellation suite | **> 200 ms absolute** |

Rows 4 and 16 have absolute rather than relative gates because both are perception thresholds. 50 ms is
roughly where keystroke echo stops feeling attached to the key; a relative gate would let latency ratchet
upward 9% at a time until it crossed that line without any single commit failing.

### 1.1 Startup is the metric users feel

Startup is the one number a user experiences on every single invocation, and it is the one most easily
destroyed by ordinary, well-intentioned code. The pattern is always the same: something is initialized at
import or at `main()` because that is where initialization is tidy, and the cost is paid whether or not
the feature is used.

Named defences, each with an owning invariant or test:

| Defence | What it prevents | Enforced by |
|---|---|---|
| **Lazy MCP connect** | N servers × ~400 ms serialized handshakes on every start. Six servers is three seconds before the prompt appears | `mcp/manager.py` connects on first tool resolution for a server. A test asserts zero connections after startup with servers configured |
| **Deferred repository index** | Walking a 50k-file tree before the first prompt | Index starts after first paint, on a background task, yielding to turn work (row 13) |
| **Lazy vendor imports** | `import anthropic`, `import mem0ai`, ONNX runtime, and tokenizers at module scope | Import discipline in `CLAUDE.md` §3 — vendor SDKs imported inside the function that needs them. A test asserts the set of modules present in `sys.modules` after startup against a committed allowlist |
| **No eager embedder load** | Loading a 33 MB ONNX model to display a prompt | Embedder materialized on first recall or write |
| **Migration check, not migration run** | A schema check that reads every table | Compare `schema_migrations` max against the code's max; run nothing when equal |
| **Lazy provider health** | Probing every model and memory provider at start | Health is checked by `kalash doctor` on request, never on the startup path |

The `sys.modules` allowlist test is the load-bearing one. It converts "remember to import lazily" from a
convention into a build failure, which is the only form of startup discipline that survives a year of
contributions.

---

## 2. Resource limits

System-wide budgets. These are distinct from the per-agent token, cost, and turn ceilings in
[`context-budget.md`](./context-budget.md) and `orchestration/budgets.py` (I-026): those bound what one
agent run may spend, these bound what the Kalash process may consume on the user's machine.

| Resource | Default | On breach | Configurable |
|---|---|---|---|
| Kalash RSS | Warn at 1 GiB, refuse new concurrent agents at 2 GiB | **Warn**, then **refuse** to admit new agent runs and new concurrent tool slots; running work finishes | Yes, user scope |
| Subprocess address space | 4 GiB per child (`RLIMIT_AS`; Job object on Windows) | **Refuse** — the child dies with a clear error in the tool envelope | Yes |
| Subprocess CPU time | tool `timeout_s` + 30 s (`RLIMIT_CPU`) | **Refuse** — SIGKILL after the grace period (I-013) | No — it is a backstop for a timeout that did not fire |
| Concurrent subprocesses | 16 | **Degrade** — queued on the exec semaphore | Yes |
| Processes per child tree | 512 (`RLIMIT_NPROC`) | Refuse | Yes |
| Open file descriptors | Soft-raise Kalash to 8192 and warn if the hard limit is lower; 4096 per child | Warn on raise failure; refuse the operation that would exceed | Partly |
| Concurrent tool slots | 8 reads; 1 write/exec, serialized ([`tools.md`](./tools.md) §3) | **Degrade** — queued, ordered per §3 | Yes |
| Concurrent agent runs | 8 global, spawn depth 5 | **Refuse** — surfaced to the parent as an adaptable tool error (I-027) | Yes, within the depth cap |
| Concurrent outbound connections | 32 total, 8 per host | Degrade — queued in the egress gateway | Yes |
| Disk, `~/.kalash` total | Warn at 5 GiB | Warn, with the largest consumers named | Yes |
| Blob store | 10 GiB; LRU GC of blobs unreferenced by any live checkpoint | **Degrade** — GC runs; if still over, refuse new blobs and report | Yes |
| SQLite database size | Warn at 2 GiB, strong warn at 5 GiB with a `kalash session rm --before` suggestion | **Warn only.** Never auto-delete history | Thresholds yes; auto-delete does not exist |
| WAL size | Checkpoint at 64 MiB | Automatic checkpoint | Yes |
| Log storage | 50 MiB × 10 rotations = 500 MiB | Degrade — oldest rotation dropped | Yes |
| Embedding model cache | 500 MiB on disk under `~/.kalash/models/` | Warn; LRU eviction of unused model versions | Yes |
| In-process vector cache | 256 MiB | Degrade — LRU eviction, recall falls back to disk | Yes |
| Tool output | 256 KiB / 2000 lines per call ([`tools.md`](./tools.md) §4) | Degrade — truncated with a declared marker | Yes |
| Session write volume | 100 MiB, warn at 50 | Warn | Yes |

Three rules behind the breach column.

**History is never deleted automatically.** The database holds the user's entire record of working with
the tool. Growth is reported with a remedy; the deletion decision is the user's. An agent that silently
garbage-collected sessions to stay under a threshold would be destroying the one thing that cannot be
regenerated.

**Refuse beats degrade wherever a silent degradation is indistinguishable from working.** Admitting a
ninth concurrent agent at 2 GiB RSS would trade a clear refusal for an OOM kill mid-write, which is the
worse failure by a wide margin.

**Every breach is a `structlog` event and a `kalash doctor` line**, not just a log entry. A limit that
fires invisibly produces a support conversation about mysterious slowness.

---

## 3. Prompt versioning

Every prompt Kalash sends is a versioned artifact in `kalash/prompts/`, and the version in force is
recorded on every turn (I-037).

| Prompt | ID | Consumed by | Snapshot test |
|---|---|---|---|
| System prompt / agent contract | `system` | `runtime/context.py` | `snapshot/test_system_prompt.py` |
| Tool descriptions (one version per tool, plus a set digest) | `tools.<name>` | Tool registry → request assembly | `snapshot/test_tool_schemas.py` |
| Memory extraction | `memory.extract` | `memory/pipeline/extract.py` | `snapshot/test_memory_prompts.py` |
| Memory injection block framing | `memory.inject` | `memory/pipeline/inject.py` | Same — the I-020 framing fields are asserted present |
| Compaction | `compaction` | `runtime/compaction.py` | `snapshot/test_compaction_prompt.py` |
| Rerank | `memory.rerank` | `memory/pipeline/rerank.py` | `snapshot/test_memory_prompts.py` |
| Query synthesis | `memory.query_synth` | Recall pipeline | Same |
| Title generation | `session.title` | Session creation | `snapshot/test_session_title.py` |
| Subagent contract | `subagent` | `orchestration/subagent.py` | `snapshot/test_subagent_prompt.py` |

**Scheme.** A monotonic integer per prompt (`system@7`), plus the SHA-256 of the rendered template
recorded alongside it. Both go in the turn record. The integer is what a human reads; the digest is what
makes the integer trustworthy.

**A test asserts the digest matches the registered version.** Editing a prompt without bumping its version
fails CI, naming the prompt and both digests. Without that test, version bumping is a convention, and the
first hurried prompt tweak breaks the audit trail for every session that follows — silently, because
nothing observable changes.

Snapshot tests exist so that a prompt change is a **diff a reviewer sees**. Prompt text is the highest-
leverage code in the product and the easiest to change without review noticing: a reordered instruction or
one softened word can measurably alter tool selection and safety behaviour, and none of it shows up in a
type check. A prompt change is also a likely cause of an eval movement, which is why the eval baseline
records prompt versions (`evaluation.md` §8.6) — otherwise a regression hunt starts by bisecting code that
did not change.

---

## 4. Reproducibility

### 4.1 What a turn records

"Replayable" must mean more than "we kept the chat log." I-037 requires a turn to be *explainable*, which
means recording every input that shaped it.

| Recorded | Field(s) | Why it is needed to explain the turn |
|---|---|---|
| Model identity | `model_id`, `model_version`, `provider`, `provider_endpoint` | Same name, different model, different behaviour |
| Sampling parameters | `temperature`, `top_p`, `max_output_tokens`, `stop`, `reasoning_effort` | The most common cause of "why was that so different today" |
| Prompt versions | `prompt_versions: {system: "7@sha256:…", memory.inject: "3@sha256:…", …}` | §3 |
| Tool schema digest | `tool_schema_digest`, plus per-tool `version` on each call | A widened parameter changes what the model could ask for ([`tools.md`](./tools.md) §2.4) |
| Skill and plugin state | Name, version, and **content hash** per loaded skill and plugin | Executable extensions are inputs. The hash is what the trust grant pins (I-031) |
| MCP state | Per server: name, revision negotiated, tool-set digest, transport | A server whose schema mutated is a changed input (T-B) |
| Config | `config_digest` over the effective merged config, security keys separated so a scope-ignored value is visible | A behaviour change with no code change is usually config |
| Memory | IDs of every injected record, their trust and provenance, and the recall query actually issued after synthesis | I-020: what the model was told about the past, and how it got there |
| Tool I/O | Normalized arguments, the full envelope, and `side_effects` per call | The actual sequence of effects on the world |
| Capability decisions | Resolved set, deciding stage, and `capabilities_source` per call | Why an action was allowed or refused ([`permissions.md`](./permissions.md) §4.1) |
| Environment | `env_digest`: OS, kernel, Python version and build flags, SQLite version and compile options, sandbox backend and enforcement level, locale, timezone | The 3.14t / no-FTS5 / no-Landlock class of difference |
| Working tree | Git SHA, branch, dirty flag, and a per-file digest of every modified-but-uncommitted file | The SHA alone is a lie on a dirty tree, which is the normal state during agent work |
| Registry | `model_registry_version`, `pricing_version` ([`model-gateway.md`](./model-gateway.md) §8.2) | Historical cost stays explainable after prices change |
| Usage and cost | Normalized `Usage`, cost as `Decimal`, `null` when pricing is unknown | Never `float`; never zero standing in for unknown |
| Time | `started_at`, `first_token_at`, `completed_at`, all UTC with explicit offset (I-038) | Ordering and latency attribution |

Storage cost is bounded by digesting rather than copying: config, environment, and tool schemas are
digests with the full value stored once in a content-addressed side table and referenced. A turn record
that duplicated the full config would be larger than the conversation.

### 4.2 Explainability, replay, determinism

Three different promises, routinely conflated, and only two of them are keepable.

| | Definition | Achievable | Kalash promise |
|---|---|---|---|
| **Explainability** | For any past turn, state exactly what the agent saw and why it acted: prompts, memory, tools, capabilities, decisions | **Yes**, entirely within our control | **Guaranteed.** `kalash session explain <turn>` reconstructs it from §4.1, offline, with no provider call |
| **Replay** | Re-issue the same reconstructed inputs to a provider and observe a fresh response | **Yes**, with caveats below | **Supported**, best-effort. `kalash session replay <turn> [--dry-run]` |
| **Determinism** | Identical inputs produce byte-identical output | **No** | **Not promised. Not attempted.** Any design that depends on it is wrong |

Why determinism is unavailable, plainly:

- **Model nondeterminism.** Even at `temperature: 0`, batching, non-associative floating-point reduction
  order, mixture-of-experts routing, and speculative decoding make identical requests produce different
  responses. Temperature 0 reduces variance; it does not remove it.
- **Provider models change behind a stable name.** A model ID is a product label, not a content hash. The
  weights behind it are updated, quantized, and re-served without notice.
- **The world moved.** Tool outputs depend on a filesystem, a git remote, a package index, and a network
  that are all different now. Re-running `npm test` on a repository that has advanced is a different
  observation, correctly.
- **Prompt caching.** Cache hits and misses change nothing semantically and everything about cost and
  timing, so cost and latency are not reproducible either.

Consequently `replay` is explicit about which of the three it is doing. `--dry-run` reconstructs the exact
request and prints it without sending — fully deterministic, because no model is involved, and the mode
that answers most debugging questions. A live replay reports what differed from the original: model
version, registry version, working-tree state, and any tool output that changed. It does not present a
different response as a failure; a differing response is the expected outcome.

This is stated at length because the honest version is more useful than the marketable one. "Fully
reproducible sessions" would be a false claim, and a user who trusts it will eventually conclude Kalash is
broken when a replay diverges — when in fact divergence is the only possible behaviour.

---

## 5. Version compatibility matrix

One row per release. Every protocol that crosses a boundary Kalash does not control has a version, and
that version is recorded in the turn record (§4.1) and in export headers.

| Kalash | DB schema | Plugin API | Memory protocol | Model provider | Tool protocol | Hook payload | MCP revision | Skill format | Export format | Capability protocol |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.1.0 (M0–M1) | 3 | — | — | 1 | 1 | — | — | — | 1 | 1 |
| 0.2.0 (M2) | 7 | — | 1 | 1 | 1 | — | — | — | 1 | 1 |
| 0.3.0 (M3) | 9 | — | 1 | 1 | 1 | — | — | — | 2 | 1 |
| 0.4.0 (M4) | 11 | — | 1 | 1 | 2 | 1 | — | 1 | 2 | 2 |
| 0.5.0 (M5) | 12 | — | 1 | 1 | 2 | 1 | 2026-07-28 | 1 | 2 | 2 |
| 0.6.0 (M6) | 14 | — | 1 | 1 | 2 | 1 | 2026-07-28 | 1 | 2 | 3 |
| 0.7.0 (M7) | 16 | — | 1 | 1 | 2 | 2 | 2026-07-28 | 1 | 3 | 3 |
| 0.8.0 (M8) | 17 | 1 | 1 | 1 | 2 | 2 | 2026-07-28 | 1 | 3 | 3 |
| 0.9.0 (M9) | 18 | 1 | **2** | **2** | **3** | 2 | 2026-07-28 | 1 | 3 | 4 |
| 1.0.0 (M10) | 19 | 1 | 2 | 2 | 3 | 2 | 2026-07-28 | 1 | 3 | 4 |

Bold entries are breaking bumps. Pre-1.0 they are permitted with a migration note in the changelog;
from 1.0.0 they follow the deprecation policy in §6.1.

### 5.1 Support window

| Client | Supported against |
|---|---|
| Plugin declaring API version *v* | Kalash whose supported range includes *v*. Range is the current version and the previous two minors |
| Memory provider package | Protocol *v* and *v*−1. The conformance suite (`memory.md` §21) is published per protocol version so a third party can verify before publishing |
| Export file | Every version ever emitted, forever. An export is a user's data escape hatch and it must never expire ([`memory.md`](./memory.md) §4.5) |
| Hook payload consumer | Current and previous. New fields are additive; a removal is a version bump |
| MCP server | Whatever the SDK negotiates, with `2026-07-28` preferred |

### 5.2 Mismatch rules

| Situation | Behaviour |
|---|---|
| **Newer plugin, older Kalash** | **Refuse to load.** Message names the plugin, its required version, the installed version, and the upgrade command. Never partially loaded |
| **Older plugin, newer Kalash** | Load if its declared version is inside the support window, with a deprecation warning naming the release that drops it. Outside the window, refuse with the same clarity |
| Unrecognized capability from either direction | **Denied**, never unconstrained ([`capabilities.md`](./capabilities.md) §9). A grant recorded against a different `protocol_version` is treated as absent and re-asked ([`permissions.md`](./permissions.md) §4.4) |
| Newer DB schema than the binary knows | **Refuse to start.** Report the found and maximum-known versions, and the path of the pre-migration backup. Forward-only migrations (I-022) mean an older binary cannot interpret a newer shape, and writing to it anyway risks unrecoverable corruption |
| Older DB schema | Migrate forward, after taking a backup retained until one successful start (I-022) |
| Newer export file | Refuse the import, naming the version. A partial import of a format we do not understand is silent data loss |
| Memory provider protocol mismatch | Provider is not registered; `kalash memory doctor` reports it as unavailable with the reason. Recall proceeds without it (I-005) |

**Partial loading is never an option.** A plugin that loads its hooks but not its tools produces a system
whose behaviour matches no version of anything, and the failure surfaces later as a mystery. Refusal with
a precise message is worse UX for thirty seconds and better UX forever after.

### 5.3 Forward compatibility of configuration

A config file written by a newer Kalash must still boot an older one.

| Case | Behaviour |
|---|---|
| Unknown key | **Warn once and ignore.** Named in the warning and in `kalash doctor` |
| Unknown value in a known enum | Warn, fall back to that key's default, continue. Never fail the whole config for one bad value |
| Unknown key under a security namespace (`permissions.*`, `network.*`, `memory.egress.*`, `capabilities.*`, `sandbox.*`) | Warn and ignore — and the resulting posture is displayed at startup, so a setting the user believes is active but is not is visible |
| Known key, wrong type | **Refuse to start**, naming file, key, expected type, and found value. A type error is a mistake, not a version difference |
| Project-scope security key | Ignored with `KALASH_CONFIG_SCOPE_IGNORED` regardless of version ([`permissions.md`](./permissions.md) §8) |

Warn-and-ignore for unknown keys, refuse for a wrong type: the first is almost always a version skew and
recoverable, the second is almost always a typo that would silently disable something.

---

## 6. Release engineering

### 6.1 Semantic versioning

`MAJOR.MINOR.PATCH`, with per-component meaning stated because "breaking change" means different things
to a user, a plugin author, and a provider author.

| Change | Bump |
|---|---|
| Removing or renaming a CLI command, flag, or `--output-format json` field | MAJOR |
| Removing a config key, or changing a default in a way that alters security posture | MAJOR |
| Breaking the plugin API, memory protocol, tool protocol, or hook payload schema | MAJOR (post-1.0) |
| Removing an export format reader | Never. Readers are permanent (§5.1) |
| New tool, new capability, new provider, new hook event | MINOR |
| New config key with a backward-compatible default | MINOR |
| DB migration that is purely additive | MINOR |
| Prompt version bump that measurably changes agent behaviour | MINOR, with the eval delta in the changelog |
| Bug fix, dependency patch bump, performance work | PATCH |
| Security fix | PATCH where possible, always with an advisory (§8) |

Tool deprecation is two releases: the tool remains, returns a deprecation notice in `metadata`, and its
description tells the model what to use instead ([`tools.md`](./tools.md) §2.4). Config key deprecation is
the same shape: honoured with a warning for two minors, then removed.

### 6.2 Changelog discipline

`CHANGELOG.md`, keep-a-changelog format, updated **in the PR that makes the change**, never assembled
from git log at release time — a log-derived changelog documents commits, and users need to read about
behaviour.

Sections: `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`.

`Security` entries name the invariant IDs involved and the affected version range:

```markdown
### Security
- Fixed a path-canonicalization gap where a symlink in a non-final component could place a write
  outside the writable roots (I-009). Affects 0.7.0–0.8.1. Advisory GHSA-xxxx-xxxx-xxxx.
  Regression test: `test_i009_escape_corpus_denied::non_final_component_symlink`.
```

Naming the invariant is what makes the entry actionable: a reader can go to `invariants.md`, see the
property, and see the test that now holds it. "Fixed a security issue in path handling" tells a user
nothing they can act on.

### 6.3 Artifacts

| Artifact | Built by | Notes |
|---|---|---|
| Wheel (`py3-none-any`) | `uv build` / hatchling | The primary artifact. Pure Python, no compiled extensions of our own |
| sdist | Same | Required for distribution packagers and for building on platforms without a wheel for a dependency |
| `SHA256SUMS` + signature | Release workflow | §7.3 |
| SBOM (CycloneDX JSON) | `cyclonedx-py` | §7.1 |
| Standalone binary | Deferred, optional | Honest note below |

A standalone binary (PyInstaller or similar) is attractive for a CLI and is deliberately not in the first
release. It is a 60–100 MB artifact per platform, it complicates the isolated plugin dependency
environment ([`security.md`](./security.md) §5.1) because a frozen interpreter is a poor base for
installing third-party packages, and it makes the `sqlite3` feature detection in `evaluation.md` §7.3 a
property of *our* build rather than the user's — which is arguably better, but is a different support
surface. `uv tool install kalash-code` covers the same need with none of that.

### 6.4 Platform testing before release

The full compatibility matrix (`evaluation.md` §7) runs on the release candidate: every supported
interpreter including free-threaded 3.14, every OS row, every SQLite build including the `FTS5`-absent and
extension-loading-disabled builds. Plus a per-platform install smoke test — `uv tool install` from the
built wheel, `kalash doctor`, one scripted session, one plugin install — because a green test matrix
against the source tree says nothing about whether the packaged artifact works.

### 6.5 Signing, publishing, and rollback

| Concern | Approach | Why |
|---|---|---|
| Artifact signing | **Sigstore** keyless signing via the release workflow's OIDC identity; `.sigstore` bundle published per artifact | No long-lived key for a maintainer to lose or leak, and the transparency log makes the provenance publicly checkable |
| Checksums | `SHA256SUMS`, signed | Cheap, universally verifiable, useful to distro packagers who will not adopt Sigstore |
| PyPI | **Trusted publishing** (OIDC) | A long-lived API token in CI secrets is a standing credential whose compromise publishes malicious releases under our name. Trusted publishing removes the credential entirely |
| Release candidates | `X.Y.0rc1`, minimum 7-day soak, announced for testing | The compatibility matrix cannot cover every real environment; RC users are the coverage it lacks |
| Yank policy | Yank on: a security vulnerability with no fix available, data corruption, or a broken install. **Never** for a functional bug — fix forward | Yanking breaks reproducible installs for everyone who pinned it, so it is reserved for cases where using the release is worse than being unable to install it |
| Rollback | Users pin the prior version. If a migration ran, the pre-migration backup path is in the release notes and in `kalash doctor` output | Forward-only migrations mean an older binary refuses a newer DB (§5.2). The backup is the actual rollback path, and saying so is more useful than implying downgrade works |
| Hotfix | Branch from the release tag, minimal diff, full Fast + Standard + security suites, RC skipped for a confirmed security fix | Speed matters for a security fix; a full RC cycle would leave users exposed for a week |

### 6.6 The I-040 audit

I-040 requires that no `SAFETY` or `PRIVACY` invariant rests on convention alone, and it is proven by
manual audit recorded per release. It is the one invariant with no automated test (`evaluation.md` §3),
so the audit is a release gate rather than a good intention.

Every `SAFETY` and `PRIVACY` invariant — I-001 through I-004, I-006, I-007, I-009 through I-011, I-014,
I-015, I-017, I-018, I-020, I-030 through I-036 — gets a row:

| I-0NN | Class | Enforcement | Mechanism | Verdict |
|---|---|---|---|---|
| I-003 | PRIVACY | Static | import-linter contract `egress-chokepoint` + banned-pattern scan | ✓ |
| I-009 | SAFETY | Runtime, fail-closed | Canonicalization before policy; `O_NOFOLLOW`; escape corpus | ✓ |
| I-020 | SAFETY | Architectural | Injection framing in a single chokepoint; sensitive-class records unreachable without confirmation | ✓ |
| I-040 | INTEGRITY | Review | This table | ✓ |

The verdict is ✗ when enforcement has degraded to convention — typically because a chokepoint gained a
second call path, or a static contract was relaxed to unblock something. **A ✗ blocks the release.** The
completed table goes in the release notes, so the audit is public and a reader can check the claim rather
than trust it.

The audit also re-reads the residual-risk statements in [`threat-model.md`](./threat-model.md) and
[`memory.md`](./memory.md) §23: a risk that has been mitigated is promoted, one that has grown is
restated. Stale residual-risk text is worse than none, because it is read as current.

---

## 7. Supply chain

Extends [`security.md`](./security.md) §5 with the release-time and ongoing controls.

### 7.1 SBOM

**CycloneDX JSON**, generated per release with `cyclonedx-py` from the locked environment, attached to the
release and published alongside the wheel.

CycloneDX over SPDX for three reasons: the Python tooling is more mature and produces a lockfile-accurate
component graph; it carries **VEX** (Vulnerability Exploitability eXchange) natively, which is what lets
us state "this advisory does not apply to Kalash because the vulnerable code path is unreachable" in a
machine-readable way rather than in a blog post; and most scanners consumers actually run ingest it
directly. One format, not two — publishing both doubles the surface for them to disagree.

The SBOM is **diffed against the previous release** in the pre-release stage. An unexpected new transitive
component is exactly the signal a supply-chain attack produces, and it is invisible in a `uv.lock` diff
that touches forty lines of hashes.

### 7.2 Pinning and scanning

| Control | Where | Failure mode it closes |
|---|---|---|
| `uv sync --locked` in CI and for installs | Every job | A resolution drifting from the reviewed lockfile |
| Lockfile hash verification | `tests/supply_chain/` | A mutated hash for an unchanged version |
| No floating ranges anywhere, dev dependencies included | Test asserting `pyproject.toml` has no `>=`, `~=`, or `*` in any dependency table | Dev dependencies run in CI with repository write access, so they are production dependencies for our threat model |
| Direct-dependency allowlist assertion | `tests/supply_chain/` against a reviewed list | A new dependency landing without a reviewer noticing (T-D) |
| Typosquat name check at plugin install | `plugins/loader.py` | `httpxx`, `pydantik`, `requsts` |
| Vulnerability scan **at merge** | `pip-audit` + OSV against the lockfile | A known-vulnerable version entering the tree |
| Vulnerability scan **on a daily schedule** | Same tooling, `main` and the latest release | An advisory published *after* merge. Merge-time-only scanning silently assumes advisories are known before the code lands, which is backwards |
| Native dependency justification | A comment per native dependency in `pyproject.toml` | Unexamined accumulation of the hardest-to-audit dependency class |

### 7.3 Reproducible builds

The goal, and an honest account of how far it goes.

**Our wheel is reproducible.** Pure-Python, built with `SOURCE_DATE_EPOCH` set from the release commit
date and deterministic file ordering; two builds of the same tag produce byte-identical wheels, and the
release workflow verifies this by building twice and comparing.

**A user's installed environment is not.** The dependency closure is prebuilt wheels from third parties,
some containing compiled extensions we did not build and cannot reproduce. Python packaging also gives us
no way to verify that a published wheel corresponds to its published source. Hash pinning proves we
installed exactly the bytes we reviewed; it does not prove those bytes were built from the source they
claim.

So the claim is precise: **the Kalash artifact is reproducible, the environment is pinned and
hash-verified, and the dependency closure is trusted rather than verified.** That gap is a real residual
risk (T-D) and no amount of process closes it from our side.

### 7.4 Compromised dependency response

| Step | Action | Target |
|---|---|---|
| 1 | Confirm from the advisory and the SBOM: which versions, which code path, is it reachable from Kalash | 2 hours |
| 2 | Pin or remove. Where a patched version exists, pin it; where none does, vendor a minimal patch or drop the dependency | 24 hours for a reachable critical |
| 3 | Assess exposure: what did the compromised version have access to, over which release window | With step 2 |
| 4 | Release a PATCH, advisory, and yank of affected versions if the vulnerability is unfixable in place | With step 2 |
| 5 | Notify: `SECURITY.md` channel, GitHub advisory, `CHANGELOG.md` `Security` section with invariant IDs, and a `kalash doctor` warning for known-vulnerable installed versions | With step 4 |
| 6 | Regression: a `tests/supply_chain/` case asserting the vulnerable version cannot be resolved | Same release |

Step 3 deserves emphasis. An in-process dependency runs with Kalash's full authority (T-D), so the
exposure question is "everything the user's Kalash could reach" — keyring entries, the session database,
the working tree — and the advisory must say so plainly rather than describing a narrow theoretical
impact.

### 7.5 Plugin signing and publisher verification

Designed here, **not shipping in the first release**, per [`security.md`](./security.md) §5.2.

| Element | Design |
|---|---|
| Signing | **Sigstore** keyless, over the plugin manifest and a Merkle digest of the bundle. A publisher signs from their CI using an OIDC identity — GitHub, GitLab, or Google — and the identity is what the user sees |
| Verification | At install and at every load: signature valid, bundle digest matches, identity matches the identity recorded at install. An identity change is treated as a new publisher and re-prompts |
| Trust display | `kalash plugin ls` shows the signing identity, the pinned source, the content hash, and the granted capabilities |
| Revocation | A signed revocation list fetched through the egress gateway, cached with a TTL. On fetch failure the **last known good list is retained** and staleness is warned — never treated as "no revocations", which would make a network outage into a trust bypass |
| Advisories | Plugin advisories carried in the same list: affected versions, severity, and a `kalash doctor` warning for installed matches |

Sigstore over minisign because minisign moves key management onto the publisher, and most plugin authors
are individuals who will generate one key, keep it on a laptop, and never rotate it. Keyless signing binds
a release to an account identity that already has 2FA and a recovery path, and the transparency log gives
public auditability we could not otherwise offer. The cost is a dependency on public infrastructure at
verification time, which the cached revocation list and offline-tolerant verification are designed around.

**Until signing ships, plugin trust rests on the source repository, and the install prompt says exactly
that** — naming the repository, the pinned commit, the content hash, and every requested capability with
its stated reason. Hash pinning proves the code did not change since you looked. It does not prove the
author is honest, and the prompt does not imply otherwise.

---

## 8. Documentation requirements

### 8.1 Two kinds of document

**Normative specs** (`docs/`, this tree) define required behaviour. A conflict between a spec and the code
is a bug in one of them, and the PR must resolve it rather than leave it. **User guides** explain how to
use what the specs define; they may simplify but must not contradict.

| Normative spec | Owns |
|---|---|
| `CLAUDE.md` | Repository layout, tech stack, import discipline, build order |
| `AGENT.md` | Runtime agent contract — what the agent does and does not do |
| `invariants.md` | The 40 properties, their classes, owners, and proving tests |
| `threat-model.md` | Actors T-A…T-J, assets, chains, residual risk |
| `security.md` | Egress gateway, network policy, secrets, redaction, supply chain, logging privacy |
| `capabilities.md` | Authority vocabulary, principals, attenuation, sandbox modes |
| `permissions.md` | Decision algorithm, confirmation class, grants, approval UX, sandbox enforcement |
| `tools.md` | Tool contract, lifecycle, filesystem, shell, git, web |
| `memory.md` | Memory kinds, provider protocol, KME, pipelines, egress, conformance |
| `model-gateway.md` | Normalized requests, streaming, errors, fallback, usage and cost |
| `state-machines.md` | Every state machine, the crash matrix, concurrency |
| `data-model.md` | Schema, migrations, retention, checkpoints, blobs |
| `context-budget.md` | Assembly, budgeting, compaction |
| `observability.md` | Error taxonomy, audit schema and hash chain, traces, logging |
| `evaluation.md` | Test taxonomy, security corpora, evals, retrieval harness, CI |
| `operations.md` | This document |
| `platform.md` | Per-OS specifics: sandbox backends, shells, paths |
| `interfaces.md` | CLI, TUI, SDK, HTTP surface contracts |

| User documentation | Contents |
|---|---|
| Getting started | Install, first session, the two-axis sandbox and approval model in plain language |
| Configuration reference | Every key: type, default, scope (user or project), effect. Generated from the config schema so it cannot drift |
| CLI reference | Every command and flag, with `--output-format json` shapes. Generated from the `typer` app |
| Troubleshooting | Sandbox unavailable, provider auth, `FTS5`/extension-loading absence, `SQLITE_BUSY`, stale-read errors, recovery failure — each with the diagnostic `kalash doctor` line |
| Memory provider setup | One guide per provider: credentials, scoping, what leaves the machine, how to verify with `memory audit` |
| Plugin authoring | Manifest, capabilities and `reason`, isolated dependencies, trust model, publishing |
| Skill authoring | `SKILL.md` format, progressive disclosure, `scripts/` and the `skill.script` gate |
| **Security posture for users** | What Kalash protects against and what it does not, in the user's language: the sandbox boundary, prompt injection as a permanent condition, what a memory provider receives, what the model provider sees |

The security-posture document is not marketing. Its job is calibration: a user who believes the sandbox
stops prompt injection will make worse decisions than one who understands it stops the *consequences*.

### 8.2 The same-PR rule

**A change touching a subsystem updates that subsystem's spec in the same PR.** Not a follow-up issue, not
a docs sprint. Documentation debt compounds faster than code debt and is never repaid, because the person
who knows what changed has moved on and nobody else can write it.

Enforced socially through review plus one mechanical aid: a PR touching `src/kalash/<subsystem>/` with no
change under `docs/` and no `docs-not-needed` label gets an automated comment naming the spec that most
likely needs updating. A comment, not a block — plenty of changes genuinely need no spec edit, and a hard
gate would train people to add a whitespace change to a doc.

---

## 9. Definition of Done gates

A change is done when every applicable line is true. `N/A` is a valid answer stated explicitly, never
assumed by omission.

```markdown
- [ ] Unit tests cover the new behaviour AND its failure mode
- [ ] Integration tests where a subsystem seam changed
- [ ] Regression test referencing the issue, for a bug fix
- [ ] Security tests updated where an actor's surface changed (T-A…T-J)
- [ ] `mypy --strict src/kalash` clean
- [ ] `ruff format --check` and `ruff check` clean
- [ ] Import-linter contracts and the banned-pattern scan pass
- [ ] Migration test from every prior schema version, if the schema changed (I-022)
- [ ] Backward-compatibility test, if a protocol version changed (§5)
- [ ] Performance regression check against the §1 gate for any touched metric
- [ ] Audit and logging verified: new decisions produce audit rows; new sinks pass the
      I-035 corpus test; log tiers respected (`security.md` §6)
- [ ] Docs updated in this PR (§8.2)
- [ ] CHANGELOG.md entry, in the right section
- [ ] **Invariant impact stated: which I-0NN IDs this affects, and how each remains enforced**
- [ ] **Threat-model impact assessed: which actors' surface this changes, or none**
- [ ] Prompt version bumped and snapshot updated, if a prompt changed (§3)
- [ ] Eval delta reported, if agent behaviour could plausibly move (`evaluation.md` §8)
```

The two bold lines are the ones that matter most and the ones most often skipped.

**"No security impact" is an acceptable answer when it is true, and it must be written.** Omission is not
an answer, because omission is indistinguishable from not having thought about it — and the reviewer
cannot tell which. A PR that says "touches `tools/fs.py`; I-008 and I-012 still enforced by the atomic
write path and the digest check; no change to I-009 canonicalization" gives a reviewer somewhere to aim.
A PR that says nothing gives them a diff and a hope.

Changes touching the modules in [`security.md`](./security.md) §9 need explicit security review beyond
normal code review, and that list is authoritative.

---

## 10. On-call and incident response

Kalash is an open-source, single-user, local-first tool. There is no on-call rotation, no pager, and no
production to page about. What exists is a commitment about response, and it is deliberately modest so it
can actually be kept — an overstated SLA that is missed is worse than an honest one that is met.

### 10.1 Reporting

Per [`security.md`](./security.md) §8: `SECURITY.md` at the repository root with a private channel
(GitHub private vulnerability reporting, plus an email address). No public issues for unpatched
vulnerabilities. Coordinated disclosure timeline stated up front so a reporter knows what to expect.

### 10.2 Severity and response

| Severity | Definition | Ack | Fix or mitigation |
|---|---|---|---|
| **Critical** | Remote or repository-triggered code execution; credential disclosure; any break of a `SAFETY` or `PRIVACY` invariant reachable without user error | 24 h | 7 days, or a documented mitigation plus an advisory within 7 days |
| **High** | Sandbox escape requiring an unusual configuration; data corruption; an `INTEGRITY` invariant break | 48 h | 30 days |
| **Medium** | Denial of service; information disclosure limited to the local user; a break requiring an implausible precondition | 5 days | Next minor |
| **Low** | Hardening gap with no demonstrated impact; a defence-in-depth layer missing behind a working one | 10 days | Backlog, tracked publicly |

Severity is assigned by which invariant class breaks and how reachable the break is, not by how alarming
the report sounds. A `SAFETY` invariant reachable by cloning a repository is Critical no matter how simple
the bug; a theoretical bypass requiring `danger-full-access` plus a hostile local process is Low no matter
how sophisticated the write-up.

### 10.3 Advisory format

Published as a GitHub Security Advisory with a CVE where applicable, containing: affected version range,
fixed version, **the invariant IDs broken**, the threat actor it maps to, severity with reasoning,
required preconditions, impact, workaround if any, credit, and the regression test that now covers it.

### 10.4 Fix requirements

Every security fix ships with **a regression test that references the invariant it restores** — the same
`test_i0NN_*` naming that the I-039 meta-test collects (`evaluation.md` §3). A fix without a test is a fix
that will be undone by the next refactor, and the person who undoes it will have no way to know.

If the fix reveals that an invariant was enforced only by convention, the I-040 audit row for that
invariant is updated in the same release and the enforcement is strengthened — architecturally,
statically, or fail-closed at runtime — rather than patched at the one call site where it was noticed.

---

*See also: [`evaluation.md`](./evaluation.md) for how every number here is measured and which suites gate
merges versus releases, [`invariants.md`](./invariants.md) for I-037 and I-040,
[`security.md`](./security.md) §5 for the supply-chain controls this document extends and §8 for
disclosure, [`threat-model.md`](./threat-model.md) T-D and T-G for the threats signing and SBOM address,
[`capabilities.md`](./capabilities.md) §9 for capability protocol changes,
[`memory.md`](./memory.md) §20 for the memory provider protocol this matrix versions,
[`model-gateway.md`](./model-gateway.md) §8 for the pricing registry recorded per turn, and
[`context-budget.md`](./context-budget.md) for the per-agent budgets these system limits sit beside.*
