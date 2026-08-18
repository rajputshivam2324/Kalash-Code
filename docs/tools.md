# Tools

> **Status:** normative.
>
> Tools are the agent's only means of affecting anything. This document specifies the execution
> contract, then the semantics of the four subsystems where the details actually bite: filesystem,
> shell, git, and web.

---

## 1. The contract

### 1.1 Declaration

```python
class Tool(Protocol):
    name: str
    version: int                       # bumped on any schema or semantic change
    description: str                   # the model's only guide to when to use it
    params: type[BaseModel]            # JSON Schema is GENERATED, never hand-written
    side_effect: SideEffect            # NONE | READ | WRITE | EXEC
    capabilities: frozenset[Capability]        # static floor
    dynamic_capabilities: Callable | None      # argument-derived additions
    timeout_s: float
    max_output_bytes: int
    idempotent: bool
    cancellable: bool

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope: ...
```

Schemas are generated from Pydantic models. A hand-written JSON Schema drifts from the validation code
it is supposed to describe, and the drift shows up as the model passing arguments that validate against
the advertised schema and then fail at runtime.

`capabilities` is a floor, not the answer. `dynamic_capabilities` inspects normalized arguments and adds
what they imply — this is what makes `shell` with `git push --force` resolve to the right authority set
(see [`capabilities.md`](./capabilities.md) §4).

### 1.2 The envelope

Every execution returns this. There is no other return shape, and exceptions escaping a tool are a bug
in that tool.

```python
@dataclass(frozen=True, slots=True)
class ToolEnvelope:
    ok: bool
    content: str | tuple[ContentBlock, ...]
    metadata: Mapping[str, Any]        # bytes_read, lines, exit_code, duration_ms, digest...
    truncated: bool
    truncation: TruncationInfo | None  # what was cut, how much, how to get the rest
    error: ToolError | None            # code, message, recoverable, remediation
    side_effects: tuple[SideEffectRecord, ...]   # paths written, commands run — for audit and rewind
```

Four rules:

**Errors are results.** A missing file, a failed command, a denied permission — all return `ok=false`
with an actionable message. The agent gets a chance to adapt. Raising would end the turn and lose the
opportunity to correct a typo'd path.

**Truncation is always declared.** `truncated=true` plus `TruncationInfo` saying what was cut and how to
retrieve it. Silently returning half a file and letting the model reason about it produces confidently
wrong code, and the failure is invisible.

**`side_effects` is populated for anything that changed state.** This feeds the audit log and the
checkpoint manifest. A write that does not record its path cannot be rewound.

**`remediation` is filled where a fix is knowable.** `KALASH_TOOL_STALE_READ` says "re-read the file";
a denied capability says which one and how to grant it. The difference between an error the agent
recovers from and one it flails against is usually whether the message said what to do.

---

## 2. Lifecycle

Fixed, total order. No stage is skippable, and there is no path to `execute()` except through this
pipeline (I-004).

```
 1  receive call            from the model stream, after MessageStop      model-gateway.md §5.3
 2  resolve tool            unknown → error result, not a crash
 3  validate schema         Pydantic; failure → error result with the validation detail
 4  normalize arguments     canonical paths, resolved hosts, expanded globs   ← BEFORE policy
 5  resolve capabilities    static ∪ dynamic(args)
 6  evaluate permission     capabilities.md §4; deny → error, ask → prompt/stop
 7  admit to sandbox        writable roots, protected paths, network         I-009, I-010
 8  PreToolUse hooks        deny → error result with hook stderr as feedback  I-034
 9  acquire scheduling slot §3
10  execute                 with timeout, resource limits, cancellation
11  cap output              §4
12  PostToolUse hooks       may annotate; cannot un-execute
13  record                  audit row, side effects, usage
14  return envelope
```

**Stage 4 precedes stage 6, and that ordering is load-bearing.** Evaluating a policy against
`../../../etc/passwd` and then executing the resolved path is the canonical sandbox escape. Policy sees
only canonical, resolved values.

**Stage 12 cannot undo stage 10.** `PostToolUse` observes and annotates. A hook that wants to prevent
something must be a `PreToolUse` hook. This is stated because the alternative — post-hoc veto — is
impossible to implement honestly for a command that already ran.

### 2.1 Failure at each stage

