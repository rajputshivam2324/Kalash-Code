# Real-World Project Build Evaluation & Harness Comparison

## Overview
We built and wired a comprehensive end-to-end evaluation suite (`evals/project_build.py`) into the Kalash offline eval runner (`evals/run.py`). 

Instead of isolated single-turn tool checks, this evaluation simulates an **entire multi-turn real-world software engineering workflow**:
1. **Planning**: Initializing project goals via the `todo` plan tool (`TaskItem` / `TaskList`).
2. **Skill Discovery**: Discovering and inspecting installed skills (`.kalash/skills/code-refactoring-ast`).
3. **Memory Persistence**: Extracting and persisting project architectural conventions to long-term SQLite memory (`remember`).
4. **Code Generation**: Structuring a complete Python project:
   - `pyproject.toml` (Hatchling backend configuration)
   - `src/taskflow/__init__.py`
   - `src/taskflow/models.py` (`Task`, `TaskStore`, `Priority`, `Status`)
   - `src/taskflow/cli.py` (Typer CLI commands with Rich table formatting)
   - `tests/test_models.py` (Pytest test suite covering store operations & persistence)
   - `README.md` (Project documentation)
5. **Codebase Inspection**: Verifying written files via `read`, searching symbol references via `search`, and discovering directory tree via `glob`.
6. **Execution & Validation**: Running tests and file listings via `shell`.
7. **Memory Recall**: Recalling project conventions from memory (`recall`).
8. **Progress Tracking**: Updating persistent scratchpad notes.
9. **Golden Reference Cross-Comparison**: Generating an independent golden standard build and performing structural sequence matching and file coverage analysis against the harness output.

---

## Benchmark Results: 17/17 Passed (100%)

| Benchmark / Assertion | Expected Criteria | Harness Output Result | Status |
| :--- | :--- | :--- | :--- |
| **`pyproject.toml`** | Structural AST/line match | `match ratio 100%` | ✅ **PASS** |
| **`src/taskflow/__init__.py`** | Package version & init | `match ratio 100%` | ✅ **PASS** |
| **`src/taskflow/models.py`** | Data classes & store logic | `match ratio 100%` | ✅ **PASS** |
| **`src/taskflow/cli.py`** | Typer commands & table rendering | `match ratio 100%` | ✅ **PASS** |
| **`tests/test_models.py`** | Comprehensive model unit tests | `match ratio 100%` | ✅ **PASS** |
| **`README.md`** | Documentation & usage examples | `match ratio 100%` | ✅ **PASS** |
| **Plan Tool Execution** | Agent creates durable plan | `<plan>Build TaskFlow CLI task manager app...` | ✅ **PASS** |
| **Scratchpad Progress** | Notes stored during execution | `2 scratchpad block(s)` | ✅ **PASS** |
| **Skill Discovery** | Discover `.kalash/skills/` | `code-refactoring-ast listed in output` | ✅ **PASS** |
| **Memory Persistence** | Store project conventions | `hatchling / taskflow conventions stored` | ✅ **PASS** |
| **Codebase Search** | Cross-file symbol resolution | `TaskStore found across files` | ✅ **PASS** |
| **Glob Discovery** | Discover project `.py` files | `models.py, cli.py, __init__.py matched` | ✅ **PASS** |
| **Shell Execution** | Run commands in sandbox | `ls output contained project tree` | ✅ **PASS** |
| **Multi-Turn Continuity** | Maintain conversation state | `16 iterations, 15 tool calls` | ✅ **PASS** |
| **Clean Termination** | Agent terminates without crash | `exit reason: no_tool_calls` | ✅ **PASS** |
| **Golden Reference Completeness** | Baseline self-consistency | `6 files present` | ✅ **PASS** |
| **Harness vs Golden Coverage** | File set coverage ratio | `100% (6/6 files matched)` | ✅ **PASS** |

---

## Global Evaluation Suite Summary

```
================================================================================
Kalash evals — deterministic, offline
================================================================================
capability           8/8   ok (100%)
safety              43/43  ok (100%)
policy               7/7   ok (100%)
token efficiency     9/9   ok (100%)
project build       17/17  ok (100%)
================================================================================
TOTAL BENCHMARKS    84/84  ok (100% Passed)
================================================================================
```
