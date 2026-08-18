# Permissions

> **Status:** normative.
>
> [`capabilities.md`](./capabilities.md) defines *what* authority exists. This document defines how a
> request for it is decided, presented, recorded, and enforced: the decision algorithm, the confirmation
> class, grant semantics, the approval UX down to what happens when nobody answers, and the OS-level
> sandbox that makes the decision real. It is the enforcement companion to the capability model and is
> not readable without it.
>
> Inherited constraints: I-004, I-009, I-010, I-011, I-029, I-030, I-032, I-034. Threats addressed:
> T-J (operator error) and T-A (a repository that would like to disable its own containment).

---

## 1. Scope

Kalash separates **containment** (what the OS permits, expressed as a sandbox mode) from
**interruption** (when the user is asked, expressed as an approval policy). Conflating them yields
either a nag-fest or a footgun: a system that asks about everything trains the user to stop reading, and
a system that contains nothing has to ask about everything.

| Axis | Values | Owner |
|---|---|---|
| Sandbox mode | `read-only` · `workspace-write` · `danger-full-access` | `sandbox/policy.py` + platform backend |
| Approval policy | `untrusted` · `on-request` · `on-failure` · `never` | `permissions/policy.py` |

Defaults: `workspace-write` + `on-request`, network denied inside the sandbox. Modes are capability
presets, not a parallel mechanism — the table in [`capabilities.md`](./capabilities.md) §5 is the
definition and there is no second place where a mode means something. That table, the vocabulary, and
the attenuation rule are assumed here, not restated.

---

## 2. The decision algorithm

Runs at stage 6 of the tool lifecycle ([`tools.md`](./tools.md) §2) — after argument normalization
(stage 4) and capability resolution (stage 5), before sandbox admission (stage 7). No path reaches
`execute()` around it (I-004).

### 2.1 Pseudocode

```python
def evaluate(rq: Request) -> Decision:
    """rq: principal, project P, capability C, normalized target T, sandbox mode M,
    approval policy A, risk class R (tools.md §6.5), held: set[Capability],
    just_asked: bool (§2.4)."""
    # ── 1 · explicit deny rules: user config ∪ plugin manifest ∪ static hook deny
    for rule in deny_rules(P):
        if rule.matches(C, T):
            return DENY("deny_rule", rule.id)          # KALASH_PERMISSION_DENIED
    # ── 2 · protected paths and protected branches
    if T.is_path:
        if is_protected(T) and not per_path_override(T):
            return DENY("protected_path")              # KALASH_SANDBOX_PROTECTED_PATH  I-010
        if writes(C) and not inside_writable_roots(T):
            return DENY("outside_roots")               # KALASH_SANDBOX_PATH_DENIED     I-009
    if T.is_branch and is_protected_branch(T) and C not in branch_allowed(T):
        return DENY("protected_branch")
    # ── 3 · existing grants (§4.3). Satisfies C; does NOT skip stage 6
    g = match_grant(P, C, T, M, protocol_version)
    if g is not None:
        if g.lifetime is ONCE:
            consume(g)                                 # at admission, not at success
        held |= {C}; source = f"grant:{g.lifetime}:{g.id}"
    # ── 4 · sandbox-mode capability defaults (capabilities.md §5)
    if C not in held:
        match mode_default(M, C):
            case DENY:  return DENY("sandbox_mode")    # KALASH_PERMISSION_DENIED
            case GRANT: held |= {C}; source = f"sandbox_default:{M}"
            case ASK:   pass
    # ── 5 · approval policy
    need_prompt = False
    if C not in held:
        match A:
            case UNTRUSTED | ON_REQUEST: need_prompt = True
            case ON_FAILURE:             need_prompt = rq.prior_attempt_failed or R is not READ
            case NEVER:                  return DENY("policy_never")
    # ── 6 · confirmation class (§3) — can only ADD a prompt, never remove one
    if R is DESTRUCTIVE or in_confirmation_class(C, T, rq):
        if not rq.just_asked:
            need_prompt = True         # overrides every grant above, `always` included
    # ── the single ALLOW
    if not need_prompt:
        return ALLOW(source)
    if not is_interactive():
        return DENY("unattended")                      # KALASH_APPROVAL_REQUIRED       I-032
    return ASK(build_prompt(rq))                       # §5
```

