# Kalash coding session handoff

## Recovery and competitive-design update (2026-10-02)

See `docs/HARNESS_DESIGN.md` for the architecture decision, implemented limits,
ordered readiness gates and reproducible comparison protocol. The user wants the
strongest harness; no universal or measured superiority claim is justified.
Benchmarking remains paused; no commit or push was made.

SQLite runtime operations now own connections; mixed writers serialize through
SQLite, cancellation rolls back/closes owned writes, and migration DDL/receipts
commit atomically through one sync implementation. The old private bootstrap
handle remains for compatibility but no runtime callers use it.

ToolExecutor settles each result durably before the next dependent write. A read
receipt failure cancels/joins siblings. Abrupt subprocess exits test before effect,
after effect without receipt, and after first receipt in a two-call batch. Resume
repairs unknown results without replaying effects; exactly-once and durable event
reduction remain unavailable. WAL/NORMAL is not a power-loss durability proof.

`core/privacy.py` scrubs ordinary observations before host deferral, scratchpad,
transcript, outgoing request and event subscribers. Effects retain original args.
Known environment credential values and complete/incomplete PEM bodies have
canary tests. Opaque provider blocks, images, split streaming chunks, raw logs,
readable credential files and trusted host extensions remain security gaps.

Built-in shell/write/edit/multi_edit results carry runtime evidence; serialization
and trimming retain it. `runtime/evidence.py` preserves up to 32 receipts / 6k JSON
characters outside generated summaries, with omission counts. Exit codes are
command outcomes, not proof of correctness. Full traces retain older evidence.

Current verification: 663 tests pass in 29.33s (20 new regression cases); strict
mypy passes 162 source files; F/I/UP, formatting (236 files) and whitespace pass.
Wheel/source distribution build passes; wheel contains the new privacy/evidence
modules, discovery/artifact CLI and all 12 skills, without bytecode. Full configured
Ruff still has 713 repository findings; tracked changes introduce none and new
modules/tests pass all configured rules. These are local regression results, not
real-model quality or a comparative benchmark.

## Capability-loading update (2026-10-02)

The working tree now adds 12 packaged workflow skills (filesystem, shell,
code, Git, web, PDF, documents, spreadsheets, presentations, images, archive
and data), seven individually loadable recipe references, and optional artifact
parsers under the `artifacts` extra. See `docs/CAPABILITIES.md` for support limits.
Project/user/plugin definitions override bundled defaults; discovery remains
metadata-only. The skill tool can read a bounded resource range and rejects
traversal and symlinks outside the selected skill.

`tool_search` browses permission-filtered extension metadata and activates up
to five selected plugin/MCP schemas per call. Core tools remain immediately
available. Activated schemas persist through compaction and are restored from
history on resume without replaying effects. Network-disabled tools are hidden
from the request as well as refused at execution. Recorded skill bodies are
retained after resume, and resource reads do not overwrite their bodies.

`python -m kalash.artifacts` inspects selected pages/paragraphs/slides/rows or
archive members with source hashes and output limits. Run it through the shell
sandbox; it does not extract archives or mutate files. Office XML/archive and
compressed-TAR limits are tested. PDF text is not OCR, image metadata is not
vision, and openpyxl does not recalculate formulas. Rendering/visual QA still
needs installed converters and a connected vision integration.

Verification: 643 tests passed; strict mypy passed for 160 source files;
F/I/UP checks and formatting passed; wheel/source distribution built and wheel
contents verified (12 skills, seven references, artifact CLI and discovery).
Full configured Ruff still fails on the pre-existing repository lint backlog.
New modules/tests pass configured rules. Existing integration tests require
execution outside this workspace sandbox for SQLite/process access.

A synthetic 100-extension fixture estimates 32,613 eager schema tokens versus
4,160 with one extension loaded (87.2% less schema context); this is not a live
benchmark or evidence of superior task performance. TURN_START now exposes
schema count and estimated schema tokens. MCP transport startup remains eager,
and activated schemas have no eviction policy. Benchmarking remains paused;
the readiness priorities below still apply. No commit or push was made.

## Start here

Continue the harness cleanup and hardening described below. Read this file,
[the review](docs/HARNESS_REVIEW.md), [the architecture](docs/ARCHITECTURE.md),
and `coding-agent-harness-builder/SKILL.md` before changing the runtime.
Read the skill's relevant references as needed. Check git status and recent
commits first; preserve any changes made after this handoff.

At handoff, the completed refactor is commit **d79d09b**:
`refactor: simplify harness runtime and memory infrastructure`.
The working tree was clean before adding this handoff. Commit/push actions
are handled by the user unless separately requested.

## What the user wants

- A small, understandable, strong coding-agent harness. Remove dead code and
  misleading implementations; split files by real responsibilities.
- Thoroughly review memory, prompt construction/loaders, skills, subagents,
  MCP configuration, context management and issues discovered beyond that list.
- Reach the quality of the **official DeepSeek Harness** eventually. This means
  comparing harnesses, not adding a DeepSeek model integration.
- Sarvam is a requested provider. The user selected **glm5.3**, requiring V2
  access. An API key is reportedly available, but working V2 access is unverified.
- **Finish the cleanup and readiness work before benchmarking. Benchmarking is
  paused. Do not jump straight to a live run or claim benchmark parity.**

## Completed work to preserve

- Removed TPM pacing, request shrinking, tool profiles and forced synthesis.
  TPM error diagnostics remain informational. Run token/cost/time/turn/tool
  ceilings remain; they are not TPM policy.
