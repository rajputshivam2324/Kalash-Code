# Security Mechanisms

> **Status:** normative.
>
> [`threat-model.md`](./threat-model.md) says what we defend against. This document specifies the
> mechanisms that do the defending: the egress gateway, network policy, secret lifecycle, redaction,
> supply chain, and logging privacy.

---

## 1. The egress gateway

### 1.1 Why it is architectural, not a helper function

The original design said "anything leaving the machine passes `core.redact` first." That is a
convention, and conventions fail predictably: a new provider adapter, written six months from now by
someone who has not read this document, constructs an `httpx.AsyncClient` and works perfectly. Nothing
breaks. No test fails. The data leaves unredacted and unaudited.

So egress is a **chokepoint with static enforcement**. There is exactly one place in the codebase that
can perform network I/O, and CI fails if that changes.

```
     model gateway ─┐
  memory providers ─┤
    MCP remote      ├──▶  core/egress.py  ──▶  network
       web tools    │      ┌──────────────┐
   notifications    │      │ 1 capability │
  plugin updates    │      │ 2 net policy │
       telemetry   ─┘      │ 3 redaction  │
                           │ 4 audit      │
                           │ 5 dispatch   │
                           └──────────────┘
```

### 1.2 Enforcement

Three layers, because one is not enough:

**Static — import-linter contract.** `pyproject.toml` declares:

```toml
[[tool.importlinter.contracts]]
name = "egress-chokepoint"
type = "forbidden"
source_modules = ["kalash"]
forbidden_modules = ["httpx", "socket", "urllib", "requests", "aiohttp", "http.client"]
ignore_imports = ["kalash.core.egress -> *"]
```

**Static — banned-pattern scan.** A CI check greps for socket construction, `subprocess` invocations of
known network binaries (`curl`, `wget`, `nc`, `ssh`, `scp`) outside `tools/shell.py`, and any
`verify=False`. Findings fail the build.

**Runtime — capability gate.** Every `egress.request()` call requires a `Capability` argument. There is
no default. A caller that does not know which capability authorizes its request cannot make it, because
the parameter is required and unset is a type error.

### 1.3 Interface

```python
async def request(
    *,
    purpose: EgressPurpose,        # MODEL | MEMORY | MCP | WEB | NOTIFY | UPDATE | TELEMETRY
    capability: Capability,        # authority; audited
    principal: PrincipalId,        # who; audited
    url: str,
    method: str = "POST",
    payload: Payload | None = None,
    redaction: RedactionProfile,   # explicit, no default
    timeout: Timeout,
    ...
) -> EgressResponse
```

`redaction` has no default value. Choosing a profile — including choosing `RedactionProfile.NONE`, which
requires a documented justification and is audited as such — is a deliberate act at the call site,
reviewable in a diff. A default would mean the safe choice happens by accident, and accidents run both
directions.

### 1.4 Pipeline

```
request
 1  capability check          missing → EgressDenied, audited            I-014
 2  network policy            §2; resolve, validate, pin address         I-015
 3  redaction                 apply profile to payload                   I-006
 4  audit                     row written BEFORE dispatch                I-007
 5  dispatch                  to the pinned address, TLS verified
 6  response handling         size cap, content-type check, decompress cap
 7  audit completion          status, bytes, duration, digest
```

Step 4 precedes step 5 deliberately. If the process dies mid-request, the audit shows an attempted
egress with unknown outcome — which is the honest record. Auditing after dispatch would let a crash
erase evidence of a transmission that did occur.

### 1.5 What the gateway does not cover

Honest boundaries:

- **Subprocess network I/O.** A `shell` command running `curl` does not pass through the gateway. It is
  contained by the OS-level sandbox network denial instead (I-014, second layer). These are genuinely
  different enforcement mechanisms for genuinely different surfaces.
- **MCP stdio servers.** A stdio server is a subprocess; its own network activity is governed by
  sandbox policy, not the gateway. Its declared capabilities are still checked before spawn.
- **DNS resolution itself** leaks the hostname to the configured resolver. Unavoidable; noted for
  completeness.

---

## 2. Network policy

### 2.1 One object, every subsystem

