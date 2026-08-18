# The Capability Model

> **Status:** normative.
>
> One vocabulary for authority across the entire system. Permissions, sandboxing, plugins, subagents,
> MCP servers, scheduled runs, and hooks all express what they may do in these same terms.

---

## 1. Why one vocabulary

Without this, every subsystem invents its own authority language and they cannot be reasoned about
together. You end up with a plugin permission list, a separate sandbox mode enum, a separate subagent
`tools: [...]` allowlist, a separate MCP per-server config, and a separate schedule flag — five
systems that overlap without composing. The predictable result is a gap: a subagent that inherits a
tool it should not have, or a plugin whose declared permissions don't actually constrain the MCP
server it ships.

With one vocabulary, authority becomes a **set**, and the rules become set operations:

- Granting is set membership
- Delegating is **attenuation** — a subset, never a superset
- Auditing is recording which capability authorized an action
- Sandboxing is enforcing the set at the OS level
- A permission prompt is a request to add to the set

`kalash status` can then print one answer to "what can this session do right now?", which is a
question the current design cannot answer without inspecting five places.

---

## 2. The vocabulary

Capabilities are dotted, hierarchical strings. Granting a parent grants its descendants:
`filesystem` implies `filesystem.read` and `filesystem.write`.

### Filesystem

| Capability | Authorizes |
|---|---|
| `filesystem.read` | Reading files and directory listings within readable roots |
| `filesystem.write` | Creating, modifying, deleting files within writable roots |
| `filesystem.read.outside` | Reading outside the workspace (home dotfiles, system paths) |
| `filesystem.write.outside` | Writing outside the workspace — **never granted by default in any mode** |
| `filesystem.protected` | Writing to a protected path. Requires per-path config; see I-010 |

Note that `filesystem.write` does not imply `filesystem.write.outside`. Roots are a separate
dimension from the verb, resolved in `sandbox/policy.py`. A capability grant says *what kind of
action*; the root set says *where*. Both must pass.

### Execution

| Capability | Authorizes |
|---|---|
| `shell.execute` | Running commands via the `shell` tool |
| `shell.execute.elevated` | `sudo`, `doas`, `runas` — separate because privilege escalation escapes the sandbox entirely |
| `process.spawn` | Spawning subprocesses outside the `shell` tool (hooks, plugins, MCP stdio servers) |
| `process.background` | Long-running processes that outlive the tool call |

### Network

| Capability | Authorizes |
|---|---|
| `network.connect` | Any outbound connection. Gate for I-014 |
| `network.connect.private` | Connections to RFC1918, loopback, link-local. Gate for I-015 |
| `network.listen` | Binding a listening socket (`kalash serve`, `kalash mcp serve`) |
| `network.listen.public` | Binding to a non-loopback interface — separate, because this exposes the machine |

### Memory

| Capability | Authorizes |
|---|---|
| `memory.read` | Recall from the memory layer |
| `memory.write` | Creating and updating records |
| `memory.forget` | Deleting records and writing tombstones |
| `memory.export` | Bulk extraction of all records |
| `memory.egress` | Transmitting records to a remote provider. Gate for I-002/I-003 |
| `memory.egress.artifacts` | Including source-code chunks in that transmission |
| `memory.scope.global` | Writing user-global records that affect every project |

`memory.export` is separate from `memory.read` because bulk extraction is a different risk than
query-scoped recall — it is the shape of a data exfiltration, and a plugin that needs recall almost
never needs export.

### Git

| Capability | Authorizes |
|---|---|
| `git.read` | Status, log, diff, show — read-only inspection |
| `git.write` | Add, commit, branch creation, tag, stash |
| `git.write.history` | Rebase, amend, filter-branch, reset --hard — anything rewriting existing history |
| `git.remote.fetch` | Fetch, pull |
| `git.remote.push` | Push to a remote |
| `git.remote.push.force` | Force push. Always requires confirmation regardless of grant |
| `git.remote.push.protected` | Push to `main`, `master`, or a configured protected branch |

