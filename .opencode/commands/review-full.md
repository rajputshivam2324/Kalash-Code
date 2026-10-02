---
description: Full exhaustive review with 100% coverage guarantee — prints inventory first, then deep audits every file
agent: exhaustive-reviewer
subtask: true
---

You are in exhaustive mode. Review $ARGUMENTS (or entire repo if empty) with 100% coverage guarantee.

Workflow:
- Step 0: Print inventory table (path, language, lines, status) before any findings — so user sees you missed nothing.
- Step 1-4: Same as /review (16 dimensions, file:line findings, cross-file checks).
- Step 5: Write dated report to `./reviews/REVIEW_YYYY-MM-DD.md` and also update `./CODE_REVIEW.md` as latest symlink/copy.
- Step 6: Summarize counts by severity and by category matrix. Offer interactive explanation.

Context injection:
- All tracked files count: !`git ls-files | wc -l`
- File list sample: !`git ls-files | head -n 100`
- Git log recent: !`git log --oneline -10 2>/dev/null`
