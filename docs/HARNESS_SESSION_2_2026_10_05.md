# Harness reliability session 2 — categories 6–10

This continues the three-session plan after [session 1](HARNESS_SESSION_1_2026_10_05.md).
The existing dirty tree, user commits and historical failed evaluation evidence
were preserved. No live provider calls, allowance resets, commits or pushes occurred.
Categories 11–14 remain the next session; this is not a reliability certification.

## Changes and evidence

| Category | Implemented contract | Evidence and limits |
| --- | --- | --- |
| 6. Cost per verified result | Shorten verbose older observations by retrievable reference before buying a semantic summary. Do not repeatedly summarize user anchors already retained verbatim. Admit summary requests against their complete estimated context and output allowance. | The original scripted context fixture dispatched twice, including a summary; it now dispatches once. The saved diagnostic measures 62,500 estimated input tokens before reduction and 11,661 afterward. Its answer remains `stopped_unverified`; no real-model cost per verified result was measured. |
| 7. Relevant context and compaction | Refresh session notes/plans on each model iteration. Bound note/index prompt projections, and give all optional saved data one aggregate allowance. Report system/tools/history/tail estimates. Preserve user messages and tool/result identities and evidence. Refuse a request that cannot fit its output allowance instead of silently reducing it. | Tests cover notes created within a turn, oversized optional data, preserved constraints, summary admission, output allowance refusal and the existing 300-step paired-exchange run. Instructions that still exceed the window produce an error; this is not a general semantic relevance selector. |
| 7. Context estimation | Include complete serialized blocks, signatures, images and opaque payloads. Estimate non-ASCII text using UTF-8 bytes rather than the ASCII character ratio. | Unicode, image and signed-payload regressions. This remains a heuristic; it can overestimate media and undercount some ASCII/provider framing. No provider tokenizer or exact billing bound is claimed. |
| 8. Memory provenance and authority | Model-callable `remember` writes `AGENT_INFERRED`, low trust and confidence 0.5, with its source call ID. Recall includes source/scope/time/trust; injected memory also includes confidence. Scope parameters are validated; forget forwards its requested scope. | Agent-authored permission claims cannot become high-trust user statements. Extraction excludes common inline quotations, blockquotes, code fences and negated requests; heuristic confidence is reduced to 0.6. Current instructions and runtime permissions remain authoritative. |
| 9. Resource handling | Reads acquire at most 16 MiB, process outside the event loop, avoid a list of every line, and return at most 1 MiB. Search collects bounded stdout/stderr, stops its process group at a bound and drains/reaps on timeout or cancellation. | Minified-file, acquisition-bound, aggregate-search and cancellation regressions. A read still buffers a bounded source snapshot; it is not an arbitrary-size streaming file reader. Search truncation reports that the complete match count is unknown. |
| 9. Web handling | Stream bounded uncompressed HTTPS text with a whole-operation deadline. Validate each redirect before requesting it; refuse changed origins and embedded credentials. Treat HTTP errors as failures. Request identity encoding and reject unsolicited compression before body processing. | Mock HTTP fixtures prove early close at the byte limit, HTTP 503 failure, no contact after HTTPS downgrade or origin change, and no compressed-body decoding. Servers that require compression are unsupported. DNS/private-address isolation is not established. |
| 9. Scratchpad handling | Bound observation bodies (8 MiB), metadata (8 KiB), entries (5,000) and index files (16 MiB). Refuse writes over damaged indexes. Bound blob reads. `expand(grep=...)` now performs case-insensitive literal matching outside the event loop. | Notes remain retrievable by reference despite bounded prompt projections. Literal selection removes Python regex backtracking from this path; use the ripgrep `search` tool for regex. A corrupt index remains on disk instead of being silently replaced. These limits do not bound every artifact parser, plugin or process. |
| 9. Journal handling | Migration 5 tracks journal record/byte usage transactionally. Admission refuses records above 8 MiB and sessions above 128 MiB or 100,000 records. Recovery processes fixed-size keyset pages, excludes old request bodies when folding lifecycle state, and selects the latest compaction before loading its active transcript. | Quota rollback preserves earlier rows/checkpoints. Paged recovery and old-progress-after-compaction tests pass alongside existing crash/effect recovery regressions. Existing oversized journals are preserved; later additions can be refused. These are operational storage bounds, not spending ceilings or automatic evidence deletion. |
| 10. Delegation and workspace isolation | Shared-workspace children use read-only tool authority and skip hooks/extensions. Independent writing requires a caller-owned separate workspace. Return child ID and explicit workspace mode. Persist child sessions and parent links when the parent is durable; reconcile observed and unknown usage on exit. | A child proposing a write cannot create the file. Hooks are not dispatched. A failed child retains partial text in its own transcript and links its unknown usage to the parent. Standalone transient runs remain transient. No automatic worktree creation, merge system or parallel team speedup is claimed. |

