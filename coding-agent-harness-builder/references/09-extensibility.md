# 09 — Subagents, Hooks, MCP, Modes, Headless, Client Protocol

## Contents
1. Subagents
2. Hooks
3. MCP client
4. Plan mode and modes
5. Skills and slash commands
6. Background tasks and parallel agents (worktrees)
7. Core ↔ client protocol
8. Headless / CI mode
9. HTTP API

---

## 1. Subagents

Purpose: isolate context. A subagent explores or does a bounded task in its own context window and returns a short, structured result, keeping the parent's context small.

```python
@dataclass(frozen=True)
class AgentType:
    name: str                       # "explorer", "test-runner", "reviewer", "reader"
    system_prompt: str
    tools: frozenset[str]           # allowlist; explorer = {read_file, grep, glob, list_dir}
    model_role: str                 # route to a cheaper model for explorers
    budget_fraction: float          # slice of parent's remaining budget
    permission_mode: str            # usually stricter than parent (read_only)
    can_spawn: bool = False         # depth limit: default no recursion beyond depth 2
```

Rules:
- Fresh context: gets the `task` text + explicitly listed context files, **not** the parent's history.
- Returns ≤ N tokens (e.g., 1.5k) of final text (+ optional structured JSON). Parent sees only that. Full child transcript stays in the event log as `child_run_id`.
- Child permissions ⊆ parent's. A child can never exceed the parent's mode, tool set or budget.
- Parallel subagents allowed for read-only types; writing subagents must work in separate worktrees/snapshots (§6) and return a patch for the parent to apply.
- Use cases: codebase exploration, running and summarizing long test output, independent code review/verification, "reader" for untrusted web content (`08` §3.7), research fan-out.
- Evaluate usefulness: A/B on the eval suite; subagents add cost and failure modes.

## 2. Hooks

User-defined deterministic callbacks at lifecycle points. They let teams enforce standards without changing the core.

Events: `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PermissionRequest`, `PreCompact`, `Stop` (before completion), `SubagentStop`, `Notification`.

Contract (language-agnostic: command hooks over stdin/stdout JSON):

```json
// stdin
{"event":"PreToolUse","run_id":"…","tool":"bash","args":{"command":"npm test"},"effect":{...},"cwd":"…"}
// stdout (optional)
{"decision":"allow|deny|ask|modify","reason":"…","modified_args":{...},"additional_context":"…"}
// exit code: 0 ok, 2 = blocking error (stderr fed back to model), other = non-blocking error (logged)
```

Rules:
- Hooks run **inside the security envelope**: they may *tighten* decisions (deny/ask) freely; a hook returning `allow` cannot override a policy `deny`.
- Timeouts (default 10 s), output caps, run in sandbox when from untrusted (repo-level) config; repo-level hooks require explicit user trust (hash-pinned) because they are code execution.
- `PostToolUse` output (lint/format/typecheck) is appended to the tool result so the model fixes issues immediately.
- `Stop` hook can block completion with feedback (this is the user-extensible version of the completion gate).
- Record `HookRan` events with decision and duration.

## 3. MCP client

- Transports: stdio (local servers), streamable HTTP/SSE (remote). Implement capability negotiation, `tools/list`, `tools/call`, optionally resources/prompts.
- **Namespacing**: expose as `mcp__<server>__<tool>`; detect collisions.
- **Schema handling**: downgrade/validate; limit tool count per server (hundreds of tools wreck selection; support per-server allowlists and lazy "tool search" if counts are high).
- **Security**: every MCP tool defaults to `ask`; results are `untrusted`; servers run sandboxed or with minimal env; timeouts and output caps apply; log all calls; pin server versions.
- **Lifecycle**: start lazily, health-check, restart with backoff, kill on run end; surface connection errors as tool errors.
- **Auth**: OAuth flows handled by the host app/secret broker, not the model; never place tokens in model context.

## 4. Plan mode and modes

