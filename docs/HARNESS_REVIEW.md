# Harness review and restructuring

Reviewed against `coding-agent-harness-builder/SKILL.md` and its context,
extensibility, safety and evaluation contracts. The reference is the
[official DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness).
The selected evaluation model is Sarvam `glm5.3`. Source review and scripted
tests do not establish benchmark parity or production readiness.

## Original gaps and changes

| Original gap | Change | Verification |
| --- | --- | --- |
| TPM policies shrink requests, remove tools and force synthesis | Removed pacing, request shrinking, iteration profiles and synthetic completion; errors cannot degrade the request | Gateway and model-limit tests |
| Large prompt builder mixes discovery and behavior | Separate static identity/workflow, hierarchical discovery and context assembly; source/scope/precedence explicit; both AGENTS.md and KALASH.md load | Prompt and guidance tests |
| History cuts split tool exchanges and lose constraints | Retain closed exchanges and user text; account for semantic summaries; bound observations with retrievable scratchpad bodies | Compaction and 300-step context tests |
| Skills/agents use fragile parsing and wrong discovery paths | Safe YAML, metadata catalog, on-demand bodies, bounded references and retained loaded skills | Loader and skill-retention tests |
| Duplicate subagent systems and shared child state | Delete obsolete orchestration; one isolated child path, inherited provider/policy, restricted tools and charged usage | Delegation contracts |
| Flattened tool errors, ignored timeouts and uncounted attempts | Preserve is_error, count attempts, enforce timeouts, parallelize reads and serialize mutations | Loop and registry tests |
| Sandbox silently executes on host; commands inherit API credentials | Require actual OS sandbox unless full access selected; safe subprocess environment; process-group cleanup | Shell security tests |
| Network retrieval bypasses deny rules; approval config ignored | Apply policy to retrieval and enforce configured network and never/untrusted/on-request modes | Permission contracts |
| Memory config/scope/isolation failures | Configuration-aware services, owning config passed into memory tools, scoped SQL before ranking and scoped deletion | Memory and service tests |
| Fake embeddings, mem0, unused vectors/ledger and extraction queues | Remove them and their dependencies; keep bounded fact capture/recall and transactional SQLite FTS | Memory tests and build |
| Incomplete MCP config, initialization and cleanup | Command/args, env/header references, asynchronous setup, HTTP sessions, SSE handshake/notifications, EOF failures and owned disconnect | HTTP wire and lifecycle tests; SSE startup unit test |
| Project executables implicitly trusted | Persist hash pins for hooks/MCP; changed hook configuration cannot execute; drain hook output with bounded retention | Trust and hook tests |
| Provider metadata and credentials owned by TUI | Move ownership to models; retain thin UI compatibility imports | Resolution, CLI and auth tests |
| Loop, filesystem, memory and TUI modules mix responsibilities | Split request/execution/history/transcript/delegation, individual file tools, memory SQL, and UI commands/events/formatting | Full suite and mypy |
| Placeholder plugins/checkpoints, broken rewind and fabricated scores | Delete unused implementations, unsafe conversation slicing, fake memory facades, hardcoded percentages and always-green scorecards | Source review and CLI help |
| Client resources leak; schedules report failure as success | Close owned providers/MCP/processes, unsubscribe events, serialize sends, reset runtime on /new and report actual termination | Lifecycle, scheduler, CLI and TUI tests |

## Structure and size

See [ARCHITECTURE.md](ARCHITECTURE.md) for the file responsibilities and runtime
path. Counts include new modules, so moving code does not count as deletion.
Old database migration tables remain for compatibility. Existing user review
documents and custom agent definitions are preserved.

| Python source | Before | After |
| --- | ---: | ---: |
| Total lines | 35,954 | 30,219 |
| Memory lines | 4,336 | 2,302 |
| Main loop | 1,166 | 644 |
| Static prompt module | 981 | 172 |
| TUI application | 1,652 | 1,205 |

Deleted: runtime/shrink.py, runtime/checkpoint.py, orchestration/, plugins/,
hooks/trust.py, memory/ledger.py, memory/pipeline/dedupe.py, the mem0 provider
and unused skill index. Filesystem operations now have individual tools/fs/ modules.

TPM detection remains only to explain provider errors. It cannot remove tools,
instructions, reasoning or output allowance. Run token/cost/time/turn/tool limits
remain bounds on execution; they are separate from provider throughput policy.

## Package review and remaining gaps