### 2.2 Why this makes I-030 structural

`ALLOW` is returned from exactly one place, reachable only after every deny gate has declined to fire. No
later stage can revisit an earlier denial, because the earlier stage has already returned. "Deny always
beats allow" is a property of the control flow, not a rule an implementer must remember and a reviewer
must catch.

Two consequences, both being shapes people try to route around. An `always` grant cannot defeat a deny
rule: stage 1 precedes stage 3. An `always` grant cannot defeat the confirmation class: stage 6 is last
and only adds prompts. The second is deliberate — a grant answers "may you do this class of thing at
all?", a confirmation answers "is this specific instance what you want right now?" Different questions,
so one answer cannot substitute for the other.

### 2.3 Decision table

| # | Situation | Decided at | Outcome | Code |
|---|---|---|---|---|
| 1 | `filesystem.write` on `/proj/src/a.py`, `M=workspace-write` | 4 (GRANT) | allow, `sandbox_default:workspace-write` | — |
| 2 | Same, path resolves through a symlink to `/etc/hosts` | 2 | **deny**, even under `danger-full-access` | `KALASH_SANDBOX_PATH_DENIED` |
| 3 | `filesystem.write` on `.git/hooks/pre-commit` | 2 | **deny** in every mode (I-010) | `KALASH_SANDBOX_PROTECTED_PATH` |
| 4 | Row 3 with a per-path override in **user** config | 2 (override) | falls through to 3 | — |
| 5 | Row 3 with the same override in **project** config | — | ignored + warned, then **deny** | `KALASH_CONFIG_SCOPE_IGNORED` |
| 6 | `network.connect` to `api.github.com`, session grant on that host | 3 | allow, `grant:session:<id>` | — |
| 7 | `network.connect` to `evil.example`, grant covers only `api.github.com` | 3 (no match) → 4 (ASK) | prompt | — |
| 8 | `filesystem.read` on `/proj/src/a/b.py`, `always` grant on `/proj/src/**` | 3 | allow | — |
| 9 | `filesystem.read` on `/proj/srcfoo/x.py`, same grant | 3 (no match, segment boundary) | falls through | — |
| 10 | `git.remote.push.force` with an `always` grant | 6 | **prompt anyway** | — |
| 11 | Row 10, and the user's current message says "force push this branch" | 6 (`just_asked`) | allow | — |
| 12 | `shell.execute` of `rm -rf build/`, `A=never`, interactive | 6 (destructive) | prompt | — |
| 13 | Row 12 under `kalash -p` or a schedule | 6 → unattended | **stop and report** (I-032) | `KALASH_APPROVAL_REQUIRED` |
| 14 | Write inside roots, but a user-scope deny rule names the directory | 1 | **deny**, no prompt offered | `KALASH_PERMISSION_DENIED` |
| 15 | `shell.execute.elevated` (`sudo`) under `workspace-write` | 4 (DENY) | **deny**; needs a mode change, not a grant | `KALASH_PERMISSION_DENIED` |
| 16 | Unrecognized capability from a newer plugin protocol | 4 (no row) | **deny** — unknown is denied, never unconstrained | `KALASH_PERMISSION_DENIED` |

Row 15 is the one to internalize: some things are not grantable at the current mode, and offering a
prompt would imply the sandbox can be widened per call, which it cannot.

### 2.4 `just_asked`

The only bypass in the algorithm, so it is bounded tightly. It requires the action to be named in the
**user's own most recent message in the current turn** — not an earlier turn, not a standing
instruction, not `KALASH.md`, not a recalled memory, not a file comment — and to name **this action**
rather than its category, so "clean up the repo" does not authorize `git clean -fdx`. Nothing outside the
user-authored channel can set it: not a memory record asserting standing approval (I-020), not tool
output or fetched content (I-033). Without that last constraint T-A and T-E both become confirmation
bypasses, since a poisoned memory or an injected comment would skip the one gate a grant cannot override.

---

## 3. The confirmation class

Requires explicit confirmation regardless of approval policy, regardless of an `always` grant, and
regardless of sandbox mode, unless `just_asked` holds. In an unattended run these are a hard stop
(I-032).

