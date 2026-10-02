---
description: Exhaustive zero-gap code review — audits every file, generates CODE_REVIEW.md, offers interactive explanation
agent: exhaustive-reviewer
subtask: true
---

Perform an exhaustive code review with zero gaps.

Scope: $ARGUMENTS
If $ARGUMENTS is empty, review the entire repository (all git-tracked files).

Execute the `exhaustive-review` skill workflow strictly:

1. Inventory: use `bash` (git ls-files, git status), `glob`, `grep` to enumerate every file. Create todos.
2. Read every file in scope fully — do not sample. Track coverage table.
3. Audit each file against all 16 dimensions: correctness, security (OWASP), performance, resource leaks, concurrency/async, error handling, type safety/validation, API design, data/persistence, architecture, testing, observability, config/infra, a11y, supply chain, docs.
4. Cross-file analysis: dead code, circular deps, env var consistency, test gaps.
5. Generate report: write to `./CODE_REVIEW.md` (or path provided in $ARGUMENTS via --output). If CODE_REVIEW.md already exists, create `./reviews/REVIEW_YYYY-MM-DD.md`. Follow the exact report structure defined in the agent prompt and skill.
6. After writing, print: "Review complete — report at <path> — Files reviewed N/M — CRITICAL:x HIGH:y MEDIUM:z. Say 'explain' to walk through findings."

If the user later says "explain", explain findings from the report with file:line, root cause, impact, and diff-style fix — do not re-audit from scratch.

Current context:
- Project root files: !`git ls-files | head -n 50`
- Recent changes: !`git diff --stat HEAD 2>/dev/null | head -n 50`
- Untracked: !`git status --porcelain 2>/dev/null | head -n 50`