Git is deliberately granular. "The agent can use git" is far too coarse: reading history and
force-pushing to `main` differ by orders of magnitude in consequence, and lumping them together means
users either block useful work or accept unacceptable risk.

### Extensions

| Capability | Authorizes |
|---|---|
| `mcp.connect` | Connecting to a configured MCP server |
| `mcp.connect.remote` | Connecting to a non-stdio (network) MCP server |
| `mcp.serve` | Exposing Kalash tools over MCP |
| `plugin.execute` | Running plugin-provided code |
| `plugin.install` | Installing or updating a plugin |
| `hook.execute` | Running project-defined hooks |
| `skill.script` | Executing a skill's bundled scripts |

### Orchestration

| Capability | Authorizes |
|---|---|
| `agent.spawn` | Creating subagents |
| `agent.spawn.depth` | Parameterized: maximum additional depth this principal may add |
| `scheduler.create` | Creating a schedule |
| `scheduler.create.write` | Creating a schedule that holds write capabilities — the dangerous one |
| `scheduler.trigger` | Manually firing a schedule |

`scheduler.create.write` exists because a schedule is *deferred, unattended authority*. An agent that
can create a write-capable schedule can grant its future self write access with nobody watching,
which routes around the interactive approval it would otherwise face.

### Model and data

| Capability | Authorizes |
|---|---|
| `model.invoke` | Calling a configured model |
| `model.invoke.expensive` | Calling a model above a configured cost-per-token threshold |
| `data.export` | Exporting sessions and audit logs |
| `data.delete` | Deleting sessions, running destructive GC |
| `config.write` | Modifying Kalash's own configuration |
| `telemetry.send` | Sending anonymized usage data. Off unless explicitly enabled |

---

## 3. Principals

A **principal** is anything that holds a capability set.

| Principal | Set origin |
|---|---|
| **Interactive session** | Sandbox mode baseline ∩ config policy ∪ user grants earned during the session |
| **Subagent** | Parent's set ∩ the agent definition's declared set. Strict attenuation |
| **Scheduled run** | The schedule's declared set, itself capped by what the creating principal held |
| **Hook** | The triggering principal's set ∩ the hook's declared set |
| **Plugin** | The plugin manifest's declared set, approved by the user at install |
| **MCP server** | Per-server config set. A server's tools cannot exceed it |
| **Skill script** | The invoking session's set ∩ the skill's `allowed-tools` mapping |

### Attenuation is absolute

```
child_capabilities ⊆ parent_capabilities
```

No principal may create another with authority it does not itself hold. There is no escalation path,
no "system" principal that bypasses this, and no configuration that enables it.

Consequences worth stating explicitly, because they are the cases people try to route around:

- A read-only session cannot spawn a writing subagent.
- A subagent cannot create a schedule with capabilities beyond its own.
- A plugin cannot register an MCP server with capabilities beyond the plugin's own grant.
- A hook cannot grant the tool call it is gating more authority than the hook itself has.

**Hooks are the sole exception, in one direction only:** a hook may *deny*. Denial is not authority, so
it needs no grant, and per I-034 it is final at any depth.

### Attenuation is not the same as tool allowlists

An agent definition's `tools: [read, write, shell]` is a *convenience projection* over capabilities,
not the authority itself. `tools` is expanded to a capability set at load time and then intersected
with the parent's. This matters because a tool can require multiple capabilities — `shell` requires
`shell.execute` and, depending on the command, `network.connect` or `git.write` — and because MCP tools
have no fixed built-in identity to allowlist against.

---

## 4. Resolution

Every tool call resolves to a required capability set before permission evaluation (I-004).

```
tool call
  → normalize arguments (canonical paths, resolved hosts)
  → required: set[Capability]          # from the tool's declaration + argument inspection
  → held: set[Capability]              # the principal's current set
  → missing = required - held
      empty          → admitted, proceed to sandbox admission
      non-empty      → consult policy for each missing capability:
                          deny     → refuse, return tool error
                          allow    → add for this call, proceed
                          ask      → prompt user (interactive) / stop and report (unattended, I-032)
```