| Group | Members |
|---|---|
| **Bulk destruction** | Recursive or glob-target deletes; `rm -r`/`-rf`; `git clean -f`/`-fd`/`-fdx` |
| **Destructive git** | Force push (`--force`, `-f`, `--force-with-lease`); `reset --hard`; `branch -D`; rebase, amend, `filter-branch`, any history rewrite |
| **Remote publication** | Push to `main`/`master`/a configured protected branch; **any** push the user did not ask for; `npm publish`, `cargo publish`, `twine upload` |
| **Commits** | Creating a commit when the user did not ask |
| **Production** | Deploys; `terraform apply`, `pulumi up`; `kubectl`/`helm` writes against a live context; write-intent connections to a live database |
| **Data destruction** | `DROP DATABASE`, `DROP TABLE`, `TRUNCATE`; bulk `UPDATE`/`DELETE` without a narrow predicate |
| **Security surface** | Removing or weakening authn/authz — deleting a policy file, loosening CORS or CSP, disabling a check |
| **Boundary crossing** | Writes outside the workspace; enabling sandbox network access; installing global system packages; `sudo`/`doas`/`runas` |
| **Trust configuration** | Modifying `git config`; adding or changing a git remote; modifying Kalash's own security config |
| **Deferred authority** | Creating a write-capable schedule (`scheduler.create.write`) |

Two entries have non-obvious reasoning. **Adding a git remote** is here because a new remote is an
exfiltration destination and the push that follows looks entirely routine (Chain 2 in
[`threat-model.md`](./threat-model.md)). **Creating a write-capable schedule** is here because it is an
agent granting authority to its own future self with nobody watching — the deferral *is* the attack
(Chain 4).

The classifier in [`tools.md`](./tools.md) §6.5 is authoritative for this class and advisory for
capability resolution. An obfuscated destructive command that defeats parsing still cannot leave the
writable roots or reach the network, because the sandbox is the boundary and the classifier is not.

### 3.1 The asking rule