| Stage | Failure | Result |
|---|---|---|
| 2 | Unknown tool name | `KALASH_TOOL_UNKNOWN`, with the closest available names. Common after an MCP server disconnects mid-session |
| 3 | Schema violation | `KALASH_TOOL_INVALID_ARGS` with the field-level detail, so the model can correct rather than guess |
| 3 | Unparseable JSON args | `KALASH_TOOL_MALFORMED_ARGS`. Never partially applied |
| 4 | Path escapes roots | `KALASH_SANDBOX_PATH_DENIED` naming the resolved path |
| 6 | Capability denied | `KALASH_PERMISSION_DENIED` naming the capability and how to grant it |
| 6 | Approval needed, unattended | `KALASH_APPROVAL_REQUIRED` — run stops and reports (I-032) |
| 8 | Hook denied | `KALASH_HOOK_DENIED` with the hook's stderr verbatim |
| 10 | Timeout | `KALASH_TOOL_TIMEOUT` with partial output where meaningful |
| 10 | Cancelled | `KALASH_CANCELLED`; no partial state (I-008) |
| 10 | Unexpected exception | `KALASH_TOOL_INTERNAL`, logged with traceback, sanitized message to the model |

### 2.2 Retries and idempotency

**The runtime never silently retries a tool.** A failure returns to the model, which decides. Automatic
retry of a tool with side effects risks double-application; automatic retry of a read hides a real
problem behind latency.

`idempotent` is metadata for two consumers: the speculative-execution path in
[`model-gateway.md`](./model-gateway.md) §5.3, and crash recovery, which may safely re-run an idempotent
tool whose completion was not recorded.

Loop guard: the same tool with byte-identical normalized arguments failing identically three times in a
row is reported to the model as a repeated failure with an instruction to change approach. Prevents the
model burning a turn budget retyping the same wrong path.

### 2.3 Cancellation

`cancellable: true` tools must respond to `asyncio` cancellation promptly and leave no partial state.
For `shell`, cancellation kills the process group (I-013). For a write, cancellation before
`os.replace` leaves the original untouched (I-008).

`cancellable: false` is permitted only where interruption would corrupt state and the operation is
bounded — a single atomic database write, for instance. It requires a comment justifying it.

### 2.4 Versioning and collisions

`version` bumps on any parameter or semantic change. The active version is recorded per call in the
turn record (I-037), so a session replayed after an upgrade is interpretable rather than mysterious.

Removal is a two-release deprecation: the tool remains, returns a deprecation notice in `metadata`, and
its description tells the model what to use instead.

Name resolution order, first match wins:

```
1  built-in tools               reserved; cannot be shadowed
2  plugin-provided tools        namespaced  plugin__<plugin>__<tool>
3  MCP tools                    namespaced  mcp__<server>__<tool>
```

Built-ins are unshadowable by design (T-B): a malicious MCP server registering a tool named `read` that
exfiltrates is otherwise a trivial attack. Two plugins claiming the same name is a load-time error
naming both, not a silent last-wins.

---

## 3. Scheduling and concurrency

| `side_effect` | Scheduling |
|---|---|
| `NONE` | Unlimited concurrency |
| `READ` | Concurrent, capped by a read semaphore |
| `WRITE` | Serialized against all `WRITE` and `EXEC` |
| `EXEC` | Serialized against all `WRITE` and `EXEC` |

Reads and writes to overlapping paths order deterministically: a `WRITE` to a path with a pending
`READ` waits for the read. Otherwise the model sees a file it just wrote as its pre-write content, or
worse, sees it inconsistently between runs.

Batching independent calls into one assistant turn is expected and encouraged — sequential round-trips
for independent reads waste both latency and tokens.

Global caps: concurrent reads, concurrent subprocesses, and total open file descriptors, all from
[`operations.md`](./operations.md) §Resource limits.

---

## 4. Output caps

Unbounded tool output is a context-exhaustion vector and a cost problem — a `find /` or a verbose test
suite can produce megabytes.

| Cap | Default |
|---|---|
| Per-call output | 256 KiB |
| Per-call lines | 2000 |
| Single line length | 4000 chars |
| `read` file size | 250 KiB or 2000 lines, whichever first |
| Shell stdout/stderr each | 128 KiB |

**Truncation retains head and tail**, not just the head. A failing test suite puts the summary at the
end and the first failure at the start; keeping only the beginning discards the most useful part. The
middle is elided with an explicit marker stating how much was dropped and how to get it:

