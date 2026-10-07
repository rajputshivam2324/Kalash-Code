# Repository Guidelines

## Session Continuity

Before continuing harness work, read [NEXT_SESSION.md](NEXT_SESSION.md) for the
current baseline, measured failures, completed fixes and next reliability priorities.
Check the current working tree before relying on its recorded baseline.

## Harness Reliability Priority

- The user's current objective is a reliable harness. Real-model reliability
  remains unproved; passing unit tests and packaged skills do not establish it.
- Read the handoff and measured eval reports before changing the runtime.
  Preserve existing work, failed runs and the user's commits. Diagnose one
  demonstrated failure at a time and add a regression before claiming it fixed.
- Prioritize provider/tool-call completion, actionable errors, accurate budget
  accounting, safe read-to-write workflows, verified completion and recovery.
  Keep improvements in the relevant runtime/provider/tool modules.
- Distinguish model failure, provider failure, controller refusal, grader defect
  and infrastructure failure. A budget-stopped partial artifact is not a complete
  build; a scripted reference pass is not a real-model success.
- Preserve permissions, OS sandboxing, stale-write checks and secret handling.
  Never bypass them, ignore usage, reset a shared ledger or weaken a grader to
  manufacture a passing score. Fix flawed checks transparently and preserve revisions.
- Work is deferred to the next session. Start with local reproduction and fixes;
  live benchmarks are paused until the user requests another live evaluation.
  Then establish its budget and scope from the user's instructions; do not infer
  fresh spending authority merely from an old successful provider probe.
- Judge produced artifacts using independent checks and evidence. Report actual
  outcomes, limitations and failures; make comparative claims only after matched trials.
- Update NEXT_SESSION.md with changes, verification and remaining work before
  handing off. Do not commit or push unless the user requests those Git actions.

## Project Structure & Module Organization

Application code lives in `src/kalash/`. The CLI and terminal UI are in `cli/` and `tui/`; agent execution, models, tools, permissions, memory, and storage have their own packages. Put new behavior in the relevant package rather than growing CLI entry points. Tests live in `tests/unit/`, `tests/integration/`, and `tests/e2e/`; shared fixtures belong in `tests/conftest.py`. Evaluation scripts and fixtures are under `evals/`. Project agent definitions and skills live in `.kalash/agents/` and `.kalash/skills/`.

## Build, Test, and Development Commands

- `uv sync --all-extras`: install the project and development dependencies (Python 3.12+ required).
- `uv run kalash`: start the interactive terminal UI; `uv run kalash doctor` checks local setup.
- `uv run pytest -q`: run the test suite; add a path such as `tests/unit/test_memory.py` for focused work.
- `uv run ruff check src tests`: check lint rules and import order.
- `uv run mypy src`: check types with the repository's strict mypy configuration.
- `uv build`: build distributable packages from `pyproject.toml`.

## Coding Style & Naming Conventions

Use four-space indentation, type annotations, and Python 3.12 syntax. Ruff is configured for a 100-character line length and treats `kalash` as first-party for import sorting. Use `snake_case` for modules, functions, and variables, `PascalCase` for classes, and descriptive names for CLI commands and tools. Keep async boundaries explicit and follow patterns in neighboring modules.

## Testing Guidelines

Use pytest and `pytest-asyncio`; async tests run in automatic asyncio mode. Name test files `test_*.py` and test functions `test_*`. Add unit tests for local behavior and integration or e2e tests when changes cross runtime, provider, CLI, or UI boundaries. Run the focused test first, then the full suite before opening a PR. No numeric coverage threshold is configured.

## Commit & Pull Request Guidelines

Recent commits use short, action-oriented summaries, often with prefixes such as `feat:`. Follow that pattern (`fix: handle expired session`) and keep unrelated changes separate. PRs should explain the behavior change, note test commands and results, and link a relevant issue. Include terminal output or screenshots for visible TUI changes.

## Security & Configuration

Keep API keys in environment variables or local user configuration; never commit credentials, session data, or generated caches. Review permission and sandbox behavior when adding tools or external integrations.
