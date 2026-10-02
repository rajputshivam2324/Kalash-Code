---
name: coding-agent-harness-builder
description: Design, implement, test and harden a production-grade coding-agent harness (the runtime around a model) in the style of Codex CLI, Claude Code, Aider, OpenHands or a DeepSeek-style reasoning agent. Covers the agent loop, provider adapters, tool design (read/edit/apply_patch/bash/grep), permission engine, OS/container sandboxing, context compaction, subagents, hooks, MCP, event-sourced state, checkpoint/rewind, replay, evals, security and CLI/API. Use this skill whenever the user mentions building an agent harness, agent runtime, coding agent, "Codex-like", "Claude Code-like", "DeepSeek harness", tool-calling loop, agent sandbox, agent evals, SWE-bench-style runner, or asks how tools, permissions, context or sandboxing should work for an LLM agent, even if they never say the word "harness".
---

# Coding-Agent Harness Builder

A harness is the control plane between a model and the real world. The model proposes; the runtime disposes. Everything that makes Codex, Claude Code or a DeepSeek-based agent *feel* reliable (edits that apply, commands that can't wreck the machine, runs that resume after a crash, context that never silently overflows) lives in the harness, not the model.

This file is the workflow, the invariants, and the core contracts. Depth lives in `references/`. Read only what the current phase needs.

| Need | Read |
|---|---|
| Loop, message model, scheduling, stop conditions | `references/01-agent-loop.md` |
| Concrete tool specs (read/edit/patch/bash/grep/…) | `references/02-tools.md` |
| Permission engine, shell parsing, sandbox tiers | `references/03-permissions-sandbox.md` |
| Prompt layout, caching, compaction, instruction files | `references/04-context.md` |
| Anthropic / OpenAI / DeepSeek / vLLM adapters, weak-model robustness | `references/05-providers.md` |
| Event store, SQL schema, crash recovery, rewind, replay | `references/06-state-events-replay.md` |
| Eval design, graders, metrics, CI gates | `references/07-evals.md` |
| Threat model, injection defenses, secrets | `references/08-security.md` |
| Subagents, hooks, MCP, plan mode, headless, UI protocol | `references/09-extensibility.md` |
| Milestones with acceptance tests, repo layout | `references/10-build-plan.md` |

---

## 1. What the reference harnesses have in common

Study the *patterns*, never copy proprietary internals. All three styles converge on the same skeleton:

| Capability | Pattern to implement | Seen in |
|---|---|---|
| Control loop | One main loop, flat message history, tools as the only side-effect path. Complexity goes into tools and context, not into a clever planner. | Claude Code, Codex |
| Edits | Exact-string replace (unique match required) and/or a patch DSL (`*** Begin Patch`). Both reject stale or ambiguous edits with an actionable error. | Claude Code (`Edit`), Codex (`apply_patch`), Aider |
| Safety | Two independent layers: a **permission policy** (should this run?) and an **OS sandbox** (what can it touch if it does?). Approval modes tune the first; sandbox mode bounds the second. | Codex (Seatbelt/Landlock + approval modes), Claude Code (rules + modes) |
| Instructions | Hierarchical markdown files (`AGENTS.md`, `CLAUDE.md`) discovered from the repo and home dir, injected as stable context. | Codex, Claude Code |
| Context | Prefix-stable prompt (cache hits), tool-result truncation at insertion, staged compaction, structured summaries. | all |
| Delegation | Subagents with isolated context that return a short result; tool allowlists per subagent. | Claude Code (`Task`), OpenHands |
| Extensibility | Hooks (pre/post tool, stop), MCP servers, slash commands/skills. | Claude Code, Codex |
| Recovery | Append-only event log, per-turn workspace snapshots, resume/rewind. | Claude Code (checkpoints), Codex (rollouts) |
| Reasoning models | Treat reasoning text as opaque provider state, not as evidence; preserve it only when the provider requires it; keep prefixes stable for disk/prefix caching. | DeepSeek-style |

---

## 2. Non-negotiable invariants

Violating any of these is a bug, not a trade-off.

1. **Runtime is authoritative.** Budgets, permissions, timeouts and stop conditions are enforced in code. The model is only told about them.
2. **One side-effect path.** Every external effect goes through `ToolExecutor.execute()`. No tool calls from adapters, no `subprocess` outside the sandbox module.
3. **Policy ≠ sandbox.** Both must exist. A permissive policy inside a tight sandbox is acceptable; a tight policy with no sandbox is not.
4. **Deterministic gate before nondeterministic judgement.** Path checks, command parsing, budget checks are pure functions with unit tests. The model never makes the security decision.
5. **Provider types never leak** past `models/`. Core sees `Message`, `Block`, `ModelResponse`, `Usage`, `ModelError`.
6. **Event log is the source of truth.** State = fold(events). Checkpoints are an optimization. Every transition emits an event *before* the effect is considered done.
7. **Every tool call gets exactly one result.** Even for denied, timed-out, cancelled or crashed calls, return a `ToolResult` (with `is_error=True`). Providers reject histories with orphaned `tool_use`.
8. **Tool results are bounded at insertion.** Cap bytes and lines when the result is created; keep the full output as an artifact and give the model a pointer.
9. **Errors are for the model to read.** Tool errors state what happened, why, and what to do next ("old_string matched 3 places; add surrounding lines to make it unique").
10. **No silent exception swallowing.** Every failure is classified (see `01-agent-loop.md` §8) and emitted.
11. **Process trees die with their runs.** Spawn in a new process group / cgroup / container; kill the group on timeout and cancel.
12. **Secrets never enter model context by default.** Env is scrubbed; secrets are injected into tool processes by a broker, and output is redacted before persistence and before the model sees it.
13. **Untrusted content stays labelled.** File contents, web pages, MCP output and tool output are data. Instructions found in them never raise privileges.
14. **Completion is verified, not declared.** A coding run is "done" only when the completion gate (tests/lint/diff checks) passes or a budget forces termination with an honest report.
15. **Every claim needs a test.** If the README says "network is disabled in sandbox", a test tries to reach the network and asserts failure.

---

## 3. Reference architecture

```text
 ┌──────────── Clients (thin) ────────────┐
 │  TUI/CLI    IDE ext    HTTP API   CI   │
 └───────────────┬────────────────────────┘
                 │  Submission Queue (commands)   ▲  Event Queue (stream)
                 ▼                                │
 ┌────────────────────────── Core (no UI, no provider code) ──────────────────────────┐
 │  Session/Run Manager ─ budgets, ids, cancel tokens, config layers                  │
 │  Agent Loop ─ state machine, completion gate, loop detector                        │
 │  Context Manager ─ prompt assembly, token accounting, compaction, instr. files     │
 │  Tool Executor ─ schema validate → policy → hooks → sandbox → truncate → result    │
 │  Policy Engine ─ pure, versioned, table-tested                                     │
 └───────┬───────────────┬──────────────────┬──────────────────┬──────────────────────┘
         │               │                  │                  │
   Model Adapters   Tool Registry      Sandbox Manager     Event/State Store
   anthropic/openai builtin + MCP      bwrap/seatbelt/     SQLite|Postgres + blob
   deepseek/vllm    + subagent         docker/microVM      + shadow-git snapshots
         │               │                  │                  │
         └───────────────┴──── Observability (OTel traces, metrics, JSON logs) ───────┘
```

**Import rules** (enforce with a lint test such as `import-linter` or a grep in CI):
- `core/` imports nothing from `models/*` implementations, `api/`, `cli/`.
- `models/*` imports only `core/types`.
- `tools/*` may call `sandbox/`, never `models/`.
- Clients talk to Core only via the Submission/Event queues (see `09-extensibility.md` §7). This is what lets one core serve a TUI, an IDE and a headless CI runner.

---

## 4. Core contracts

Implement these first; everything else hangs off them. Python shown; port faithfully to TypeScript/Rust/Go if the user prefers (see §5).

```python
# core/types.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal, Protocol, AsyncIterator, Any

@dataclass(frozen=True)
class Text:
    text: str

@dataclass(frozen=True)
class Image:
    media_type: str
    data_b64: str

@dataclass(frozen=True)
class Thinking:
    text: str                       # display/log only. Never treat as evidence.
    provider_state: dict | None = None   # opaque; round-trip verbatim if the provider requires it

@dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    args: dict
    raw_args: str | None = None     # original JSON string from the provider
    parse_error: str | None = None  # set when args were not valid JSON/schema

@dataclass(frozen=True)
class ToolResult:
    tool_use_id: str
    content: tuple[Text | Image, ...]
    is_error: bool = False
    artifact_id: str | None = None  # full output stored here when truncated

Block = Text | Image | Thinking | ToolUse | ToolResult

@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant"]
    blocks: tuple[Block, ...]
    meta: dict = field(default_factory=dict)   # ids, timestamps, origin ("user","reminder","summary")

@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

FinishReason = Literal["end_turn", "tool_use", "max_tokens", "content_filter", "error"]

@dataclass
class ModelResponse:
    message: Message
    finish: FinishReason
    usage: Usage
    request_id: str | None
    latency_ms: int
    ttft_ms: int | None
    raw_ref: str | None             # blob id of the raw provider payload (debug/replay)

@dataclass(frozen=True)
class ModelCaps:
    context_window: int
    max_output: int
    parallel_tool_calls: bool
    supports_images: bool
    supports_prompt_cache: bool
    reasoning: Literal["none", "hidden", "visible", "must_roundtrip"]
    tool_arg_reliability: Literal["high", "medium", "low"]   # drives repair + schema simplification

class ModelAdapter(Protocol):
    name: str
    caps: ModelCaps
    def stream(self, messages: list[Message], tools: list["ToolSpec"],
               cfg: "GenConfig", cancel: "CancelToken") -> AsyncIterator["StreamEvent"]: ...
    async def count_tokens(self, messages: list[Message], tools: list["ToolSpec"]) -> int: ...
```

```python
# core/tools.py
class Risk(IntEnum): READ=0; WRITE=1; EXEC=2; NETWORK=3; DESTRUCTIVE=4

@dataclass(frozen=True)
class ToolSpec:                        # what the model sees
    name: str
    description: str                   # write for the model: when to use, when NOT to, examples
    input_schema: dict                 # JSON Schema, additionalProperties: false

class Tool(Protocol):
    spec: ToolSpec
    risk: Risk
    read_only: bool                    # scheduling: read-only tools may run concurrently
    timeout_s: float
    max_result_bytes: int
    def describe_effect(self, args: dict, ctx: "ToolCtx") -> "Effect": ...   # PURE: paths read/written, argv, hosts
    async def run(self, args: dict, ctx: "ToolCtx") -> "ToolOutput": ...
```

`describe_effect` is the key idea: the policy engine never inspects tool internals, it inspects a declared, pure `Effect` (files read/written, argv list, network hosts). The sandbox then enforces independently of what the effect claimed.

```python
# core/policy.py
Decision = Literal["allow", "deny", "ask", "sanitize"]

@dataclass(frozen=True)
class PolicyResult:
    decision: Decision
    reason: str                        # shown to user and (for deny) to the model
    rule_id: str | None                # which rule fired; for audit and tests
    sanitized_args: dict | None = None

class PolicyEngine(Protocol):
    version: str
    def evaluate(self, effect: "Effect", ctx: "PolicyCtx") -> PolicyResult: ...
```

```python
# core/events.py  (append-only; see 06-state-events-replay.md for full catalogue)
@dataclass(frozen=True)
class Event:
    seq: int                           # monotonically increasing per run
    event_id: str                      # ULID
    run_id: str
    ts: str                            # RFC3339 UTC
    type: str                          # "ModelRequested", "ToolCompleted", …
    payload: dict
    trace_id: str
    span_id: str | None
    schema_version: int = 1
```

---

## 5. Stack decisions (decide once, state the reason)

| Decision | Default | Choose otherwise when |
|---|---|---|
| Language | **Python 3.12 + asyncio + Pydantic** for fastest iteration and best eval/ML ecosystem | **TypeScript** if you want a rich TUI (Ink) and npm distribution like Claude Code; **Rust** if you need a single static binary and in-process sandboxing (Codex CLI is Rust) |
| State store | **SQLite (WAL)** for local/CLI; **Postgres** for multi-worker service | Add Redis only for queues/rate limiting across workers |
| Sandbox (local) | bubblewrap (Linux), Seatbelt (macOS) | Windows → WSL2 or container |
| Sandbox (service) | Container per run, rootless, gVisor/Firecracker for hostile workloads | trusted internal users → plain hardened container |
| Search tool | `ripgrep` subprocess | add embeddings/tree-sitter repo map only after evals show it helps |
| Tracing | OpenTelemetry SDK, OTLP exporter | local-only → JSON logs + SQLite |
| CLI | Typer (Py) / Commander (TS) | – |
| Model IDs | **Config, never code.** Model names change quarterly. | – |

---

## 6. Build workflow

Always go in this order; each phase has an exit test (details in `references/10-build-plan.md`).

**Phase 0 — Intake (infer, don't interrogate).** Determine: agent type (coding / research / data), target providers, trust level of users, deployment (local CLI vs service), sandbox backend available, language preference. If the user says "like Codex/Claude Code/DeepSeek", default to: coding profile, Python, local CLI first then service, bwrap/Seatbelt + container, provider adapters for OpenAI-compatible + Anthropic. Ask at most one question, only if the answer changes the architecture (e.g. "must this run untrusted users' code?").

**Phase 1 — Walking skeleton (M0–M2).** Types, fake model adapter that replays scripted responses, loop with budgets, `read_file`/`grep`/`bash` with truncation, event log to SQLite, structured logs. *Exit:* a scripted-model test drives a full multi-tool run, kills it mid-tool, and `resume` completes it.

**Phase 2 — Real model + edit tools (M3–M4).** One real adapter, streaming, retries, `edit_file` + `apply_patch`, read-before-edit tracking, completion gate (tests). *Exit:* agent fixes 5 seeded bugs in a toy repo; edit-tool error paths covered.

**Phase 3 — Safety (M5).** Policy engine, shell parser, path canonicalization, approval flow, OS sandbox. *Exit:* the adversarial suite (`07-evals.md` §6) passes with zero escapes.

**Phase 4 — Context (M6).** Prompt layout for caching, compaction ladder, instruction files. *Exit:* a 300-step synthetic run completes inside a 32k window with the task constraints still honored.

**Phase 5 — Scale-out (M7–M8).** Subagents, hooks, MCP, second/third provider (DeepSeek, vLLM), model routing. *Exit:* same eval suite passes on ≥2 providers with provider-specific code only under `models/`.

**Phase 6 — Evals & ops (M9).** SWE-bench-style runner, regression gate in CI, dashboards, HTTP API, Docker image, `doctor`. *Exit:* a PR that degrades resolve-rate or raises cost-per-task beyond tolerance fails CI.

Do not skip to features (subagents, MCP) before the Phase 3 exit test passes.

---

## 7. Profiles

**Coding profile (default).** Tools: `read_file`, `list_dir`, `glob`, `grep`, `edit_file`, `apply_patch`, `write_file`, `bash`, `todo_write`, `ask_user`, `spawn_subagent`. Loop adds: repo-discovery turn, plan mode, test-run discipline, diff review before finalize, completion gate. Final report must state: what changed, why, what was run, what remains.

**Reasoning/research profile (DeepSeek-style long-horizon).** Tools: `search`, `fetch`, `python`, `notes_write`, `notes_read`, `spawn_subagent`. Adds: an explicit **working-state document** (question, hypotheses, evidence-with-source-ids, open items) kept outside the transcript and re-injected after compaction; source ledger (every claim in the final answer cites a ledger entry); verification pass by a separate model call with *only* the evidence, not the reasoning; reasoning tokens budgeted separately. Reasoning text is logged for debugging, never fed to verifiers as evidence.

**Batch/eval profile.** Headless, `bypass` approval inside a disposable sandbox with network off, deterministic config (temperature 0, fixed seeds, pinned image), hard per-task budget, artifacts exported for graders.

---

## 8. Production-readiness gate (each line has a proof)

A checkbox without a command that proves it is not accepted.

| Property | Proof (automated) |
|---|---|
| Bounded loop | Test: model that always calls a tool terminates at `max_iterations` with `BudgetExceeded` and a final report |
| Bounded time | Test: tool `sleep 1e6` is killed at timeout, process group gone (`pgrep` empty), result returned |
| Cancellation | Test: cancel during model stream and during tool run; no orphans; events show `RunCancelled`; history valid for resume |
| Crash recovery | Test: `kill -9` the harness at each `ToolStarted`; `resume` yields consistent history, no duplicated mutating effects |
| Orphan-free history | Property test: for any run prefix, every `ToolUse` has exactly one `ToolResult` |
| Path safety | Table test ≥ 50 cases: `../`, absolute, symlink-out, `~`, `$HOME`, unicode normalization, case-insensitive FS, `.git/hooks`, mounts |
| Shell safety | Table test ≥ 100 commands incl. `;`, `&&`, `|`, `$()`, backticks, heredocs, `env`/`xargs`/`find -exec` wrappers, unparseable input → `ask`/`deny` |
| Network off | Test inside sandbox: `curl`, `python -c socket`, DNS lookup all fail |
| Secrets | Test: planted `AWS_SECRET_ACCESS_KEY` in host env absent in sandbox env, absent from events/logs/artifacts |
| Injection | Suite: README/web page/MCP output instructing exfiltration or privilege escalation → no `allow` change, no network call |
| Budgets | Test: token, cost, tool-call, wall-clock limits each trip independently with correct failure class |
| Context | Test: compaction preserves constraints/decisions/file-state; post-compaction run still passes the task |
| Provider isolation | CI check: no provider SDK import outside `models/<provider>.py` |
| Replay | Test: recorded-run replay with stub model reproduces identical event types and tool args |
| Regression | CI job runs smoke eval (≤ 10 min) and blocks on resolve-rate or cost regression |
| Observability | One run produces a trace with spans for model calls and tool calls, JSON logs sharing `run_id`, and cost totals that match provider usage within 1% |

---

## 9. Anti-patterns (reject on sight)

- A 2,000-line `agent.py` that mixes prompting, parsing, subprocess and retry logic.
- Letting the model choose its own timeouts, budgets or sandbox mode.
- Matching dangerous commands with a regex on the raw string (`"rm -rf" in cmd`). Parse the shell; fail closed when parsing fails.
- Allowing `bash` to be the only way to read/write files. Dedicated file tools give better errors, diffs, staleness checks, and tighter policy.
- Dumping whole `cat` / `grep` / test output into context.
- Truncating context by dropping the oldest messages. Compact with structured summaries, preserve the task.
- Retrying on every exception. Retry only classified transient errors with jittered backoff.
- Stashing run state in module globals or the sandbox filesystem only.
- Using an LLM judge as the only grader for coding tasks.
- Declaring "production-ready" because a demo task passed.
- Copying model-specific prompt hacks into the core loop. Put them in adapter/profile config.

---

## 10. Output contract

When asked to *build*, deliver in this order, with real files (not pseudocode) for everything through item 9:

1. Architecture decision record (stack, boundaries, threat model summary)
2. Repository tree
3. Core types and interfaces
4. Agent loop + budgets + completion gate
5. Model adapter(s) with streaming + error normalization
6. Tools (read, edit, patch, bash, grep, glob, todo) with tests
7. Policy engine + shell parser + path resolver + sandbox backend
8. Event store, checkpoints, resume/rewind
9. Context manager + compaction
10. Eval runner + seed tasks + adversarial suite
11. Observability (logs, metrics, traces)
12. CLI (`run`, `resume`, `inspect`, `replay`, `evaluate`, `doctor`, `export`) and API
13. Deployment (Dockerfile, config, health/readiness)
14. Test results (actual output of the commands in §8)
15. Known limitations, each with: what is missing, why it matters, safe boundary until implemented

If something cannot be done safely in the session (e.g. real microVM isolation), say so, implement the safe boundary (e.g. refuse to run untrusted code without a container), and list it under limitations. Never ship a toy in place of a production component without labelling it.

When asked to *review or upgrade* an existing harness, produce a gap analysis against §2 and §8 first, ranked by blast radius (sandbox/policy gaps → crash recovery → context → evals → UX).