```python
@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    enabled: bool = False
    allowed_hosts: frozenset[str] = frozenset()      # exact or *.suffix
    denied_hosts: frozenset[str] = frozenset()       # wins over allowed
    allowed_ports: frozenset[int] = frozenset({443})
    allow_private: bool = False                      # RFC1918, loopback, link-local
    allow_plaintext: bool = False                    # http:// — off
    max_redirects: int = 3
    max_response_bytes: int = 25 * 1024 * 1024
    max_decompressed_bytes: int = 100 * 1024 * 1024
    max_decompress_ratio: int = 100
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 60.0
    proxy: str | None = None
    tls_min_version: TLSVersion = TLSVersion.TLS1_2
    pinned_cas: frozenset[Path] = frozenset()
```

Resolved once per principal and passed down. The model gateway, memory providers, MCP client, web
tools, and notifiers all consume the same object — there is no second network configuration anywhere.
That is the point: previously network settings appeared in sandboxing, config, MCP, and web tooling
independently, which is four places for them to disagree.

Per-purpose overrides narrow, never widen:

```toml
[network]
enabled = true
allowed_hosts = ["api.anthropic.com", "*.githubusercontent.com"]

[network.web]              # the web tool gets a narrower set
allowed_hosts = ["docs.python.org", "*.readthedocs.io"]
```

`allow_plaintext = false` means `http://` URLs are upgraded to HTTPS and refused if that fails.
Credentials and source code do not travel in cleartext because a docs site did not configure TLS.

### 2.2 SSRF defence

The full sequence, because partial implementations of this are the norm and they do not work:

```
1  parse URL                   reject non-http(s), reject embedded credentials (user:pass@)
2  hostname checks             denied_hosts → deny; allowed_hosts non-empty and no match → deny
3  resolve                     getaddrinfo → [A/AAAA...]
4  classify EVERY address      any in a denied range → deny the whole request
5  select + PIN                choose one validated address; connect to THAT address
6  TLS                         SNI and cert verification use the HOSTNAME, not the IP
7  redirect                    for each hop, restart at step 1 with the new URL
```

**Step 5 is the one that matters and the one usually missed.** Validating a hostname then handing the
hostname to the HTTP client means the client re-resolves at connect time. An attacker controlling DNS
returns a public IP for the validation lookup and `127.0.0.1` for the connection lookup. Pinning the
socket to the specific validated address closes this. `httpx` supports it through a custom transport
that overrides address resolution while preserving SNI.

**Step 4 checks every returned address, not the first.** A hostname resolving to both a public and a
private address must be denied entirely; taking the public one and proceeding leaves the attacker a
retry.

Denied ranges by default:

| Range | Reason |
|---|---|
| `127.0.0.0/8`, `::1` | Loopback — local services, dev servers |
| `10/8`, `172.16/12`, `192.168/16`, `fc00::/7` | Private — corporate internal |
| `169.254.0.0/16`, `fe80::/10` | Link-local — **cloud instance metadata (A1)** |
| `100.64.0.0/10` | CGNAT |
| `0.0.0.0/8`, `::`, multicast, reserved | Non-routable and unexpected |
| `.local`, `.internal`, `.home.arpa` | mDNS and internal naming |

Overridable only via `allow_private` plus an explicit host allowlist — never by `allow_private` alone.
Someone running a self-hosted Supermemory at `localhost:6767` or a local Ollama needs this, and they
should have to name the host.

### 2.3 TLS

Verification always on. There is no configuration option to disable it, because such an option is
inevitably used to work around a transient problem and then never removed. Custom CAs are supported for
corporate MITM proxies via `pinned_cas`, which is the legitimate need that `verify=False` usually
stands in for. Minimum TLS 1.2. Client certificates supported for enterprise endpoints.

---

## 3. Secret lifecycle

### 3.1 Can a secret ever appear in an LLM request?

The review asked for an explicit answer. Here it is:

> **Kalash never originates a secret value into a model request.** No secret reaches the model through
> config resolution, credential handling, file reads, tool output, environment inspection, error
> messages, or memory recall — every one of those paths redacts at its boundary.
>
> **A secret can reach the model through content the user authored themselves**, and Kalash does not
> silently prevent that. If the user pastes an API key into chat, it is transmitted. Kalash detects it,
> warns before sending, offers redaction, and never persists it — but it does not override the user's
> explicit input.