State three things, then stop: **what it does**, concretely ("deletes 214 files under `build/` and
`dist/`", not "performs cleanup"); **what could go wrong**, named; **whether it is reversible**, and if a
checkpoint was taken first, that it was.

Then wait. **Do not ask and proceed in the same breath.** "This will delete your uncommitted work —
proceeding" is a notification wearing a confirmation's clothes, and it is worse than silence because it
looks like a control. The prompt is a blocking state in the machine (`ASKED`,
[`state-machines.md`](./state-machines.md) §5), not a log line.

---

## 4. Grants

### 4.1 Shape

Keyed by `(project_id, capability, normalized_target_pattern)`, persisted in `permission_grants`.

| Field | Meaning |
|---|---|
| `id` | ULID; appears in audit as `capabilities_source` → `grant:session:a1b2c3` |
| `project_id` | Grants never cross projects (I-030) |
| `capability` | Dotted capability string, exactly as prompted |
| `target_pattern` | Normalized per §4.2; `-` when the capability has no target dimension |
| `lifetime` | `once` · `session` · `always` |
| `sandbox_mode`, `protocol_version` | Values in force when the user answered; see §4.4 |
| `request_id` | The `permission_requests` row holding the exact prompt text and answer time |

`once` is **consumed at admission, not at successful completion.** Consumption must depend on the
authorization decision, not the outcome — otherwise a tool that fails and is re-issued by the model runs
twice on one approval. The cost is a re-prompt after a transient failure, accepted over a
double-application risk.

`request_id` is what makes audit reviewable: `audit_log.capabilities_source → grant.id →
grant.request_id → permission_requests` resolves to the literal text the user read and when they
answered. An audit row saying "allowed by grant" without that chain records a decision nobody can review.

### 4.2 Pattern normalization

Applied before storage and before matching, by the same function on both sides. A grant stored in one
form and matched in another is a silent authority bug.

| Target | Normalization | Pattern forms |
|---|---|---|
| Path | Full canonicalization per [`tools.md`](./tools.md) §5.1: expand, absolutize against the tool `cwd`, NFC, resolve symlinks, collapse `..` **after** resolution, case-fold for comparison only, resolve Windows junctions and 8.3 names | exact path · `<dir>/**` |
| Host | Lowercase, IDNA-encode, strip trailing dot; port is a separate dimension | exact host · `*.suffix` |
| Command | The classifier's normalized argv — first token resolved to an absolute binary path, flags in canonical order. Never the raw string | exact argv · binary-only |
| Git ref | Full ref name (`refs/heads/main`), not the short form | exact ref |
| None | `-` | `-` |

Normalizing the *command* rather than the string matters because a grant on `git push origin main`
should match `git  push  origin main`. Normalizing the *path after symlink resolution* matters for the
opposite reason: a grant on `/proj/src/**` must not match a request whose canonical target is
`/etc/hosts`, however the argument was spelled.

### 4.3 Matching semantics

A request `(P, C, T)` matches a grant `(P′, C′, Pat)` iff all three hold:

```
P == P′
C′ == C  or  C′ is a dotted ancestor of C       # filesystem covers filesystem.write
contains(Pat, T)                                 # segment-wise, on canonical values
```

| Pattern | Request | Match | Why |
|---|---|---|---|
| `/proj/src/**` | `/proj/src/a/b.py` | ✓ | `**` spans zero or more segments |
| `/proj/src/**` | `/proj/src` | ✓ | zero segments included — a grant on a tree covers its root |
| `/proj/src/**` | `/proj/srcfoo/x.py` | ✗ | segment boundary, not string prefix |
| `/proj/src/**` | `/proj/src/link` → `/etc/passwd` | ✗ | containment is tested on the resolved target |
| `/proj/src/a.py` | `/proj/src/a.py` | ✓ | exact |
| `/proj/src/a.py` | `/proj/src/` | ✗ | exact patterns never generalize upward |
| `*.github.com` | `api.github.com` | ✓ | one or more leading labels |
| `*.github.com` | `github.com` | ✗ | approving a subdomain is not approving the apex |
| `/proj/src/**` | `/proj/**` | ✗ | **a grant never widens** |

The last row is I-030 restated as a matching rule. Containment is one-directional: authority flows from
pattern to contained target, never the reverse. Nothing generalizes a grant — a `once` approval of
`/proj/src/a.py` does not become a pattern, and an `always` grant is only ever as wide as the pattern the
prompt displayed verbatim. The capability-ancestor clause carries the matching creation restriction: a
grant on a **parent** capability exists only if the prompt asked for the parent, so a request needing
`filesystem.write` never sees a prompt offering `filesystem`. Widening at the moment of asking is how
"approve always" becomes authority the user never evaluated.

### 4.4 Revocation and automatic invalidation

`kalash trust ls` / `kalash trust revoke <id>` for individual grants, `kalash trust revoke --project` for
all of them, `kalash config` for the rule sets. Bulk revocation exists because the useful operation after
a suspicious session is "undo everything I agreed to here," not "find the row."

A grant is automatically **invalid — treated as absent, not as denied** — when any recorded value differs
from the current one:

| Change | Why it cannot carry over |
|---|---|
| `project_id` | A different project is a different trust context (I-030) |
| Workspace root resolution | Patterns were canonicalized against the old root; the same text would authorize different files |
| `protocol_version` | A capability string's meaning can change across versions; fail closed on the unknown ([`capabilities.md`](./capabilities.md) §9) |
| `sandbox_mode` | The answer concerned an action inside a specific containment; widening the containment changes what was agreed |
| Content hash of a hook, skill, or plugin | I-031; tracked in `trust_grants`, invalidated on any edit |

Invalidation is re-ask, never silent allow. `kalash status --capabilities` lists stale grants with the
reason, so a user who wonders why they were asked again gets an answer instead of suspecting a bug.

---

## 5. Approval UX

### 5.1 What the prompt must display

Not "may I run a command?" A prompt the user cannot evaluate trains reflexive approval, which is a
security failure (§5.5). Every applicable field is mandatory.

| Field | Requirement |
|---|---|
| Action | One concrete line: what happens if approved |
| Exact command | Post-normalization argv, or the complete unified diff for a write. Never a summary of the diff |
| Affected paths | **Resolved**, absolute. Where the resolved path differs from the argument as written, show both and flag it — divergence is a symlink or traversal signal, and hiding it hides the attack |
| Capabilities | Each one with the argument that implied it: `git.remote.push.force ← --force` |
| Network destinations | Host, resolved IP, port, purpose. The IP, because hostname-only display hides SSRF (I-015) |
| Risk class | read · write · write-remote · network · destructive |
| Reversibility | Reversible · irreversible · "a checkpoint was taken first" |
| Grant scope per option | What each answer would persist, in the exact pattern form it would be stored |

The last row prevents the most common approval mistake: choosing "always" believing it covers the file in
front of you when it covers a tree.

### 5.2 Response options

| Key | Option | Effect |
|---|---|---|
| `a` | Approve once | Admits this call, persists nothing |
| `s` | Approve for session | `lifetime: session` grant on the displayed pattern |
| `A` | Approve always | `lifetime: always` grant, this project, the displayed pattern |
| `d` | Deny | `KALASH_PERMISSION_DENIED` envelope to the model |
| `D` | Deny with reason | Same, with the user's text as correctable feedback |
| `m` | Modify | Edit the command or arguments, then decide on the edited form |

**Deny with reason is the highest-value option and is usually missing from designs like this.** A bare
denial teaches the model nothing and it retries a variant; "no, use the staging database" redirects the
turn. It is fed back as tool-result content, not as a system instruction (I-033).

**Modify re-enters the pipeline at stage 3, not stage 6.** A modified request is a new request:
re-validated, re-normalized, re-resolved to a capability set. Re-entering at the permission stage would
evaluate a decision against arguments that were never canonicalized — exactly the hazard I-004 exists to
prevent. An edit that changes the risk class prompts again, and should.

### 5.3 What happens when the user does not respond

Answered explicitly, because an unspecified answer here defaults to whatever the event loop happens to
do, and that is not a security posture.

| Condition | Behaviour | Code |
|---|---|---|
| **Any timeout, ever** | Never auto-approves. No configuration makes silence mean yes | — |
| Interactive, no answer | Waits **indefinitely** in a visible pending state. The agent is idle: no model request in flight, no polling, no tokens accruing. The TUI shows the request and how long it has waited | — |
| `approval.timeout` set (default `null`) | On expiry: **denies**. For scripted use where a stuck run is worse than a failed one | `KALASH_APPROVAL_EXPIRED` |
| `SIGINT` while pending | Cancels **the tool call**, not the session. The prompt closes, the call returns cancelled, the turn continues and the model may adapt | `KALASH_CANCELLED` |
| Second `SIGINT` within 2s | Ends the turn (I-029). Two intents need two gestures | `KALASH_CANCELLED` |
| Terminal disconnected, stdin closed, SSH dropped | **Denies**, request recorded as abandoned, run stops and reports what it was asking | `KALASH_APPROVAL_ABANDONED` |
| Process killed while pending | Row is `ASKED` with an expired lease; recovery resolves it to `ABANDONED` and reports ([`state-machines.md`](./state-machines.md) §12) | `KALASH_APPROVAL_ABANDONED` |
| Non-interactive: `kalash -p`, schedule, subagent of an unattended run | Never prompts. Stops and reports (I-032), exits non-zero; structured output carries the full pending request so a human can re-run it interactively | `KALASH_APPROVAL_REQUIRED` |

Indefinite waiting is chosen over a default timeout deliberately. A user who steps away and returns to a
waiting agent has lost nothing; one who returns to a denied run has lost the turn's work; one who returns
to an *approved* run has lost the security model. Billing matters too — a pending prompt must hold no
open model stream, or waiting costs money and the idle timeout in
[`model-gateway.md`](./model-gateway.md) §6 kills the turn out from under the user.

### 5.4 Batch approval

One assistant turn may produce several calls needing approval. Batching is allowed, with constraints:
every item is rendered **in full** (each command, each diff, each resolved path — no collapsed "3 file
writes" summaries); every item is **individually approvable and individually rejectable**, so approving
four of six is a normal outcome and the rest return denied for the model to adapt to; a bulk affirmative
is offered **only after full itemization** and creates per-item `once` grants, never `always` and never
over items not rendered; items are ordered by risk class with destructive last, so the dangerous one is
not the fourth of seven; and identical pending requests are deduplicated into one item with a count.

A single "approve all 7" over unrendered items is not batching; it is a blanket grant with extra steps.

### 5.5 Prompt fatigue is a security failure mode

A prompt is only a control while the user reads it. Twenty prompts an hour produce a reflex, and the
reflex approves the twenty-first — the one that mattered. Prompt volume is therefore a security metric,
not a UX preference.

The sandbox is what keeps volume low. Because `workspace-write` contains writes to the resolved roots at
the OS level, ordinary editing needs no prompt at all: containment already made it safe. What remains is
the small set the sandbox cannot make safe — leaving the box, reaching the network, rewriting history,
touching production. Those carry signal precisely because they are rare.

Volume is managed by containment and deduplication, never by widening default grants, which trades a
visible cost for an invisible one. Prompt count per session is recorded, and `kalash doctor` flags a high
rate as the configuration problem it usually is: a project whose test suite needs `network.connect`
should grant it once, not answer it forty times.

---

## 6. Sandbox enforcement

### 6.1 Backends

| Platform | Mechanism | Enforces |
|---|---|---|
| Linux ≥ 5.13 | Landlock LSM + seccomp-bpf | Filesystem roots, socket creation. Unprivileged, no setuid helper |
| Linux, no Landlock | bubblewrap | Mount + network namespaces. Coarser; requires `bwrap` on `PATH` |
| macOS | Seatbelt (`sandbox-exec`) | Filesystem roots, network, process ops. Profile generated per session from the resolved roots |
| Windows | AppContainer + Job objects | Filesystem ACLs, network capability, process-tree limits. Job objects also supply the caps `rlimit` gives elsewhere |
| Anything else | none | Refuses `read-only` and `workspace-write`; see §6.2 |

### 6.2 Fails closed (I-011)

If the requested mode cannot be enforced — no Landlock, no `bwrap`, kernel too old, capability missing
inside a container, restricted CI runner — the sandbox **refuses** with
`KALASH_SANDBOX_UNAVAILABLE`, naming the backend it wanted and why it could not be established. It does
not run unconfined while reporting the mode. Proceeding requires an explicit downgrade to
`danger-full-access`, which is a visible act with a persistent indicator, and protected paths still
apply after the downgrade.

A sandbox that degrades to no sandbox while still reporting `workspace-write` is worse than no sandbox at
all: unsandboxed, the user knows they are exposed, whereas with a lying sandbox they approve commands and
clone repositories on the strength of a protection that does not exist.

`kalash doctor` reports the backend and its enforcement level **per dimension**:

```
SANDBOX   mode workspace-write   backend landlock (kernel 6.8)   enforcement full
          filesystem enforced · network enforced · process enforced (seccomp)
          writable roots: /home/shivam/proj, /tmp/kalash-ses_01JQ.../
          protected overrides: none
```

Per-dimension reporting matters because degradation is rarely total: bubblewrap gives strong filesystem
and network isolation but no syscall filtering, and "sandboxed: yes" would flatten a distinction the user
should be able to see.

### 6.3 Writable roots resolution

Resolved at session start, re-resolved on workspace change. Union of: the **workspace root**
(`git rev-parse --show-toplevel`, else the session `cwd`); a **per-session temp directory**
(`$TMPDIR/kalash-<session_id>/`, mode `0700` — not all of `$TMPDIR`, because other processes' temp files
are not the agent's business and a shared temp directory is a cross-process tampering surface);
**configured additional roots**, user scope only (§8); and **each submodule** as a separate root, because
a submodule is a different repository with a different trust context.

Minus: every protected path (§6.4, a subtraction no root inclusion undoes), `.kalashignore` `deny:` trees
([`tools.md`](./tools.md) §5.7), and sibling git worktrees, which share `.git` but lie outside this
session's root ([`state-machines.md`](./state-machines.md) §13).

Read roots are the writable roots plus readable ancestors as the mode permits.
`filesystem.read.outside` is a separate capability so that reading a system header does not require the
authority to write one.

### 6.4 Protected paths

Denied in **all** modes including `danger-full-access`, for all principals including subagents and
scheduled runs (I-010).

| Path | Why |
|---|---|
| `.git/config` | Sets remotes, `core.hooksPath`, and `core.*`. A write here redirects pushes |
| `.git/hooks/` | **A write here converts a file edit into arbitrary code execution on the user's next commit.** The agent is not the actor at that point; the user is |
| `.env*` | Credentials by convention. Matched after NFC normalization and case folding |
| `~/.ssh/` | A1 — key theft, and `authorized_keys` persistence |
| `~/.aws/`, `~/.gnupg/` | A1 |
| `~/.kalash/credentials*` | A1, plus self-modification of the trust store |
| OS keyring stores | A1 |

`.git/hooks/` is called out because it looks like an ordinary directory and is not. It is a
deferred-execution primitive with the user's own hands on the trigger, which is why it is protected
rather than merely confirmation-class.

Overrides are **per-path only**, user scope only, warned at every session start:

```toml
[sandbox.protected_paths]
allow = [".env.example"]      # exact paths or globs; never a mode-wide disable
```

There is no `protected_paths.enabled = false`. A blanket disable is the shape of a setting added to work
around one file and never removed.

### 6.5 Adversarial test requirements

The sandbox is a **security boundary, not a config flag.** Without the corpus below the feature is not
done — a boundary nobody has attacked is a boundary nobody has measured.

| Class | Cases |
|---|---|
| Symlinks & traversal | Link to `/etc`; chains ≥ 8 deep; target created after the check; dangling link; link as a non-final component; `..` sequences; `..` after a symlink; absolute path in a relative position; `//` and `.` noise |
| Procfs | `/proc/self/cwd`, `/proc/self/root`, `/proc/self/fd/N`, `/proc/<pid>/mem` |
| TOCTOU | Path becomes a symlink between check and open (`O_NOFOLLOW` on the final component); directory component swapped mid-resolution; rename races under load |
| Re-exec | Subprocess spawning a subprocess; `setsid` detachment; a binary re-execing itself with new argv |
| Environment | `LD_PRELOAD`, `LD_LIBRARY_PATH`, `LD_AUDIT`, `DYLD_INSERT_LIBRARIES`, `NODE_OPTIONS`, `BASH_ENV`, `GIT_SSH_COMMAND` — assert each absent in the child |
| Windows | Junctions; reparse points; 8.3 names (`PROGRA~1`); alternate data streams; UNC and `\\?\` prefixes; drive-relative paths |
| Unicode & case | NFC vs NFD of every protected filename; homoglyphs; zero-width characters; RTL overrides; `.ENV`/`.Env` on case-insensitive filesystems; case-preserving-insensitive collisions |
| Network | Direct socket from a subprocess; `curl`/`nc` under denial; DNS attempts; unix socket to a local service |

Every case runs against every backend, fallbacks included. A suite that only covers Landlock has not
tested what most CI runners actually get.

---

## 7. Interaction with hooks

`PreToolUse` hooks run at stage 8 — **after** permission evaluation and sandbox admission. A hook must
not be the thing that stops a path traversal, because a hook is user code that may be absent and the
boundary cannot depend on it.

| Hook outcome | Effect |
|---|---|
| exit `0` | Proceed; stdout fed back as context |
| exit `2` | **Deny.** `KALASH_HOOK_DENIED` with stderr verbatim as correctable feedback |
| exit other | Non-blocking error, logged to `hook_runs`, execution continues |
| stdout `permissionDecision: "ask"` | Forces a prompt even where the algorithm decided allow |
| stdout `permissionDecision: "deny"` | Equivalent to exit `2` |

```json
{"hookSpecificOutput": {"permissionDecision": "ask",
                        "permissionDecisionReason": "touches migrations/"}}
```

**Hook denials are final (I-034)** — honoured at any depth, in any cycle, under any retry. The
loop-protection logic that refuses to re-enter nested hooks beyond depth 1 must never skip a *denial*:
loop protection is a resource control, and treating it as grounds to ignore a deny would make it an
authorization bypass.

**Hooks cannot grant authority they do not hold.** A hook's set is the triggering principal's ∩ the
hook's declared set ([`capabilities.md`](./capabilities.md) §3); no decision value manufactures a missing
capability. But **denial needs no grant, because denial is not authority** — the single asymmetry in the
attenuation model, and safe because adding a constraint cannot escalate. It is also why `PreToolUse` is
where a project encodes policy the model cannot argue with: a `PostToolUse` hook cannot un-run a command,
and pretending otherwise would be dishonest about what is enforceable.

---

## 8. Configuration

Security-relevant keys are read **only** from `~/.kalash/settings.json` and the user-scope environment. A
value found in `.kalash/settings.json`, `.kalash/settings.local.json`, or a project-supplied environment
file is **ignored with a warning** (`KALASH_CONFIG_SCOPE_IGNORED`) naming the file, the key, and the
discarded value.

| Key | Governs |
|---|---|
| `permissions.sandbox` | Sandbox mode |
| `permissions.approval` | Approval policy |
| `permissions.network`, `[network]` | Network enablement, host allowlists, `allow_private` |
| `memory.egress.*` | Egress mode, artifacts, audit |
| `capabilities.*` | Capability grants and deny rules |
| `sandbox.protected_paths.*` | Protected-path overrides |
| Trust and permission grants | Not in config at all — they live in the database |

Layered precedence is correct for preferences (model choice, output style, timeouts, ignore patterns) and
wrong for authority, for one reason: otherwise "clone this repo" becomes "disable your own sandbox"
(T-A). A repository is attacker-controlled input; if `.kalash/settings.json` could set
`sandbox = "danger-full-access"`, reading a stranger's issue reproduction would be equivalent to running
it unconfined, and the user's configured posture would be the one thing the attacker gets to overwrite.
The warning is loud rather than silent because a project shipping such a value is either mistaken or
hostile, and the user should learn which.

Projects may still **narrow**: additional deny rules, additional protected paths, a narrower network
allowlist. Narrowing is safe by the same logic that makes hook denials safe — adding a constraint cannot
escalate.

---

## 9. Testing

| Concern | Approach |
|---|---|
| Algorithm ordering | Table-driven over every §2.3 row plus generated (mode × policy × capability × grant state) combinations; assert the **deciding stage**, not just the outcome |
| Deny precedence | Property test: injecting a matching stage-1 deny rule yields `DENY` regardless of all other inputs (I-030) |
| Single ALLOW | Static check that `ALLOW` is constructed in exactly one place in `permissions/policy.py`; a second site fails CI |
| Confirmation class | Corpus per §3; assert prompt-or-stop under every policy including `never` and with an `always` grant present |
| `just_asked` | Assert unsettable from memory (I-020), tool output, fetched content, `KALASH.md`, or a prior turn (I-033) |
| Grant matching | Property test over generated path trees: containment reflexive on the pattern root, segment-wise, never widening, evaluated post-resolution (I-030) |
| Grant invalidation | Mutate each of project, root, protocol version, sandbox mode, content hash; assert re-prompt, never silent allow |
| `once` consumption | A tool failing after admission re-prompts on re-issue |
| Prompt content | Snapshot every prompt shape; assert resolved paths, capability→argument attribution, destination IPs, per-option grant scope, and per-item rejectability in batches |
| No-response matrix | Every §5.3 row including killed-while-pending and closed stdin; assert **no path reaches allow**. One SIGINT cancels the call and the session survives; two end the turn (I-029) |
| Unattended | `kalash -p` and schedules hit every confirmation-class action; assert stop-and-report with structured output (I-032) |
| Sandbox fail-closed | Simulate each backend unavailable; assert refusal and that no path runs unconfined while reporting a mode (I-011) |
| Escape corpus | Every §6.5 case, per platform, per backend including fallbacks (I-009, I-010) |
| Protected paths | All modes including `danger-full-access`, all principals; NFC/NFD and case variants (I-010) |
| Hook denial finality | Deny at depth 0, 1, and past the loop-protection cutoff; honoured every time (I-034) |
| Config scope | Fixture project setting every §8 key; assert ignored, warned, posture unchanged (T-A) |
| Audit chain | For every allowed call, `capabilities_source` resolves through to a `permission_requests` row containing the prompt text |

---

*See also: [`capabilities.md`](./capabilities.md) for the authority vocabulary and resolution order,
[`invariants.md`](./invariants.md) I-004, I-009 through I-011, I-030, I-032, I-034,
[`tools.md`](./tools.md) §2 for lifecycle stages and §6.5 for the command classifier,
[`state-machines.md`](./state-machines.md) §4–§5 for the tool-call and permission-request machines,
[`security.md`](./security.md) for network policy and secret handling,
[`threat-model.md`](./threat-model.md) T-A and T-J.*