| Area | Current boundary and outstanding concern |
| --- | --- |
| core/config | Value types and supplied configuration. Removed unused concurrency setting. Configured model fallback is not wired into Agent construction. |
| runtime | One loop and delegation path. Unknown effects are retained on interruption without replaying mutations. Per-tool receipts and abrupt-exit recovery have regression coverage; durable event reduction remains unimplemented. |
| models | Sarvam V1/V2 wire tests, max reasoning and 32,768 output default. Static capabilities/prices for other providers need validation. Unknown compatible pricing is zero, making dollar ceilings unreliable there. |
| tools | Dedicated file tools, errors/timeouts and bounded model observations. Scratchpad cache/index and regex searches still need hostile-input resource limits. |
| permissions/sandbox | Fail-closed shell execution and restricted environment. Shell classification remains heuristic; readable host credentials and extension authority still need adversarial proof. |
| memory | SQLite FTS and configured provider protocol. Configuration-aware service caching has no eviction/refcount lifecycle for long-lived multi-project services. No vector/graph quality claim. |
| storage | Atomic transcripts and compatible migrations. Mixed sync/async writers now own connections; isolation, cancellation and migration rollback have regression tests. No exactly-once external-effect claim. |
| agents/skills | Proper discovery and progressive loading. Delegated named agents inherit the parent model; explicit model overrides use standalone execution. |
| hooks/MCP | Optional, configurable, hash-trusted extensions with cleanup. They execute with host authority rather than tool sandbox authority. OAuth automation is explicitly unavailable. SSE needs real-server interoperability coverage. |
| scheduler | Configured budgets/permissions and honest termination. Multi-worker service isolation is outside the tested local boundary. |
| CLI/TUI/SDK | Shared runtime and owned cleanup. TUI is smaller but substantial. Broken rewind was removed; filesystem restore and durable branching/replay are unavailable. |
| tests/evals | Runtime and wire contracts, plus independent grading mechanics. Small smoke fixtures are not a representative public benchmark. |

## Verification and benchmark gates

Final local verification: **617 tests passed in 17.12s**; strict mypy passed
for 156 source files; F/I/UP lint passed; all 204 checked files are formatted;
`git diff --check` passed. Wheel and source distribution built successfully.
The wheel includes the new runtime/Sarvam modules and excludes deleted systems.
Source decreased by **5,735 lines (16.0%)**, including all newly split modules.

CI runs regressions, mypy, formatting, core lint contracts and packaging. These
commands verify changes without calling a live model or running a benchmark:

```bash
.venv/bin/python -m pytest -q
.venv/bin/mypy src
.venv/bin/ruff check src tests evals --select F,I,UP
.venv/bin/ruff format --check src tests evals
git diff --check
uv build
```

The 300-step scripted loop repeatedly compacts within a 32k window, preserves
the original constraint and checks tool-call/result pairing on every request.
It proves these mechanics, not real-model summary fidelity or task success.

The full configured Ruff rule set is not clean: style/interface debt,
private-access warnings and security findings need individual review. Passing
the selected contracts is not passing full Ruff. Global rules were not weakened.

Before a performance claim:

1. Kill the runtime at effect boundaries and prove recovery without duplicated
   mutations; add event reducer/replay proofs before claiming event sourcing.
2. Expand shell/path/network/injection cases and prove planted credentials absent
   from model requests, events and artifacts. Env isolation alone is insufficient.
   Use disposable container isolation for hostile repositories/public workloads.
3. Validate decisions, file state and verification evidence through repeated real
   model compactions. Token estimates remain character-based.
4. Verify actual Sarvam V2 access/usage and an explicit INR-to-USD conversion for
   USD budgets; validate other providers' limits/prices before comparison.
5. Integrate a pinned public dataset/environment and its official grader. Compare
   harnesses with the same model, tasks, tools and resource constraints.
6. Resolve full Ruff findings and service/storage lifecycle risks, then gate them
   in CI. The current CI does not contain a model-quality regression gate.

Benchmarking remains paused. No performance benchmark was executed during this
restructuring verification. Sarvam's
[GLM-5.3 contract](https://docs.sarvam.ai/api/getting-started/models/openweight/glm-5-3)
documents the selected model's separate access and API requirements.

## Recovery hardening update (2026-10-02)

Owned SQLite connections replace runtime shared-connection access; synchronous
and asynchronous migrations now use one atomic implementation. Each settled tool
result is persisted before the next dependent write, and failed read receipts
cancel/join siblings. Abrupt subprocess-exit tests cover before effects, after an
effect without a receipt, and after the first receipt of a two-call batch.

Observation, scratchpad, transcript, request and event boundaries apply secret
scrubbing. Runtime receipts preserve built-in command exit codes and file facts
outside semantic summaries. See [HARNESS_DESIGN.md](HARNESS_DESIGN.md) for precise
limits: opaque provider blocks, split streaming chunks, credential-file admission,
host extensions, power-loss durability and independent completion verification
remain unresolved. The update does not establish public benchmark performance.
