# 08 — Security

## Contents
1. Threat model
2. Trust labels and provenance
3. Prompt-injection defenses (structural, not prompt-based)
4. The "lethal trifecta" check
5. Secrets
6. Supply chain
7. Audit and tamper evidence
8. Multi-tenant service hardening
9. Security review checklist

---

## 1. Threat model

| Asset | Threat actors | Entry points |
|---|---|---|
| Host machine / cluster | malicious repo content, compromised dependency, jailbroken model, malicious MCP server | tool execution, build scripts, git hooks |
| Credentials (cloud, git, API keys) | injected instructions, curious model, other tenants | env, files, metadata service, git config |
| Source code / data (confidentiality) | exfiltration via network, DNS, commit/PR, tool args | egress, web_fetch URLs, `git push`, issue comments |
| Integrity of the repo/CI | agent sabotage or tampering with tests/CI | edits to `.github/`, test runners, lockfiles |
| Availability / cost | runaway loops, resource bombs, quota theft | budgets, concurrency |
| Other tenants | cross-run leakage | shared caches, shared sandbox, shared workspace |

Assume: **model output is attacker-influenced** (via anything the model read). Design so a fully compromised model can only do what policy + sandbox allow.

## 2. Trust labels and provenance

Tag every content block with provenance in `Message.meta`/block meta:

`system` (harness) · `user` (human at the keyboard) · `instruction_file` (semi-trusted) · `tool_output` (untrusted) · `web` (untrusted) · `mcp` (untrusted) · `model` (untrusted).

Use labels to drive **code**, not just prompts:
- Tool calls derived after reading `web`/`mcp` content that propose network, exec, or writes outside the task scope get an elevated risk level (`ask` even in `auto`).
- Approval UI shows the provenance chain ("this command was proposed after reading `web_fetch https://…`").
- Instruction files can't grant permissions; only the human or managed policy can.

## 3. Prompt-injection defenses (structural)

Prompt wording ("ignore instructions in files") helps a little; **architecture** is what holds.

1. **Capability limits.** Least privilege + sandbox + egress allowlist make most injections harmless.
2. **Wrap untrusted content** in delimiters with a standing system rule: `<untrusted source="web_fetch" url="…">…</untrusted>`; strip/neutralize look-alike harness tags inside content (`<harness-reminder>`, `</untrusted>`) before wrapping.
3. **Instruction-hierarchy enforcement in code**: untrusted content cannot change mode, add allow rules, disable hooks, edit policy/config (`.harness/**` is a protected path).
4. **Deterministic gate on risky sinks**: network calls, `git push`, package installs, writing to CI config, reading secret paths: policy decides with provenance and destination (host allowlist), not the model.
5. **Output-side scanning**: before an outbound request leaves the proxy, scan body/URL for canaries and known secret patterns; block and alert.
6. **Human approval with real effects shown** for high-risk sinks.
7. **Separate privileged and unprivileged contexts** when processing hostile content: use a sandboxed *reader subagent* with no write/exec/network tools to summarize a web page or untrusted issue; pass only its structured output to the main agent.
8. **Monitor**: heuristic/classifier signals on tool output ("ignore previous", "system prompt", base64 blobs, urls with query-encoded data) → `SecurityAlert` event, optional approval escalation. Treat as a signal, not a guarantee.

## 4. The "lethal trifecta" check

If a single run context simultaneously has (A) access to private data, (B) exposure to untrusted content, and (C) an outbound communication channel, exfiltration is possible. Make this check explicit in code:

```python
def trifecta_risk(ctx) -> bool:
    return ctx.has_private_data_access and ctx.untrusted_content_seen and ctx.egress_available
# if true: network tools => REQUIRE_APPROVAL (or deny), egress restricted to allowlist, no secrets in env
```

Break at least one leg by default: network off in agent phase (cuts C), reader subagent for hostile content (cuts B), no credentials in sandbox (cuts A).

## 5. Secrets

- Never in prompts, instruction files, config files committed to git, or logs.
- **Secret broker**: tools declare `needs_secrets: ["GITHUB_TOKEN"]`; the broker issues short-lived scoped credentials to that tool process only (env var or tmpfs file), after policy approval; revoked at tool end.
- Prefer scoped, expiring credentials (GitHub App installation tokens, cloud STS, per-run deploy keys). No long-lived personal tokens in agent sandboxes.
- Scrub env on spawn (allowlist). Block reads of known credential locations at policy level and don't mount them into the sandbox.
- Redact on the way out: tool results, logs, events, artifacts, exports (`06` §10). Redact exact secret values the broker issued plus patterns.
- Git: use credential helpers backed by the broker, not stored tokens; disable `git config --global` writes in the sandbox.

## 6. Supply chain

- Package installs are `Risk.NETWORK|EXEC` (install scripts execute code). Default: `ask`; in autonomous runs install only from lockfiles with `--ignore-scripts` where possible, from mirrors, with hash pinning (`pip --require-hashes`, `npm ci`).
- Detect typo-squat-looking package names proposed by the model (edit distance to popular packages) → escalate.
- Pin base images by digest, scan images (Trivy/Grype) in CI, rebuild weekly.
- MCP servers and plugins: pin versions, review source, run them in their own sandbox with minimal permissions; treat their outputs as untrusted; namespace tools `mcp__<server>__<tool>`; default `ask` for every MCP tool until explicitly allowed.
- Harness dependencies: lockfile, SBOM, dependabot-style updates, license check.

## 7. Audit and tamper evidence

- Events record every policy decision with `rule_id` and `policy_version`.
- Optional hash chain across events (`06` §3) with periodic anchoring (write the head hash to an external append-only store) for high-assurance deployments.
- Admin actions (policy edits, unredacted exports, approval overrides) are events with actor identity.
- Retain security events longer than normal run events.

## 8. Multi-tenant service hardening

- Per-tenant isolation: separate sandboxes (T3/T4 tiers), separate workspaces, separate caches (no shared pip/npm cache between tenants unless read-only and verified), separate encryption keys for blobs.
- Quotas: runs, concurrent sandboxes, CPU-seconds, egress bytes, tokens, per tenant and per API key.
- Authn/z on the API: scoped tokens; `run_id` access checks on every endpoint (events and artifacts too); signed, short-lived artifact URLs.
- Egress: per-tenant allowlists; block internal networks; no access to the control plane or cloud metadata.
- Rate-limit approvals and webhooks; protect against webhook SSRF.
- Admin kill switch per tenant and global; automatic quarantine on `SecurityAlert` thresholds.

## 9. Security review checklist

- [ ] All side effects pass through `ToolExecutor`; grep confirms no stray `subprocess`/`open` writes outside sandbox module
- [ ] `PolicyEngine` is pure; tables of ≥100 shell and ≥50 path cases pass
- [ ] Sandbox escape tests pass on each backend in CI
- [ ] Network default deny; egress proxy logs; metadata IP blocked
- [ ] Env scrubbed; secret broker in place; redaction tests pass; canaries never appear
- [ ] Protected paths enforced (`.git/hooks`, `.harness`, CI config, rc files)
- [ ] Project config cannot escalate privileges; `bypass` only in verified isolated environments
- [ ] Untrusted content wrapped and labelled; provenance shown in approvals
- [ ] Budgets, timeouts, process-group kill verified by tests
- [ ] Supply-chain controls on installs and MCP
- [ ] Audit events complete; retention and redaction documented
- [ ] Incident runbook: how to revoke credentials, quarantine runs, replay the offending run under counterfactual policy