**Argument inspection matters.** A tool's static declaration is a floor, not the answer. `shell` with
`git push --force origin main` requires `shell.execute` + `git.remote.push.force` +
`git.remote.push.protected`. Deriving that requires parsing the command, which is why
`tools/shell.py` maintains a command classifier (see [`tools.md`](./tools.md) §Shell).

Classification is **conservative**: an unrecognized or unparseable command is assigned the broadest
plausible capability set, not the narrowest. Failing toward "ask" is correct; failing toward "allow"
is a vulnerability. A command using shell metacharacters that defeat parsing (`$(...)`, backticks,
`eval`, pipes into `sh`) is classified as requiring the full set.

### Policy evaluation order

```
1. explicit deny rules            (config, plugin manifest, hook) — always win, no override
2. protected-path / protected-branch rules                        — I-010
3. existing grants                (once | session | always)
4. capability defaults for the sandbox mode
5. approval policy                (untrusted | on-request | on-failure | never)
6. confirmation-class check       — see permissions.md §Always-confirm
```

A deny at any stage terminates evaluation. Later stages cannot overturn an earlier deny. This ordering
is what makes "deny always beats allow" (I-030) structurally true rather than a rule to remember.

---

## 5. Sandbox modes as capability sets

Sandbox modes are presets, not a separate mechanism. This is what lets one enforcement path cover
both.

| Capability | `read-only` | `workspace-write` | `danger-full-access` |
|---|---|---|---|
| `filesystem.read` | ✓ | ✓ | ✓ |
| `filesystem.write` | — | ✓ (workspace roots) | ✓ (all roots) |
| `filesystem.read.outside` | ask | ask | ✓ |
| `filesystem.write.outside` | — | — | ask |
| `filesystem.protected` | — | — | — (I-010) |
| `shell.execute` | ✓ (classified read-only) | ✓ | ✓ |
| `shell.execute.elevated` | — | — | ask |
| `network.connect` | — | — | ask |
| `network.connect.private` | — | — | ask |
| `git.read` | ✓ | ✓ | ✓ |
| `git.write` | — | ✓ | ✓ |
| `git.write.history` | — | ask | ask |
| `git.remote.push` | — | ask | ask |
| `git.remote.push.force` | — | ask | ask |
| `memory.read` | ✓ | ✓ | ✓ |
| `memory.write` | ✓ | ✓ | ✓ |
| `memory.egress` | per config | per config | per config |
| `agent.spawn` | ✓ | ✓ | ✓ |
| `scheduler.create.write` | — | ask | ask |

Reading the table: ✓ granted, — denied, `ask` requires approval regardless of policy.

Two things worth noting. First, `network.connect` is denied even in `workspace-write` — the common
default. Network access is opt-in per project, because a coding agent that can both read your source
and reach the internet is the exfiltration primitive. Second, `memory.egress` is orthogonal to
sandbox mode entirely: it is governed by `memory.egress.mode` config, because it is a data-privacy
decision rather than a code-safety one, and users reason about it separately.

`read-only` grants `shell.execute` only for commands the classifier can prove are read-only
(`git status`, `ls`, `pytest --collect-only`). Anything unclassifiable is denied in this mode, which
is the conservative direction.

---

## 6. Declaring capabilities

### Agent definition

```yaml
---
name: reviewer
description: Reviews diffs for correctness and security. Use before commit.
capabilities: [filesystem.read, git.read, memory.read]
max_turns: 20
---
```

A reviewer with no write capability cannot "helpfully fix" what it was asked to review. That is a
feature: the isolation is what makes the review trustworthy.

### Plugin manifest

```json
{
  "name": "deploy-helper",
  "version": "1.4.0",
  "capabilities": {
    "required": ["filesystem.read", "shell.execute"],
    "optional": ["network.connect"],
    "reason": {
      "shell.execute":   "runs terraform plan",
      "network.connect": "fetches provider schemas; plan works offline without it"
    }
  }
}
```