The distinction is deliberate. An absolute "no secrets ever reach the model" claim would be false,
because we cannot control what a user types, and a false security claim is worse than an honest
boundary. What we can guarantee is that **no automated path** carries one.

Concretely, the paths and their handling:

| Path | Handling |
|---|---|
| Config / keyring resolution | Values never enter prompt assembly. Referenced by name only (I-036) |
| `read` on a file containing a secret | Redacted with a marker. Raw values require an explicit per-call confirmation the user must approve |
| `shell` output (`env`, `cat .env`, `aws configure list`) | Redacted before the result enters context |
| Error messages and tracebacks | Scrubbed before display, logging, or context injection |
| Memory recall | Secrets are never written, so never recalled (I-035) |
| Compaction summaries | Derived from already-redacted context |
| User-pasted text | Detected, warned, user decides |

### 3.2 Detection

Three signals, combined. Any one alone is either too noisy or too narrow.

**Pattern matching** for known-shape credentials: `AKIA`/`ASIA` AWS keys, `ghp_`/`gho_`/`github_pat_`,
`sk-`/`sk-ant-`, `xox[baprs]-` Slack, Google `AIza`, Stripe `sk_live`/`rk_live`, JWTs, PEM blocks
(`-----BEGIN ... PRIVATE KEY-----`), `postgres://`/`mysql://`/`mongodb+srv://` with credentials,
`.pem`/`.key`/`.p12` file contents. Precise, high confidence, but only catches known formats.

**Shannon entropy** over candidate tokens — length ≥ 20, high entropy, charset consistent with base64
or hex. Catches unknown formats. Noisy on its own: git SHAs, content hashes, minified JS, UUIDs, and
lockfile integrity strings all score high. Entropy alone is unusable.

**Contextual signals** — an assignment to a name matching
`(?i)(secret|token|password|passwd|api[_-]?key|credential|auth|bearer|private[_-]?key)`, presence in a
file named `.env*`/`credentials*`/`secrets*`, or a known secret-bearing config key path.

Combination rule:

```
pattern match                          → REDACT (high confidence)
entropy + contextual signal            → REDACT
entropy alone, in a code file          → FLAG, do not redact  (git SHAs, hashes)
entropy alone, in .env / credentials   → REDACT
contextual signal alone, short value   → FLAG  ("password = os.environ[...]" is not a secret)
```

**False positives are a real cost, not a hypothetical.** Redacting every 40-character hex string
breaks the agent's ability to work with git SHAs, content digests, and lockfiles — which are
everywhere in a coding context. `FLAG` means it is recorded for the user's review and shown in the UI
without being removed from content. `REDACT` means removed.

Verifier hooks are supported but off by default: a plugin can register a live-validity check for a
credential shape. Off by default because verifying a credential means transmitting it.

### 3.3 The redaction marker

Redacted values are replaced with a structured, stable marker rather than deleted:

```
[REDACTED:aws_access_key_id:d4f1a9]
```

Three fields: a fixed prefix, the detected kind, and a short digest of the value. This design does real
work:

