---
name: exhaustive-review
description: Exhaustive zero-gap code review workflow — inventory every file, audit 16 dimensions, generate markdown report, and explain findings interactively on demand.
license: MIT
compatibility: opencode
metadata:
  audience: developers
  workflow: code-review
---

## What I do
- Inventory **every** file (git ls-files + glob) and guarantee 100% coverage — no sampling.
- Audit against 16 mandatory dimensions: correctness, security (OWASP), performance, leaks, concurrency, error handling, type safety, API design, data/persistence, architecture, testing, observability, config/infra, a11y, supply chain, docs.
- Generate a structured markdown report (`CODE_REVIEW.md` or `reviews/REVIEW_YYYY-MM-DD.md`) with severity, file:line, evidence, impact, and fix.
- Explain findings interactively only when asked — plain language + code diffs.
- Track progress with todowrite and never silently skip files.

## When to use me
Use when the user says: `review`, `audit`, `code review`, `check this code`, `generate review md`, or invokes `@exhaustive-reviewer`.

## Workflow

### 1. Inventory (do not skip)
```bash
git ls-files
git status --porcelain
glob **/*.{ts,tsx,js,jsx,py,go,rs,java,json,yaml,yml,toml,Dockerfile,sql,sh}
grep -r "TODO|FIXME|eval\(|innerHTML" .
todowrite: create todos for inventory → file review → cross-file → report → explain
```

### 2. File-by-file review
For each file in inventory:
- `read` full content
- Apply all 16 dimensions (see agent prompt for table)
- Log finding as: `[SEV] file:line — Category — Evidence — Impact — Fix`

### 3. Cross-file checks
- Dead code / unused exports (`grep` symbol usages)
- Circular imports
- Env var defined vs used
- Test coverage gaps

### 4. Generate report
Default: `./CODE_REVIEW.md`. If exists or user requests dated: `./reviews/REVIEW_YYYY-MM-DD.md`.
Structure:
```
# Code Review — <repo> — date
## Executive Summary (verdict, counts, top 3 risks)
## Coverage Inventory (file | lines | status REVIEWED/UNREVIEWED)
## Findings by Severity (CRITICAL/HIGH/MEDIUM/LOW/INFO, each with file:line, evidence, fix)
## Findings by Category (16-dim matrix)
## Positive Observations
## Metrics
## Action Plan (prioritized)
## Unreviewed / Limitations
## Appendix: File Summaries
```

### 5. Explain on demand
After writing report, say:
> Report written to `CODE_REVIEW.md`. Want me to walk through the CRITICAL/HIGH findings?

If user asks `explain`, provide:
- Plain language + ELI5 analogy
- Root cause and why it matters
- Before/after diff
- Reference to report ID and file:line

## Rules
- Never sample — every file must be REVIEWED or explicitly UNREVIEWED with reason.
- Every finding must cite `file:line` and include a concrete fix.
- Ask for scope only if ambiguous; otherwise default to full repo.
- Continue incrementally for large repos — update todowrite every 20 files.

## Example invocations
- `@exhaustive-reviewer review the whole repo`
- `skill({ name: "exhaustive-review" })` then follow workflow
- `/review` or `/review src/auth --output reviews/auth.md`