- **Plan mode**: tools restricted to read-only (+ `ask_user`, `todo_write`); system reminder each turn forbidding modification; the agent ends with a plan the user approves via `exit_plan_mode(plan)` (an approval gate). On approval the mode switches (e.g. `auto_edit`) and the plan is stored in state and re-injected after compaction.
- **Mode switching** is a user/policy action, never a model action (the model may *request* via a tool that triggers approval).
- Expose current mode in every event and in the UI.

## 5. Skills and slash commands

- **Skills** = folders with `SKILL.md` (frontmatter `name`, `description` + body + resources). Load only name/description into the prompt; the model reads the body via `read_file` when relevant (progressive disclosure). Keep skill bodies <500 lines; push depth to referenced files.
- **Slash commands**: markdown prompt templates (`.harness/commands/review.md`) with arguments (`$ARGUMENTS`), optional allowed tools and model override. Expand client-side into a user message.
- **Custom subagents** defined the same way (markdown + frontmatter: tools, model, prompt).

## 6. Background tasks and parallel agents

- Long commands (dev servers, watchers, long tests) run as background tasks with handles; `bash_output`/`bash_kill`; the loop can continue while they run; notifications are injected as reminders. All killed at run end.
- **Parallel agents on one repo**: one **git worktree** (or copy-on-write snapshot) per agent, each with its own sandbox and branch; merge via patches/PRs; never share a working directory between writers.
- Concurrency control: global semaphore on sandboxes, per-repo lock for operations that touch shared git state (`git gc`, packing).

## 7. Core ↔ client protocol

Run the core as a library/daemon that speaks a small, versioned protocol; every client (TUI, IDE, web, CI) is a thin adapter. This is the "submission queue / event queue" pattern.

Submissions (client → core): `StartRun`, `UserMessage`, `Interrupt`, `ApproveTool{id, scope}`, `DenyTool{id, feedback}`, `AnswerQuestion`, `SetMode`, `Rewind`, `Resume`, `Shutdown`.
Events (core → client): the durable events (`06` §2) plus ephemeral stream deltas (`TextDelta`, `ThinkingDelta`, `ToolOutputDelta`).

Transport options: in-process channels, stdio JSON-RPC (IDE extensions spawn the core), WebSocket/SSE (web). Version the protocol (`protocol_version` handshake) and keep unknown-event tolerance on clients.

## 8. Headless / CI mode

```bash
harness run task.yaml --headless --mode bypass --sandbox docker --net none \
  --budget-usd 3 --output-format jsonl --exit-code-by-gate
```

- No interactive prompts: `ask` decisions resolve per config (`on_ask: deny|allow_rule_only|fail_run`).
- Deterministic config; machine-readable output; exit codes: `0` completed+gate pass, `1` failed gate/limit, `2` harness/infra error, `3` policy violation, `130` cancelled.
- Emits artifacts (`final.diff`, `report.json`, `events.jsonl`) to a configured directory; can open a PR via the secret broker if explicitly enabled.

## 9. HTTP API

```http
POST   /v1/runs                       Idempotency-Key: <uuid>      → 202 {run_id}
GET    /v1/runs/{id}                                               → status, outcome, cost, budgets
POST   /v1/runs/{id}/messages         {text}                       → append user message
POST   /v1/runs/{id}/cancel                                        → 202
POST   /v1/runs/{id}/resume                                        → 202
POST   /v1/runs/{id}/approvals/{aid}  {decision, scope, feedback?} → 200
GET    /v1/runs/{id}/events?after_seq=N   (SSE stream, resumable by seq / Last-Event-ID)
GET    /v1/runs/{id}/artifacts                                     → list + signed URLs
POST   /v1/evaluations                {suite, k, model, harness_ref}
GET    /healthz   (liveness)    GET /readyz (db, queue, sandbox backend, model creds)
```

Workers pull runs from a queue (Postgres `SKIP LOCKED`, Redis streams, or SQS), hold a **lease with heartbeat**, and are safe to kill at any time (recovery per `06` §5). Autoscale on queue depth; cap sandbox concurrency per node.