- The agent knows something was there and what kind, so it can reason correctly ("this file contains an
  AWS key") without knowing the value
- The digest is **stable**, so the same secret redacts to the same marker across reads. The agent can
  tell "the key in `config.prod` is the same as the one in `config.staging`" — a genuinely useful
  observation — without either value
- It is unambiguous in diffs and greps
- It cannot be reversed

Digest is `HMAC(session_key, value)[:6]`, with a per-session key. Not a plain hash: a plain
`sha256(value)[:6]` of a low-entropy secret is brute-forceable, and the marker is written into audit
rows and logs.

### 3.4 Storage and resolution

Resolution order, first hit wins:

```
1  OS keyring                 (keyring: Secret Service / Keychain / Credential Manager)
2  environment variable
3  *_api_key_command          shell command producing the value on stdout — for vault integrations
4  config file literal        → WARNS, and suggests migration to the keyring
```

Config literals are supported because refusing them breaks CI and container use cases where a keyring
does not exist. They warn every time, and `kalash doctor` reports them.

Never written to disk in plaintext by Kalash. `kalash config set --secret` routes to the keyring.
Rotation is `kalash auth set <provider>`, which replaces the keyring entry and invalidates any cached
client. On an auth failure, the cached credential is dropped and re-resolved rather than retried, so a
rotated key takes effect without a restart.

### 3.5 Leak sinks

Every sink, with its control. This table is the checklist for the I-035 corpus test.

| Sink | Control |
|---|---|
| Model requests | §3.1 |
| Log files | Redaction filter in the `structlog` chain, applied to every event before any handler |
| Trace spans | Same filter; span attributes are typed and secret-typed fields are never attached |
| Audit rows | Digests only, never values |
| Memory records | Redacted at the egress gateway and again in the write pipeline |
| Checkpoints / blobs | File snapshots may contain secrets by nature. Blobs are `0600` and never leave the machine. Excluded from exports unless `--include-blobs` with a warning |
| Session exports | Redacted by default; `--raw` requires explicit confirmation and prints what it will include |
| Crash reports | Generated from redacted state; local-only by default; never auto-transmitted |
| Error messages | Exception `__str__` passes the redaction filter before display, logging, or context injection |
| Terminal scrollback | Redacted output is what is printed, so scrollback holds markers |
| Shell environment | Sanitized allowlist for subprocesses — see [`tools.md`](./tools.md) §Shell |
| Telemetry | Off by default; counters and durations only, never content |

Two sinks deserve emphasis because they are the ones normally forgotten. **Error messages** —
`ConnectionError: failed to connect to postgres://user:hunter2@db/prod` is a real leak in a real
traceback, and it lands in logs, the terminal, and often the model's context. **Crash reports** —
these dump state by definition, which is why they are built from already-redacted state rather than
redacted afterward.

---

## 4. Redaction profiles

| Profile | Applied to | Behaviour |
|---|---|---|
| `NONE` | Nothing by default | Requires justification, audited as `redaction:none` |
| `SECRETS` | Model requests | Secret detection only. Code and paths preserved — the model needs them |
| `SECRETS_PII` | Memory egress (default) | Adds email, phone, and government-ID patterns |
| `STRICT` | Memory egress with `mode = "redacted"` | Adds absolute paths → `<workspace>/rel`, usernames → `<user>`, hostnames → `<host>` |
| `METADATA_ONLY` | Telemetry | Strips all content; counters, durations, error codes only |

`STRICT` is what makes remote memory acceptable in a compliance-bound environment: the semantic content
survives ("the auth module uses a middleware chain") while machine-identifying detail does not.

Redaction is irreversible in transmitted payloads. Kalash does not keep a mapping that could reconstruct
them, because such a mapping would itself be a high-value secret store.

---

## 5. Supply chain

### 5.1 Python dependencies

Per T-D, this is the least-contained threat, so controls are preventative.

- `uv.lock` with hashes for the full transitive set, committed. `uv sync --locked` in CI and for
  installs.
- No floating version ranges. Anywhere. Including dev dependencies, which run in CI with repository
  write access.
- The direct dependency set is asserted by a test against a reviewed allowlist, so an addition cannot
  land without a deliberate change to that list and a reviewer noticing.
- Prefer wheels; avoid install-time script execution.
- Native extensions individually justified in a comment.
- Vulnerability scanning in CI, and a scheduled scan so an advisory published after merge is still
  caught.
- SBOM generated per release ([`operations.md`](./operations.md)).

Plugin dependencies resolve into an **isolated environment**, never Kalash's own. A plugin cannot
change the version of a package Kalash imports — that would be a supply-chain attack via a legitimate
plugin.

### 5.2 Plugins

| Control | Status |
|---|---|
| Version pinning at install | Required |
| Content hash verification | Required (I-031) |
| Capability manifest with `reason` for sensitive grants | Required |
| No automatic updates | Required |
| Re-approval when a capability set changes | Required |
| Source repo pinned at install, not re-resolved via index | Required |
| Isolated dependency environment | Required |
| Publisher signing and verification | Specified in [`operations.md`](./operations.md), not in the first release |
| Revocation list | Specified, not in the first release |

Until signing ships, the install prompt states plainly that trust rests on the source repository, and
`kalash plugin ls` shows the pinned source and hash for each. Overstating the guarantee would be worse
than the gap.

### 5.3 MCP servers and skills

MCP servers are third-party code with a capability set (§T-B). stdio servers are subprocesses under
sandbox policy. Remote servers are subject to network policy. Neither is trusted.

Skills are primarily markdown, which is inert, but bundled `scripts/` are executable and gated by
`skill.script` plus content-pinned trust. A skill from an untrusted repo is exactly as dangerous as a
hook from one.

---

## 6. Logging privacy

Four tiers, with explicit content rules. The design goal is that `--log-level debug` remains safe to
share in a bug report.

| Tier | Destination | May contain | Must not contain |
|---|---|---|---|
| `AUDIT` | `audit_log` table, hash-chained | Event types, capabilities, decisions, digests, byte counts, host names | Payloads, file contents, prompts, secrets |
| `INFO` | Log file + console | Operation names, durations, outcomes, counts, tool names, file **paths** | File contents, prompt text, tool output, secrets |
| `DEBUG` | Log file when enabled | Adds structured params, schema names, provider responses **as digests**, token counts | Full prompts, full tool output, secrets, memory content |
| `TRACE` | Explicit opt-in, per session, with an on-screen warning | Full prompts, full tool I/O, memory content — everything | Secrets (redaction filter still applies) |

Two consequences worth stating:

**`DEBUG` is safe to share.** It is the level users will be asked for in bug reports, so it must not
contain prompt content or tool output. Debugging usually needs the shape of what happened, not the
content, and where content is genuinely needed there is `TRACE`.

**`TRACE` is a deliberate act.** Enabled per session, warned about on screen, never persisted as a
default, and it still applies the secret redaction filter — because a trace log is the most likely
artifact to be pasted into a public issue.

Log files: `0600`, in `~/.kalash/logs/`, rotated by size with a retention cap
([`data-model.md`](./data-model.md) §Retention). Never transmitted anywhere.

---

## 7. Cryptography

Deliberately boring. No custom constructions.

| Use | Choice |
|---|---|
| Content addressing | SHA-256 |
| Audit hash chain | SHA-256 over canonical serialization + previous hash |
| Redaction marker digest | HMAC-SHA256, per-session key |
| At-rest encryption (optional) | SQLCipher, or an OS-level FDE recommendation. Documented, not default |
| Transport | TLS 1.2+, platform trust store |
| Plugin signing (planned) | Sigstore / minisign, per [`operations.md`](./operations.md) |

At-rest encryption is not the default because the key has to live somewhere, and on a single-user
machine that is usually the same keyring an attacker with user-level access already reads (T-I). It
protects against disk theft and backup exposure, which is real but narrower than it first appears.
Full-disk encryption is the better answer for most users and we say so.

---

## 8. Vulnerability disclosure

- `SECURITY.md` at the repository root with a contact address and a response-time commitment.
- Private reporting channel. No public issues for unpatched vulnerabilities.
- Advisories published for anything affecting the invariants in [`invariants.md`](./invariants.md),
  with the affected versions and the invariant IDs involved.
- A regression test accompanies every fix, referencing the invariant it restores.
- Coordinated disclosure timeline stated up front so reporters know what to expect.

---

## 9. Security review triggers

A change requires explicit security review, not just normal code review, when it touches:

- `core/egress.py`, `core/redact.py`, `core/secrets.py`
- `permissions/`, `sandbox/`
- `memory/router.py`, `memory/ledger.py`, or any provider adapter
- `hooks/trust.py`, `plugins/loader.py`
- `tools/shell.py`, `tools/fs.py`, `tools/web.py`
- `mcp/client.py`, `mcp/server.py`
- Anything adding a dependency
- Anything adding or widening a capability
- Anything that could alter the order in the I-004 sequence

The PR must state which invariants are affected and how they remain enforced. "No security impact" is
an acceptable answer when true, but it must be stated rather than assumed by omission.

---

*See also: [`threat-model.md`](./threat-model.md) for what these mechanisms defend against,
[`invariants.md`](./invariants.md) for the properties they enforce,
[`capabilities.md`](./capabilities.md) for the authority model,
[`observability.md`](./observability.md) for audit schema and the hash chain,
[`operations.md`](./operations.md) for signing, SBOM, and release process.*