```
[... 1,847 lines elided (312 KiB). Retrieve with: read(path, offset=520, limit=200) ...]
```

Long single lines are truncated with a marker rather than wrapped — minified JS or a base64 blob should
not consume the whole budget.

Binary output is never inlined. It is written to a blob and referenced by digest, size, and detected
type.

---

## 5. Filesystem

Every item below is a real failure mode, not a hypothetical.

### 5.1 Path resolution

Applied before any policy evaluation (§2, stage 4):

1. Expand `~` and environment references
2. Make absolute against the tool's `cwd` (never the process cwd, which changes)
3. **Unicode NFC normalization** — `.env` composed differently must not evade a protected-path check
4. Resolve symlinks fully (`Path.resolve(strict=False)`)
5. Collapse `..` after resolution, never before
6. On case-insensitive filesystems, fold case **for comparison only**, preserving the original for I/O
7. On Windows, resolve junctions, reparse points, and 8.3 short names

Steps 3, 6, and 7 exist because a protected-path check that compares raw strings is bypassed by
`.ENV` on macOS, by an NFD-composed filename, and by `PROGRA~1` on Windows.

### 5.2 Symlinks and hard links

| Case | Behaviour |
|---|---|
| Read through a symlink | Allowed if the **resolved target** is within readable roots |
| Write through a symlink | Allowed only if the resolved target is within writable roots |
| Symlink pointing outside the workspace | Denied for writes; read requires `filesystem.read.outside` |
| Creating a symlink | Requires `filesystem.write`; target validated against roots |
| Dangling symlink | Read fails clearly; write creates the target if the location is permitted |
| TOCTOU: path becomes a symlink between check and open | `O_NOFOLLOW` on the final component; directory components validated on the resolved path |
| **Hard link (`st_nlink > 1`)** | See below |

**Hard links interact badly with atomic writes, and this needs stating.** `os.replace` creates a new
inode, which breaks the link — the other names keep the old content. There is no way to be both atomic
and hard-link-preserving.

Decision: detect `st_nlink > 1`, proceed with the atomic write, and report
`metadata.hard_link_broken: true` with the link count. Atomicity (I-008) wins because a torn file is
worse than a broken link, and the agent is told so it can mention it. Silently doing either would be
worse than saying so.

### 5.3 Special files

| Type | Behaviour |
|---|---|
| Regular file | Normal |
| Directory | Read lists; write refuses with a clear message |
| FIFO / named pipe | **Refused.** Opening blocks forever |
| Character/block device | **Refused.** `/dev/random` hangs; `/dev/sda` is catastrophic |
| Socket | Refused |
| `/proc`, `/sys`, `/dev` | Denied entirely — dynamic content, escape vectors, hangs |
| Network mount | Allowed, with a warning that `rename` atomicity is not guaranteed on all network filesystems |
| Sparse file | Read normally; apparent size used for caps |

Type is determined by `stat` before opening. Opening first and detecting after is how a FIFO hangs the
agent indefinitely.

### 5.4 Content handling

**Binary detection:** null byte in the first 8 KiB, or a failed UTF-8 decode with a high proportion of
control bytes. Binaries are refused for text reads with their detected type and size, so the agent
knows the file exists and what it is.

**Encoding:** BOM if present → UTF-8 → declared encoding from a `# -*- coding:` or `<meta charset>`
line → latin-1 as a lossless byte-preserving fallback. The detected encoding is recorded and reused on
write. A UTF-16 file must not be rewritten as UTF-8; that is a corrupting change invisible in a diff
viewer that normalizes.

**Newlines:** detected per file (LF, CRLF, CR, or mixed) and preserved on write. Rewriting a CRLF file
with LF endings produces a diff touching every line, which is the fastest way to make a Windows
contributor distrust the agent. Mixed files preserve the dominant style and report the mix.

**Trailing newline:** presence or absence preserved. Adding one to a file that lacked it is a spurious
diff line.

**Indentation:** detected (tabs versus width) and matched when inserting new lines.

### 5.5 Metadata preservation

Atomic replace loses the target's metadata, so it is explicitly restored: mode bits including the
executable bit, ownership where the process has permission, and extended attributes and ACLs on a
best-effort basis with a note when they cannot be preserved.

Missing the executable bit is the classic symptom — the agent edits a shell script and it stops being
runnable, with no visible cause in the diff.

