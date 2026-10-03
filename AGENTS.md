# Repository Guidelines

## Session Continuity

Before continuing harness work, read [NEXT_SESSION.md](NEXT_SESSION.md) for the
completed refactor, verified results, remaining priorities and benchmark pause.
Check the current working tree before relying on its recorded baseline.

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
