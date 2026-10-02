# 03 — Permission Engine and Sandbox

## Contents
1. Two layers, four modes
2. Shell command analysis
3. Path resolution
4. Rule language and evaluation order
5. Approval flow
6. Sandbox tiers
7. Concrete sandbox recipes
8. Network egress control
9. Two-phase execution (setup vs agent)
10. Resource limits and cleanup
11. Tests

---

## 1. Two layers, four modes

| Layer | Question | Mechanism | Failure mode if missing |
|---|---|---|---|
| **Policy** | *Should* this action run, and does a human need to approve? | Deterministic rules over a declared `Effect` | Annoying or unsafe prompts |
| **Sandbox** | *What can it touch* if it runs (or if policy was fooled)? | OS/container isolation | Escape = host compromise |

Approval modes (names are yours; behavior mirrors what Codex/Claude Code expose):

| Mode | Reads | Workspace edits | Shell | Network | Use when |
|---|---|---|---|---|---|
| `read_only` / plan | auto | deny | read-only allowlist only | deny | exploration, planning |
| `suggest` (default) | auto | ask | ask (except allowlist) | ask | interactive, first run in a repo |
| `auto_edit` | auto | auto | ask unless allowlisted | ask | trusted repo, interactive |
| `auto` (sandboxed) | auto | auto | auto **inside sandbox**, ask to leave it | deny; ask to enable | daily driver with real sandbox |
| `bypass` | auto | auto | auto | auto | **only** in disposable, network-isolated VM/container (CI, evals). Refuse to enable otherwise. |