Timestamps are **not** preserved. mtime must advance so build systems and watchers notice the change.

### 5.6 Concurrent modification (I-012)

`read` returns a `content_digest` in metadata. `edit`, `multi_edit`, and overwriting `write` require it.

```
edit(path, digest="sha256:9f2c...", old="...", new="...")
  → on-disk digest matches   → proceed
  → mismatch                 → KALASH_TOOL_STALE_READ
                               metadata: {expected, actual, modified_at}
                               remediation: "re-read the file; it changed since your read"
```

This is not defensive over-engineering. The user is editing the same files in their editor while the
agent works, and a silent overwrite of their save is data loss they discover much later. The digest
check costs one `stat` plus a hash of a file already being opened.

`write` creating a **new** file passes `digest=None` and fails if the file exists — so a create cannot
silently become an overwrite.

### 5.7 Ignore files

`.kalashignore` (same syntax as `.gitignore`) plus `.gitignore` plus `.git/info/exclude`.

| Operation | Ignored-path behaviour |
|---|---|
| `glob`, `search` | Excluded by default; `--no-ignore` to include |
| `read` | Allowed — explicit intent — but flagged in metadata |
| Memory write | **Never** (I-002) |
| Repository indexing | Excluded |

`.kalashignore` additionally supports `deny:` entries, which block reads entirely rather than just
excluding from discovery — for a directory containing production credentials that should not be
readable even on request.

### 5.8 Size limits

| Limit | Default |
|---|---|
| Single read | 250 KiB / 2000 lines |
| Single write | 10 MiB |
| Glob results | 1000 paths |
| Search results | 100 matches |
| Directory listing | 1000 entries |
| Total session write volume | 100 MiB, warns at 50 |

Files over the read cap are readable by explicit line range, so a large file is workable rather than
opaque.

---

## 6. Shell

The highest-risk tool. Every default here is chosen to fail safe.

### 6.1 Invocation

```
shell(command, cwd=None, timeout_s=120, background=False, stdin=None)
```

`cwd` is a parameter and defaults to the session workspace root. **`cd` is never used** — a process-wide
chdir races with concurrent tools and leaks between calls.

Commands run through the platform shell (`bash -c`, or `pwsh -Command` / `cmd /c` on Windows) because
models and users write shell syntax — pipes, redirects, `&&`. The security position is therefore *not*
"avoid the shell" but: never interpolate untrusted values into the command string, and classify the
command conservatively (§6.5).

`shell` is not a template. Callers pass a complete command; there is no parameter substitution that
could become an injection point.

### 6.2 Environment

**Allowlist inheritance, not blocklist.** A blocklist is always incomplete — the next runtime invents a
new `*_OPTIONS` variable and the hole appears.

Inherited: `HOME`, `USER`, `LOGNAME`, `SHELL`, `TERM`, `LANG`, `LC_*`, `TZ`, `PATH` (sanitized), `TMPDIR`,
plus a configurable project allowlist for things like `DOCKER_HOST` or `CARGO_HOME`.

Stripped unconditionally: `LD_PRELOAD`, `LD_LIBRARY_PATH`, `LD_AUDIT`, `DYLD_*`, `PYTHONPATH`,
`PYTHONSTARTUP`, `NODE_OPTIONS`, `RUBYOPT`, `PERL5OPT`, `GIT_SSH_COMMAND`, `GIT_EXTERNAL_DIFF`,
`BASH_ENV`, `ENV`, `IFS`. Each is a code-injection vector: `LD_PRELOAD` loads an arbitrary library into
every child, `NODE_OPTIONS` can `--require` a module, `GIT_SSH_COMMAND` replaces ssh.

Injected: `KALASH=1`, `KALASH_SESSION_ID`, `KALASH_SANDBOX_MODE`, and `KALASH_NETWORK_DISABLED=1` when
network is denied — so a test suite can detect the constraint and skip network tests rather than fail
confusingly.

Secrets are **not** injected. A command needing a credential gets it via the project allowlist, which
makes the grant explicit and auditable.

`PATH` is rebuilt from a configured base plus project-local tool directories (`node_modules/.bin`,
`.venv/bin`), with world-writable directories dropped.

### 6.3 Process management

**stdin is `/dev/null` by default.** The single most common hang: `apt install` waiting on a
confirmation, `git rebase` opening an editor, a test prompting for a password. With closed stdin they
fail fast with a readable error instead of blocking until timeout. `stdin` may be supplied explicitly
when a command genuinely needs input.