Runtime changes stay in context/history/compaction, memory, tool processing,
delegation and journal/storage modules. User-controlled monetary/token limits
remain unchanged. Runtime permission checks, protected paths, stale-write checks
and OS sandbox requirements remain enforced.

The context fixture is saved in
[machine-readable evidence](evals/session_two_2026_10_05.json). It uses scripted
provider events, temporary files and temporary state. Its token counts are prompt
estimates, not billed tokens. All new regressions are in
`tests/unit/test_harness_session_two.py`.

## Transparent test revisions

The first regression group produced **13 failures and one pass**. It exposed the
remaining acquisition/aggregation/cancellation/redirect/HTTP/Unicode/projection
problems and a missing memory provenance parameter. A second five-case group
failed for free context reduction, repeated compaction, context diagnostics,
summary admission and shared-workspace writes before implementation.

The first broad run after implementation produced **796 passes and two failures**:

- A missing-blob fault-injection stub lacked the new bounded-read keyword. Its
  signature was updated; the missing-blob behavior remains an error/`None`.
- The corrupt-index test expected a subsequent write to replace the damaged
  index. It now requires refusal and unchanged corrupt bytes. Read-only diagnostic
  inspection still tolerates the damaged index.

The 300-step test previously required fewer than 50 active messages. Free
observation reduction can retain more small paired exchanges. That assertion now
requires estimated input plus the full output allowance and framing margin to fit
the 32,000-token window. Its existing 300-step, constraint-preservation and exact
tool-pair assertions remain. No live grader, frozen probe or historical result was
changed. The output-allowance regression additionally restricts its tool catalog
so it exercises a fitting input that would previously have received a smaller
output allowance; that strengthened fixture passed separately after the full run.

## Verification

- Full `.venv/bin/python -m pytest -q`: **800 passed in 50.06s**, one warning from
  the existing pinned offline Anthropic fixture. No service availability was tested.
- Focused resource/memory/context checks: **113 passed in 1.33s**; the strengthened
  output-allowance refusal case passed afterward in **0.35s**.
- Final new regression file, including the strengthened fixture: **30 passed in 1.11s**.
- Strict `.venv/bin/mypy src`: **166 source files passed**.
- F/I/UP across `src tests evals`: passed. Ruff formatting: **255 files passed**.
  `git diff --check`: passed. The new regression file and journal module pass all
  configured Ruff rules.
- Full-rule repository Ruff: **780 findings**, including current work. Selected
  rule success is not full lint success; release-engineering cleanup remains.

## Remaining limits and next session

Memory extraction is still a conservative heuristic. Excluding a negated candidate
does not automatically revoke an older preference; explicit forget/update and
existing revision/expiry APIs handle corrections. General contradiction resolution,
cross-project adversarial recall and memory-on/off quality/cost trials remain
unqualified. Existing records are not retroactively relabelled.

Semantic summaries remain model-generated data. Runtime receipts and verbatim user
constraints are protected separately; this is not a proof that every decision or
fact survives every reduction. Complete tokenization, semantic relevance selection,
all parser CPU bounds, hostile installed-code isolation and automatic workspace
integration remain unproved. Optional context can be omitted visibly; oversized
authority-bearing content causes refusal rather than silent deletion.

Start session 3 with **11–14**: sustained terminal/TUI behavior, truthful complete
CLI/SDK/event surfaces, extension/capability validation, and reproducible release
and evaluation discipline including full-rule lint. Live benchmarks stay paused.
Real-model reliability, cost per verified result, a 9.5/10 score and superiority
over another harness still require authorized matched trials.