- Split runtime request/execution/history/transcript/delegation, filesystem
  operations, memory SQL/store/records/schema and TUI commands/events/formatting.
- Removed fake embeddings, mem0, unused vectors/ledger/extraction queues,
  duplicate orchestration, unused plugins/checkpoints, broken rewind and
  fabricated comparison/benchmark scores. Kept old database migrations for
  compatibility.
- Fixed project instruction discovery and precedence, source/scope labels,
  subtree guidance before mutation, progressive skill loading and retention
  after compaction. Both AGENTS.md and KALASH.md are supported.
- Isolated child context/session/scratchpad; inherited provider, permissions,
  tools and extension policy; charged child usage to the parent.
- Preserved tool errors and closed tool exchanges; counted attempted calls;
  consumed provider reasoning/usage tails; charged compaction; retained user
  constraints and retrievable oversized observations.
- Made memory configuration-aware and scoped; passed owning configuration into
  memory tools; bounded recall/capture; kept transactional SQLite FTS.
- Added configurable MCP command/args/env/headers, HTTP initialization/session
  handling, legacy SSE startup handshake, EOF failure and cleanup.
- Added hash trust for executable project configuration, safer subprocess env,
  fail-closed shell sandboxing, permission/network enforcement, process cleanup
  and bounded hook-output retention.
- Added Sarvam V1/V2 adapter and mocked SDK wire tests. glm5.3 defaults to max
  reasoning and 32,768 output tokens. No direct DeepSeek adapter was added.
- Added runtime CI checks and updated README/review/architecture documentation.

Source went from 35,954 to 30,219 Python lines, including newly split modules:
5,735 fewer lines (~16%). Memory went from 4,336 to 2,302 lines (~47% less).

## Last verified results

These are results from the completed refactor, not a new verification of future
changes:

- Full pytest: **617 passed in 17.12s**.
- Strict mypy: passed for 156 source files.
- Ruff F/I/UP checks: passed. **Full configured Ruff still fails.**
- Formatting: all 204 checked files formatted; git diff whitespace check passed.
- Wheel and source distribution built; wheel checked for new modules and absence
  of removed implementations.
- Scripted 300-step test passed in a 32k context with repeated compaction,
  retained user constraints and valid tool-call/result pairs. This is a runtime
  regression test, not evidence of real-model task quality or summary fidelity.
- No performance benchmark ran during the final restructuring verification.
  An earlier live attempt failed authentication and produced no quality score.

## Next coding priorities

1. **Crash recovery and storage:** inspect transcript persistence/resume and mixed
   synchronous/asynchronous SQLite access. Add effect-boundary crash tests and
   fix demonstrated failures. Unknown outcomes must never automatically replay
   mutations. Do not claim event sourcing or exactly-once effects without proof.
2. **Security:** expand shell/path/network/injection tests. Verify planted secrets
   cannot reach model requests, events or artifacts. Shell classification is
   still heuristic; hooks/MCP have host authority, and readable credential files
   remain a concern. Use disposable isolation for hostile evaluation workloads.
3. **Full lint:** run the actual configured Ruff rules and fix findings deliberately.
   Do not disable rules broadly or describe F/I/UP as full lint success.
4. **Context quality:** test preservation of decisions, file state and verification
   evidence, including malformed/truncated summaries. Character-based token
   estimation and scripted summary tests do not establish semantic fidelity.
5. **Lifecycle/extensions:** address memory service cache eviction/ownership,
   scratchpad cache/index and regex resource limits, and real-server MCP SSE
   interoperability. OAuth automation is explicitly unavailable.
6. **Provider/configuration:** validate capabilities and prices for other adapters.
   Unknown compatible-provider prices currently become zero, weakening USD
   ceilings. Configured model fallback is not wired into Agent construction.
   Sarvam needs verified V2 access and explicit currency conversion for USD costs.
7. **Evaluation, after readiness:** integrate a pinned public dataset/environment
   and its official grader. Existing small fixtures prove mechanics only. Compare
   harnesses under the same model/tasks/tools/resources; never grade the agent's
   own completion claim or advertise an unmeasured DeepSeek-level score.

Keep fixes proportionate and understandable. Prefer deleting unsupported
facades to adding speculative infrastructure. The detailed review distinguishes
implemented behavior from unresolved production proofs.

## Useful files and commands

Start with `src/kalash/runtime/`, `src/kalash/storage/`,
`src/kalash/permissions/`, `src/kalash/sandbox/`, `src/kalash/memory/`,
`src/kalash/mcp/`, and `tests/unit/test_harness_contracts.py`.
CI is `.github/workflows/checks.yml`; it has no model-quality regression gate.
Evaluation scaffolding is in `evals/benchmark*.py`; do not run it yet.

```bash
git status --short
uv sync --all-extras
.venv/bin/python -m pytest -q <focused-test-path>
.venv/bin/python -m pytest -q
.venv/bin/mypy src
.venv/bin/ruff check src tests evals
.venv/bin/ruff format --check src tests evals
git diff --check
uv build
```

Subprocess/SQLite tests previously needed execution outside the restricted
sandbox. Request the appropriate tool escalation if that restriction recurs.
Keep keys in local config/environment; never print or commit credentials.
Tests isolate KALASH_HOME and API-key environment variables to avoid real auth.

## Suggested next-session prompt

> Read NEXT_SESSION.md and continue the remaining harness cleanup and hardening.
> Keep benchmarking paused. Start with crash/storage and security gaps, then
> complete the remaining readiness checks. Keep the implementation small and
> report tested behavior separately from claims that remain unproved.