`GIT_TERMINAL_PROMPT=0`, `DEBIAN_FRONTEND=noninteractive`, `PAGER=cat`, and `GIT_PAGER=cat` are set for
the same reason — a pager waiting for a keypress on a pipe is indistinguishable from a hang.

New process group (POSIX `setsid`) or Job object (Windows). Termination signals the whole group:
`SIGTERM` → 5s grace → `SIGKILL` (I-013). A `npm test` that spawns a dev server must not leave it
holding a port.

Resource limits applied to the child:

| Limit | Default |
|---|---|
| CPU time | timeout + 30s |
| Address space | 4 GiB |
| Open files | 4096 |
| Processes | 512 |
| File size | 1 GiB |
| Core dumps | 0 — a core file can contain secrets |

Output is streamed and capped incrementally (§4), so a runaway producer is cut off rather than buffered
to exhaustion.

Background mode returns a handle immediately; output is readable incrementally; the process is killed at
session end unless explicitly detached.

### 6.4 Exit codes

Normalized: `0` success, `1-125` command failure, `126` not executable, `127` not found, `128+n` signal
`n`, `-1` timeout, `-2` cancelled.

A non-zero exit is `ok=false` with the code and captured output. Not an exception — a failing test is
information the agent needs, not an error in the tool.

### 6.5 Command classification

The classifier maps a command to a capability set (`dynamic_capabilities`) and a risk class. It parses
with `shlex`, walks pipelines and `&&`/`||`/`;` chains, and classifies each element.

**Failing conservative is mandatory.** Anything unparseable or opaque gets the broadest plausible set,
so an unrecognized command asks rather than proceeds:

- `eval`, `source`, `.`, backticks, `$(...)`, `<(...)`
- A pipe into `sh`, `bash`, `python`, `node`
- `base64 -d | ...`
- Unbalanced quotes or a parse failure

| Command | Additional capabilities | Class |
|---|---|---|
| `ls`, `cat`, `head`, `pwd`, `which` | — | read |
| `git status`, `log`, `diff`, `show` | `git.read` | read |
| `git add`, `commit`, `stash`, `tag` | `git.write` | write |
| `git rebase`, `reset --hard`, `commit --amend`, `filter-branch` | `git.write.history` | **destructive** |
| `git push` | `git.remote.push` | write-remote |
| `git push --force`, `-f`, `--force-with-lease` | `git.remote.push.force` | **destructive** |
| `git clean -f`, `-fd`, `-fdx` | `filesystem.write` | **destructive** |
| `rm` | `filesystem.write` | write |
| `rm -r`, `-rf`, or a glob target | `filesystem.write` | **destructive** |
| `mv`, `cp` | `filesystem.write` | write |
| `chmod`, `chown`, `chgrp` | `filesystem.write` | write |
| `sudo`, `doas`, `su`, `runas` | `shell.execute.elevated` | **destructive** |
| `curl`, `wget`, `nc`, `ssh`, `scp`, `rsync` (remote) | `network.connect` | network |
| `docker`, `podman`, `kubectl`, `helm` | `network.connect`, `process.spawn` | **destructive** |
| `terraform apply`, `pulumi up` | `network.connect` | **destructive** |
| `npm/pip/cargo/gem install` | `network.connect`, `filesystem.write` | write |
| `npm publish`, `cargo publish`, `twine upload` | `network.connect` | **destructive** |
| `mysql`, `psql`, `redis-cli`, `mongosh` | `network.connect` | write |
| `dd`, `mkfs`, `fdisk`, `shred` | `filesystem.write.outside` | **destructive** |
| `:(){ :\|:& };:` and shell-bomb shapes | — | **refused outright** |

`destructive` class always requires confirmation regardless of approval policy, and always stops an
unattended run (I-032).

The classifier is **advisory for capability resolution and authoritative for the confirmation class.**
It is not a security boundary — the sandbox is. A cleverly obfuscated destructive command that defeats
classification still cannot write outside the writable roots or reach the network. This distinction
matters: treating the classifier as the boundary would be a mistake, since command-line parsing is
adversarially hard.

### 6.6 Windows

Different enough to need explicit handling: `pwsh` preferred, `cmd` fallback; quoting differs
substantially and is handled by the platform layer rather than by string formatting; Job objects instead
of process groups; no `rlimit`, so Job object limits are used; path separators and drive letters
normalized. See [`platform.md`](./platform.md).