`reason` is mandatory for anything in `network.*`, `memory.egress*`, `shell.*`, `git.remote.*`, or
`filesystem.write.outside`, and is shown verbatim in the install prompt. A plugin that cannot explain
why it needs network access is a plugin the user should decline. `optional` capabilities may be
refused at install without breaking the plugin — it must degrade.

### Schedule

```
kalash cron add nightly-audit \
  --spec "0 3 * * *" \
  --agent auditor \
  --capabilities filesystem.read,git.read,memory.write \
  --prompt "Audit dependency advisories and summarize"
```

Omitting `--capabilities` yields `read-only`. Including anything from `filesystem.write`, `git.*write`,
`network.*`, or `shell.execute` requires `--unattended-write` as a separate acknowledgement, and the
CLI prints what that means before creating the schedule.

### MCP server

```json
{
  "mcpServers": {
    "postgres": {
      "command": "uvx",
      "args": ["mcp-server-postgres"],
      "capabilities": ["process.spawn"],
      "tools": { "deny": ["execute_sql"] }
    }
  }
}
```

An MCP server's tools execute with the server's capability set intersected with the session's. A
server declaring `network.connect` in a session without it gets connection failures, not silent
success — surfaced as a startup warning so the mismatch is visible rather than mysterious.

---

## 7. Auditing

Every audit row records the capability that authorized the action and how it was obtained:

```json
{
  "event_type": "tool.execute",
  "tool": "shell",
  "capabilities_required": ["shell.execute", "git.remote.push"],
  "capabilities_source": {
    "shell.execute":     "sandbox_default:workspace-write",
    "git.remote.push":   "grant:session:a1b2c3"
  },
  "sandbox_mode": "workspace-write",
  "decision": "allowed"
}
```

This makes the audit log answer the question that actually matters after an incident: not just "what
happened" but "what authorized it, and when did the user agree to that." A grant ID resolves to the
exact prompt the user saw and when they answered it.

---

## 8. Introspection

```
kalash status --capabilities
```

```
Session  ses_01JQ...              Sandbox  workspace-write     Approval  on-request

GRANTED
  filesystem.read                 sandbox default
  filesystem.write                sandbox default   → roots: /home/shivam/proj, $TMPDIR
  shell.execute                   sandbox default
  git.read, git.write             sandbox default
  memory.read, memory.write       sandbox default
  agent.spawn                     sandbox default   → depth remaining: 2
  network.connect                 session grant     → granted 14:22, hosts: api.github.com

DENIED
  filesystem.write.outside        sandbox mode
  filesystem.protected            invariant I-010
  network.connect.private         invariant I-015
  shell.execute.elevated          sandbox mode

WILL ASK
  git.remote.push, git.write.history, scheduler.create.write

MEMORY EGRESS  redacted · artifacts off · providers: local, mem0
```

One command, one complete answer. Users cannot reason about a security model they cannot inspect, and
support cannot debug one either.

---

## 9. Adding a capability

New capabilities are a protocol change, versioned per [`operations.md`](./operations.md).

1. Add to the vocabulary here with a precise authorization statement
2. Add a row to the sandbox-mode table in §5 — every mode must have a defined value, no implicit
   defaults
3. Decide: does it need `reason` in plugin manifests?
4. Map affected tools' required sets
5. Add the resolution test
6. Note it in the capability compatibility matrix in [`operations.md`](./operations.md)

**A new capability defaults to denied in every mode.** An unrecognized capability in an old config,
or one from a plugin built against a newer protocol version, is treated as denied — never as
unconstrained. Fail closed on the unknown.

---

*See also: [`invariants.md`](./invariants.md) for the constraints capabilities enforce,
[`permissions.md`](./permissions.md) for the decision algorithm and approval UX,
[`threat-model.md`](./threat-model.md) for what attenuation defends against.*
