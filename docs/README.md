# Kalash Code — Specification Tree

> **Status:** normative.

Reading order for someone new to the project, then an alphabetical reference.

---

## Recommended Reading Order

1. **[`../CLAUDE.md`](../CLAUDE.md)** — contributor blueprint: what we build, tech stack, repo layout, build order
2. **[`../AGENT.md`](../AGENT.md)** — runtime agent contract: identity, turn loop, memory discipline, safety boundaries
3. **[`invariants.md`](./invariants.md)** — 40 hard requirements that must hold at all times (I-001..I-040)
4. **[`capabilities.md`](./capabilities.md)** — the unified authority vocabulary
5. **[`threat-model.md`](./threat-model.md)** — who attacks, what they want, how they fail
6. **[`security.md`](./security.md)** — egress gateway, network policy, secrets, redaction, supply chain
7. **[`model-gateway.md`](./model-gateway.md)** — provider normalization, streaming, errors, fallback, cost
8. **[`model-providers.md`](./model-providers.md)** — provider catalog, routing strategies, connection flow
9. **[`tools.md`](./tools.md)** — tool lifecycle, filesystem, shell, git, web
10. **[`memory.md`](./memory.md)** — KME native engine, federation, recall/write pipelines, deletion, poisoning defence
11. **[`permissions.md`](./permissions.md)** — decision algorithm, confirmation class, grants, approval UX, sandbox
12. **[`state-machines.md`](./state-machines.md)** — 11 formal machines, crash recovery, concurrent sessions
13. **[`context-budget.md`](./context-budget.md)** — assembly order, allocation, compaction, cost ceilings
14. **[`data-model.md`](./data-model.md)** — SQLite schema, checkpoints, backup, export, GC
15. **[`observability.md`](./observability.md)** — audit schema, traces, error taxonomy, logging, metrics
16. **[`interfaces.md`](./interfaces.md)** — TUI, CLI output, SDK, HTTP server, accessibility
17. **[`scheduler.md`](./scheduler.md)** — cron, events, webhooks, safety defaults, notifications
18. **[`platform.md`](./platform.md)** — cross-platform matrix, install lifecycle, config validation, time, i18n
19. **[`evaluation.md`](./evaluation.md)** — test taxonomy, security corpora, evals, CI pipeline
20. **[`operations.md`](./operations.md)** — SLOs, release engineering, versioning, compatibility, supply chain

---

## Cross-Cutting Index

| Concern | Primary doc | Supporting |
|---------|-------------|------------|
| Security | threat-model, security | invariants, permissions, tools §6-8 |
| Memory | memory | invariants I-001/002/005/016-020, security §4, context-budget |
| Permissions | permissions, capabilities | state-machines §4-5, tools §2 |
| Model routing | model-providers, model-gateway | context-budget, operations |
| Persistence | data-model | state-machines §12-13, memory §13.1 |
| Scheduling | scheduler | state-machines §8, permissions §3 |
| Observability | observability | security §6, data-model |
| Testing | evaluation | all (each doc has a testing section) |

---

## Invariant Quick Reference

| Range | Domain |
|-------|--------|
| I-001 – I-010 | Memory integrity, privacy, egress, deletion |
| I-011 – I-015 | Sandbox, resource limits, process isolation |
| I-016 – I-020 | Memory federation, provenance, consistency |
| I-021 – I-025 | Storage: append-only, migrations, single-writer, blobs |
| I-026 – I-030 | Permissions, capability attenuation, grant scoping |
| I-031 – I-035 | Agent safety: loop limits, confirmation class, secrets out of DB |
| I-036 – I-040 | Operational: versioning, compatibility, UTC, audit chain |

---

## Threat Model Quick Reference

| ID | Threat |
|----|--------|
| T-1 | Prompt injection via tool output |
| T-2 | Exfiltration via model-controlled egress |
| T-3 | Privilege escalation through grant manipulation |
| T-4 | Memory poisoning via untrusted MCP source |
| T-5 | Supply-chain compromise of plugin/MCP server |
| T-6 | Unattended scheduled write with stale authority |
| T-7 | Sandbox escape via kernel/runtime vulnerability |
| T-8 | Credential theft from process memory or logs |
| T-9 | Denial of service via resource exhaustion |
| T-10 | Audit log tampering to hide malicious actions |

---

## Document Status Legend

| Status | Meaning |
|--------|---------|
| normative | Binding specification; implementation must conform |
| informative | Guidance and rationale; non-binding |
| draft | Under active development; may change without notice |
| deprecated | Superseded; retained for historical reference |

---

*See also:* [`../CLAUDE.md`](../CLAUDE.md) (contributor entry point), [`invariants.md`](./invariants.md) (full invariant definitions), [`threat-model.md`](./threat-model.md) (full threat analysis).