---

## 7. Git

Git is a first-class subsystem, not shell commands with special names. Operations go through
`tools/git.py`, which uses the git CLI (not a library — the CLI is the compatibility surface everyone
else targets) with structured parsing.

### 7.1 Repository state detection

Detected at session start and refreshed on demand:

| State | Handling |
|---|---|
| Not a repository | Git tools unavailable; checkpointing uses file snapshots only |
| Bare repository | Read-only; no working tree to modify |
| Detached HEAD | Allowed; commits warn that they are unreachable from any branch |
| Mid-rebase / mid-merge / mid-cherry-pick | **Detected and reported.** Most write operations refused until resolved |
| Unresolved conflicts | Reported with the conflicted paths; commits refused |
| Dirty working tree | Reported; destructive operations require confirmation |
| Submodules | Enumerated; each has its own status; treated as separate roots for path policy |
| Worktrees | The active worktree is the root; sibling worktrees are outside writable roots |
| Git LFS | Pointer files detected and reported as pointers, not content. Editing a pointer is refused |
| Shallow / partial clone | Detected; history operations warned as incomplete |
| Sparse checkout | Absent paths reported as sparse-excluded, not missing |

The mid-operation states matter because an agent that runs `git commit` during an unresolved rebase
produces a confusing mess that is tedious to unwind.

### 7.2 Commit policy

- **Commits are never automatic.** Only on explicit user request (see [`AGENT.md`](../AGENT.md) §6.1).
- Specific paths preferred over `git add -A`, so unrelated working-tree changes are not swept in.
- **Secret scan runs on the staged diff before every commit**, independent of the user's own hooks
  (I-035). A user's hooks may be absent or bypassed; this check is not.
- The user's git hooks are **never** skipped. `--no-verify` requires explicit instruction — silently
  bypassing a team's pre-commit checks is not the agent's call.
- Commit message authorship is attributed per config; the default notes agent involvement.
- Empty commits refused.

### 7.3 Protected operations

| Operation | Requirement |
|---|---|
| Push | Confirmation; `git.remote.push` |
| Push to `main`/`master`/configured protected branch | Confirmation naming the branch; `git.remote.push.protected` |
| Force push | Confirmation showing what would be lost; `git.remote.push.force` |
| `reset --hard` | Confirmation; **auto-checkpoint first** |
| `clean -fd` | Confirmation listing what would be deleted; auto-checkpoint |
| Rebase / amend / history rewrite | Confirmation; auto-checkpoint |
| Branch delete (unmerged) | Confirmation |
| Remote add/modify | Confirmation — a new remote is an exfiltration destination |
| `checkout` discarding uncommitted changes | Confirmation; auto-checkpoint |

**Auto-checkpoint before destructive git operations** is the safety net that makes this recoverable.
`git reset --hard` discards uncommitted work permanently as far as git is concerned; the checkpoint
holds it (see [`data-model.md`](./data-model.md) §Checkpoints).

### 7.4 Push safety

Before any push: verify the remote URL against the recorded expected value, and warn if it changed —
a modified remote is how a commit ends up in an attacker's repository (Chain 2 in
[`threat-model.md`](./threat-model.md)). Show the commit range and the diffstat being pushed. Never push
a branch the agent did not create without explicit instruction naming it.

Default branch behaviour: push to a new branch with upstream tracking, never directly to `main` unless
explicitly asked.

---

## 8. Web

All requests go through `core.egress` and the `NetworkPolicy` in [`security.md`](./security.md) §2.
This section covers only what is specific to the tools.

### 8.1 `fetch`

| Concern | Behaviour |
|---|---|
| Scheme | `https` only; `http` upgraded, refused if the upgrade fails |
| Timeouts | 10s connect, 30s total |
| Redirects | Max 3, each hop fully re-validated (I-015) |
| Response size | 10 MiB cap, enforced during streaming, not after |
| Decompression | 50 MiB cap, ratio cap 100:1 — defeats gzip bombs |
| Content types | `text/*`, `application/json`, `application/xml`, and PDF via extraction. Everything else refused with its type |
| HTML | Extracted to text. Scripts and styles dropped; **hidden text is extracted and marked** rather than dropped, because letting an attacker choose what the agent sees by hiding it is the wrong default |
| JavaScript | Never executed. No headless browser |
| Cookies | Not stored, not sent |
| Auth | No credentials attached. An authenticated fetch is an explicit, audited configuration |
| Downloads | Not written to disk by `fetch`; content is returned or blobbed |
| Archives | Never auto-extracted |
| User-Agent | Identifies Kalash and its version |
| Caching | Short-lived local cache keyed by URL and validators, respecting `Cache-Control` |
| Untrusted | Content wrapped in a delimited untrusted block (I-033) |