Rule: `bypass` must verify at startup that it is inside an isolated environment (marker file/env set by the provisioner, not by the user's shell) or hard-fail.

## 2. Shell command analysis

Never regex the raw string. Parse, then reason.

Pipeline:
1. **Parse** with `bashlex`/`tree-sitter-bash` into a command tree. Parse failure → `ask` (interactive) or `deny` (headless).
2. **Decompose** compound structures: `a && b`, `a || b`, `a ; b`, pipelines, subshells `( … )`, command substitution `$( … )` and backticks, process substitution `<( … )`, heredocs, brace groups, `for/while/if`. **Every** simple command must be independently allowed; the strictest decision wins.
3. **Resolve wrappers.** Unwrap and re-analyze: `env VAR=x cmd`, `command cmd`, `nohup`, `time`, `timeout 5 cmd`, `nice`, `sudo` (deny/ask), `xargs cmd`, `find … -exec cmd {} ;`, `bash -c "<string>"`, `sh -c`, `python -c`/`node -e`/`perl -e` (treat as arbitrary code → at least `ask`, unless sandboxed + allowed by mode), `ssh host cmd`.
4. **Extract effects** for each simple command: argv, redirections (`>`, `>>`, `2>`, `&>` → write targets), cwd changes, env assignments, referenced paths (resolved per §3), hosts (`curl`, `wget`, `git clone/push/fetch` remotes, `pip/npm install` registries).
5. **Classify** using a command table:

```python
SAFE_READONLY = {"ls","cat","head","tail","wc","stat","file","pwd","echo","printf","which","rg","grep","find",   # find w/o -exec/-delete/-fprint
                 "sort","uniq","cut","tr","diff","tree","du","df","date","basename","dirname","realpath"}
GIT_READONLY  = {"status","diff","log","show","branch","rev-parse","ls-files","blame","grep","describe","remote -v"}
ALWAYS_ASK    = {"git push","git remote add","git config","npm publish","pip install","npm install","curl","wget",
                 "docker","kubectl","ssh","scp","rsync","chmod","chown","ln -s","kill","pkill","mv","cp"}   # → ask unless rule allows
DENY          = {"sudo","su","mkfs","dd of=/dev/","shutdown","reboot","mount","umount","iptables","crontab"}
```

Flag-sensitive entries need per-flag logic (`find -exec`, `sed -i`, `tar x`, `git -c core.sshCommand=…`, `git clean -fdx`, `rm -r`, `rm -f`, `curl -o`, `awk` system()). Maintain these as **data tables with tests**, not scattered `if`s.

6. **Taint checks:** reject `curl|sh`, `wget -O- | bash`, `eval "$(…)"`, base64-decode-to-exec patterns → `deny`.

Fail-closed principle: unknown command + unknown flags = `ask`, never `allow`.

## 3. Path resolution

```python
def resolve_in_workspace(raw: str, ws_root: Path, cwd: Path, *, must_exist=False) -> Path:
    if "\x00" in raw: raise PathDenied("NUL in path")
    p = Path(os.path.expandvars(os.path.expanduser(raw)))     # decide policy on $VAR: simplest = DENY any $ or ~ in tool paths
    p = p if p.is_absolute() else cwd / p
    real = Path(os.path.realpath(p))                          # resolves symlinks and ..; for not-yet-existing files resolve parent
    real = Path(unicodedata.normalize("NFC", str(real)))
    root = Path(os.path.realpath(ws_root))
    if not (real == root or root in real.parents):
        raise PathDenied(f"{raw} resolves outside workspace")
    return real
```

Hardening list:
- Resolve the **parent** for create operations (file doesn't exist yet) and re-check after creation (TOCTOU): open with `O_NOFOLLOW`, or use `openat` relative to a root fd. In a real sandbox, bind mounts remove this class of bug; policy is defense-in-depth.
- Case-insensitive filesystems (macOS/Windows): compare with `os.path.normcase` and real casing from the FS.
- Protected paths inside the workspace, **read-only or approval-required even in `auto`**: `.git/` (hooks, config), `.harness/`, `.claude/`, `.codex/`, `.env*`, `.ssh/`, CI config (`.github/workflows`), package-manager rc files (`.npmrc`, `.pypirc`), shell rc files, `Makefile`/`package.json` scripts if you want to stop persistence via build hooks (optional, high-friction).
- Symlinks: creating a symlink that points outside the workspace is denied; following existing ones resolves first (above).
- Max path length, no device files (`/dev/*`), no `/proc/*/` writes.

## 4. Rule language and evaluation order

User/org rules (YAML/TOML), modeled on the patterns used by Claude Code and Codex:

```yaml
version: 3
mode: auto
rules:
  deny:
    - tool: bash
      argv: ["rm", "-rf", "/*"]
    - tool: "*"
      writes: ["**/.env*", "**/.git/hooks/**"]
  allow:
    - tool: bash
      argv_prefix: ["pytest"]
    - tool: bash
      argv_prefix: ["git", "diff"]
    - tool: edit_file
      writes: ["src/**", "tests/**"]
    - tool: web_fetch
      hosts: ["docs.python.org", "*.readthedocs.io"]
  ask:
    - tool: bash
      argv_prefix: ["git", "push"]
```

Evaluation order (first match wins, top to bottom):
1. **Hard invariants** (compiled in, not configurable): path escape, protected paths, `DENY` set, secret reads → `deny`.
2. **Org/managed policy** deny rules (cannot be overridden by user/project config).
3. **User/project deny** rules.
4. **Mode gate** (e.g. `read_only` denies all writes).
5. **Allow rules** (project < user < session; narrower scope wins ties).
6. **Ask rules** and default for the risk level in current mode.
7. Default: `ask` interactive, `deny` headless.

Precedence of sources (strong → weak): managed/org > CLI flags > user config > project config > defaults. **Project config may only narrow privileges** unless the user has trusted the repo (a cloned repo's `.harness/config` must never grant itself `bypass`).

`PolicyResult.rule_id` is recorded in every `ToolApproved/ToolDenied` event; the policy version hash is stored per run.

## 5. Approval flow

```text
ask → emit ApprovalRequested{id, tool, effect summary, risk, diff preview}
    → UI/API responds: allow_once | allow_session(rule) | allow_always(rule→project/user config) | deny(+feedback)
    → timeout (headless: immediate; interactive: configurable) → deny
```

- Show the **effect**, not just the raw command: files to be written (with diff), hosts, resolved argv.
- "Allow always" proposes the *narrowest* rule (e.g. `argv_prefix: ["npm","run","test"]`), never `bash(*)`.
- Denial feedback goes back to the model as a tool error with the user's reason.
- Approvals bind to the exact effect hash; a changed command re-asks.
- Rate-limit approval prompts (batch parallel asks into one UI) to avoid approval fatigue; fatigue is a security risk.

## 6. Sandbox tiers

| Tier | Technology | Isolation | Overhead | Use for |
|---|---|---|---|---|
| T0 | none (policy only) | none | 0 | never in production |
| T1 | **bubblewrap** + seccomp + `unshare-net` (Linux); **Seatbelt** `sandbox-exec` profiles (macOS); Landlock for FS (Linux ≥5.13) | FS/net/PID namespaces on the host kernel | ms | local CLI for trusted users |
| T2 | **Rootless container** (Docker/Podman) with dropped caps, seccomp, read-only rootfs, cgroup limits | + image reproducibility | 0.3–2 s | CI, evals, service with semi-trusted users |
| T3 | **gVisor (runsc)** or Kata | user-space kernel / lightweight VM | +10–30% syscalls | multi-tenant service |
| T4 | **Firecracker/microVM** (or cloud sandboxes) per run | hardware virtualization | 100–500 ms boot | hostile code, untrusted tenants |

Choose by threat model: single developer on own machine → T1; evals on own infra → T2; SaaS with other people's repos → T3/T4.

## 7. Concrete sandbox recipes

### bubblewrap (Linux, T1) — workspace-write, no network

```bash
bwrap \
  --die-with-parent --new-session --unshare-all \
  --ro-bind /usr /usr --ro-bind /bin /bin --ro-bind /lib /lib --ro-bind /lib64 /lib64 \
  --ro-bind /etc/resolv.conf /etc/resolv.conf \
  --proc /proc --dev /dev --tmpfs /tmp --tmpfs /home/sandbox \
  --bind "$WORKSPACE" /workspace \
  --ro-bind "$WORKSPACE/.git" /workspace/.git \        # code edits yes, hook/config tampering no
  --chdir /workspace \
  --setenv HOME /home/sandbox --setenv PATH /usr/bin:/bin \
  --cap-drop ALL \
  -- bash -lc "$CMD"
# Network enabled only when policy says so: replace --unshare-all with --unshare-pid --unshare-ipc --unshare-uts
# and route via the egress proxy (§8). Add --seccomp <fd> with a filter denying ptrace, mount, keyctl, bpf, userfaultfd.
```

### macOS Seatbelt (T1)

Generate an SBPL profile: `(deny default)`, allow process-exec/fork, allow `file-read*` on system dirs, allow `file-write*` only under workspace and tmp, `(deny network*)` unless enabled, protect `.git`. Run: `sandbox-exec -p "$PROFILE" -- bash -lc "$CMD"`. (Apple marks `sandbox-exec` deprecated but it remains what several shipping agent CLIs rely on; wrap behind your `SandboxBackend` interface so you can swap.)

### Docker (T2) — one container per run, exec per tool call

```bash
docker run -d --name run_$RUN_ID \
  --user 1000:1000 \
  --network none \                              # or a user-defined network with egress proxy only
  --read-only --tmpfs /tmp:rw,size=512m --tmpfs /home/agent:rw,size=256m \
  --cap-drop ALL --security-opt no-new-privileges \
  --security-opt seccomp=harness-seccomp.json \
  --pids-limit 256 --memory 4g --memory-swap 4g --cpus 2 \
  --ulimit nofile=4096 --ulimit nproc=512 --ulimit fsize=1073741824 \
  -v "$WORKSPACE":/workspace:rw \
  -v "$WORKSPACE/.git":/workspace/.git:ro \
  --workdir /workspace \
  $IMAGE sleep infinity
# per call: docker exec --user 1000 -w /workspace run_$RUN_ID bash -lc "$CMD"  (new process group; kill via `docker exec … kill -- -$PGID`)
# teardown: docker rm -f run_$RUN_ID ; delete workspace unless artifacts requested
```

Never mount the Docker socket, `$HOME`, `~/.ssh`, cloud credentials, or `/` into the container. Pin image by digest. Rootless Docker/Podman preferred.

### Backend interface

```python
class SandboxBackend(Protocol):
    async def provision(self, spec: SandboxSpec) -> SandboxHandle: ...     # image, mounts, limits, net policy
    async def exec(self, h: SandboxHandle, argv: list[str], *, env, cwd, timeout, cancel) -> ExecResult: ...
    async def snapshot(self, h) -> SnapshotId: ...                         # for rewind / replay
    async def diff(self, h, since: SnapshotId) -> list[FileChange]: ...
    async def destroy(self, h) -> None: ...
    def describe(self) -> SandboxFingerprint: ...                          # recorded in run metadata
```

`doctor` must prove the backend works by executing the escape tests (§11), not by checking that a binary exists.

## 8. Network egress control

Default **deny**. When enabled, never give the sandbox raw internet:

```text
sandbox ──(only route)──► egress proxy (HTTP CONNECT + DNS resolver) ──► allowlist ──► internet
```

- Proxy enforces domain allowlist (SNI/Host), blocks RFC1918/link-local/metadata IPs (`169.254.169.254`, `fd00:ec2::254`), resolves DNS itself (prevents DNS-rebinding and DNS exfil), logs every request (method, host, bytes) as events, caps bytes/requests.
- Package registries (`pypi.org`, `registry.npmjs.org`, `crates.io`) are common allowlist entries; arbitrary domains are not.
- For maximum safety use **mirrors/caches** pre-populated in the image and keep network off during the agent phase (§9).

## 9. Two-phase execution (setup vs agent)

Codex cloud-style pattern, effective for evals and services:

1. **Setup phase** (network allowed, scripted, no model): clone repo at pinned commit, install dependencies, build, warm caches. Snapshot the result.
2. **Agent phase** (network off or proxy-only, model-driven): runs from the snapshot.
3. **Validation phase** (network off, harness-driven): apply the diff to a *fresh* copy of the snapshot, run graders there. This blocks agents that tamper with the test runner or leave behind side-effect files.

Secrets are available only in phase 1 (and only if required), never in phase 2.

## 10. Resource limits and cleanup

- cgroup v2 / Docker flags: memory, CPU, pids, disk quota (tmpfs size or project quota), `ulimit -f`, open files.
- Tool-level: timeout, max output bytes, max processes per call.
- Run-level: max disk written, max total sandbox lifetime.
- Cleanup: `finally`-protected teardown; a reaper process that kills containers/process groups with label `harness.run_id` older than TTL (crash-safe); verify no orphans in `doctor`/CI (`docker ps --filter label=harness`).

## 11. Tests

Run these in CI against **every** backend, inside the real sandbox:

| Test | Expectation |
|---|---|
| `cat /etc/shadow`, `cat ~/.ssh/id_rsa`, `ls /home` | denied / empty / not found |
| write outside workspace (`echo x > /etc/x`, `/tmp/../etc`) | fails |
| write `.git/hooks/pre-commit` | fails |
| symlink workspace→`/etc`, then read through it | fails |
| `curl https://example.com`, `python -c "import socket; socket.create_connection(('1.1.1.1',53))"`, `nslookup` | fails when net off |
| reach `169.254.169.254` with net on | blocked by proxy |
| fork bomb `:(){ :|:& };:` | contained by pids limit; host unaffected |
| allocate 16 GB | OOM-killed inside cgroup |
| `yes > file` until disk full | quota hit; host unaffected |
| `sleep 1e6 &` then timeout | no leftover processes |
| env check | no host secrets; no `HARNESS_*` internals |
| `ptrace`, `mount`, `unshare`, `keyctl` | seccomp EPERM |
| Docker socket / host PID namespace visible | no |
