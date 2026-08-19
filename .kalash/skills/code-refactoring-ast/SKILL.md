---
name: code-refactoring-ast
description: AST-aware, safe multi-file refactoring instructions using atomic diff calculation and verification.
version: 1.0.0
allowed-tools: [read, edit, multi_edit, write, shell]
---

# AST-Aware Code Refactoring Skill

Follow this standardized workflow for modifying code across the Kalash codebase.

## 1. Pre-Edit Analysis
1. Read the target file and understand enclosing classes, functions, and import dependencies.
2. Check existing test coverage for the target module in `tests/`.

## 2. Edit Application
1. Prefer surgical `edit` (exact string replacement) or `multi_edit` over full-file overwrites.
2. For new files, use atomic `write` with `create_dirs=True`.
3. Preserve all existing docstrings, type annotations, and module comments.

## 3. Validation & Linting
Immediately after editing:
```bash
uv run ruff check src tests
uv run mypy
uv run pytest <relevant_test_path>
```