Extracted content notes its provenance URL and fetch time, so the agent can cite it and the user can
tell how stale it is.

### 8.2 `web_search`

Provider-agnostic behind an adapter. Results are untrusted (I-033) — a search result snippet is
attacker-influenceable through SEO. Result count capped; queries are logged at `INFO` (they are not
content). Following a result requires a separate `fetch`, which re-applies every check in §8.1.

---

## 9. Built-in tool reference

| Tool | Side effect | Capabilities | Notes |
|---|---|---|---|
| `read` | READ | `filesystem.read` | Returns `content_digest` for I-012 |
| `write` | WRITE | `filesystem.write` | Atomic; digest required for overwrite |
| `edit` | WRITE | `filesystem.write` | Exact match; fails on 0 or >1 matches |
| `multi_edit` | WRITE | `filesystem.write` | All-or-nothing within one file |
| `glob` | READ | `filesystem.read` | Respects ignore files |
| `search` | READ | `filesystem.read` | ripgrep-backed, context lines |
| `list` | READ | `filesystem.read` | Directory listing with types |
| `shell` | EXEC | `shell.execute` + dynamic | §6 |
| `git` | READ/WRITE | `git.*` | §7 |
| `fetch` | READ | `network.connect` | §8.1 |
| `web_search` | READ | `network.connect` | §8.2 |
| `todo` | NONE | — | Surfaced in the UI, not only the transcript |
| `recall` | READ | `memory.read` | [`memory.md`](./memory.md) |
| `remember` | WRITE | `memory.write` | Queued, non-blocking |
| `forget` | WRITE | `memory.forget` | Tombstone-first (I-018) |
| `task` | EXEC | `agent.spawn` | Isolated context (I-028) |
| `notebook_edit` | WRITE | `filesystem.write` | Cell-aware `.ipynb` |

`read` over `cat`, `edit` over `sed`, `search` over `grep`, `glob` over `find`. The dedicated tools give
the user visibility, respect the permission model, and return structured results with digests. `shell`
is for what genuinely needs a shell: builds, tests, git, package managers.

---

## 10. Testing

| Concern | Approach |
|---|---|
| Lifecycle order | Instrumented pipeline asserting the exact stage sequence; a test that fails if a stage is skipped or reordered (I-004) |
| Path resolution | Corpus: symlinks, chains, `..`, Unicode NFC/NFD, case variants, Windows short names, junctions (I-009) |
| Special files | Real FIFOs, devices, sockets in a fixture tree — assert refusal without hanging |
| Atomicity | Kill mid-write at multiple points; assert original intact (I-008) |
| Stale read | Concurrent modification between read and edit; assert `KALASH_TOOL_STALE_READ` (I-012) |
| Encoding / newlines | Round-trip UTF-8, UTF-16, latin-1, CRLF, LF, mixed, no-trailing-newline; assert byte-identical outside the edit |
| Metadata | Executable bit and mode survive a write |
| Process trees | Spawn grandchildren, timeout, assert no survivors (I-013) |
| Environment | Assert every stripped variable is absent in the child |
| stdin | Interactive-prompting commands fail fast rather than timing out |
| Classification | Corpus of commands including obfuscated destructive ones; assert conservative outcomes |
| Git states | Fixture repos: mid-rebase, conflicts, detached, submodules, worktrees, LFS, shallow, sparse |
| Web | Local server: redirect chains, oversized responses, bombs, wrong content types, hidden-text injection |
| Truncation | Assert head-and-tail retention and marker accuracy |

---

*See also: [`capabilities.md`](./capabilities.md) for authority resolution,
[`permissions.md`](./permissions.md) for the decision algorithm and approval UX,
[`security.md`](./security.md) for network policy and redaction,
[`data-model.md`](./data-model.md) for checkpoints and side-effect records,
[`invariants.md`](./invariants.md) I-004, I-008 through I-013.*
