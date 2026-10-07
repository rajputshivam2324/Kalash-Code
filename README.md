<div align="center">

# Kalash Code

**A Python coding-agent runtime with a terminal UI, headless CLI and SDK.**

The runtime owns permissions, tools, session history, context and run limits, so the model never has to.

![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
![Package manager: uv](https://img.shields.io/badge/package%20manager-uv-purple)
![Linting: ruff](https://img.shields.io/badge/lint-ruff-orange)
![Types: mypy](https://img.shields.io/badge/types-mypy-informational)

[Quick Start](#quick-start) · [Providers](#providers) · [Runtime](#runtime-behavior) · [MCP & Hooks](#mcp-and-hooks) · [Development](#development)

</div>

---

## Overview

Kalash Code is a coding-agent harness: the runtime that surrounds a model and turns it into a dependable agent. It runs one loop (assemble context, request the model, execute tools, append results) and enforces permissions, sandboxing, budgets and persistence around it.

| Interface | Description |
| --- | --- |
| **Terminal UI** | Interactive session with a model picker (`kalash`) |
| **Headless CLI** | Scriptable single-shot runs (`kalash -p "..."`) |
| **SDK** | Embed the runtime in your own Python code |

## Quick Start

```bash
uv sync --all-extras
uv run kalash doctor
uv run kalash
uv run kalash -p "Inspect this repository and fix the failing test"
```

Configure a provider with `kalash provider add` or through its environment variable.

| Location | Contents |
| --- | --- |
| `~/.kalash/` | Provider credentials and user settings |
| `.kalash/` | Project guidance and definitions |

## Runtime Behavior

**Loop and tools**

- One loop: assemble context, request the model, execute tools, append results.
- The full permitted tool set stays available. TPM policies and tool profiles do not rewrite prompts or shorten replies.
- Independent reads can overlap; writes and execution form ordering barriers.
- Plan mode and child capability/tool restrictions are enforced at dispatch time.

**Run limits**

Token and cost ceilings are **opt-in**. Usage is still recorded, including summary
calls and child agents. Kalash keeps the selected model and output allowance; an
explicit spending cap that cannot fit the next request produces an actionable
stop instead of silently shrinking the response. Cost estimates require provider
prices and are not a billing guarantee.

Set your own session ceilings in user settings (omit these keys or use `null` for
no spending cap):

```bash
kalash config set budget.max_tokens 500000 --scope global
kalash config set budget.max_cost_usd 2.00 --scope global
# Remove a previously configured ceiling:
kalash config set budget.max_tokens null --scope global
kalash config set budget.max_cost_usd null --scope global
```

Project overrides still take precedence over user defaults; existing configured
limits are preserved. Restart the session after changing settings. Operational
limits remain: 50 iterations per send, 100 model requests, 500 attempted tools,
3600 seconds of session execution, and spawn depth 3. Explicit evaluation budgets
remain mandatory for the evaluation runners.

Efficiency comes from bounded tool observations, lazy extension schemas, context
compaction when needed, and a stable prompt prefix. Changing workspace facts now
follow conversation history so they do not invalidate cached instructions. Savings
depend on provider caching and task behavior; cheapest successful completion has
not been established by the current live evaluations.

**Streaming transcript**

Responses appear in order with their tool calls. Incoming text is coalesced into
50 ms updates, including the last chunk before a pause. Text stays literal while
arriving and becomes Markdown when the response ends, preventing unfinished
formatting from repeatedly changing the transcript height. Tool progress keeps its
output visible, and errors retain partial responses. Scroll up to read history;
press End to follow live output again. Ctrl+O expands the most recent tool details.

**Safety**

- Shell commands require a working OS sandbox unless full access is explicitly configured.
- Subprocess environments exclude API keys by default.

**Durability**

- Each settled tool result is saved before the next dependent write starts.
- SQLite operations own their transaction connections.
- Interrupted tool calls retain their intent and receive an unknown-outcome result on resume. **Inspect the workspace before retrying a mutation.**
- Journal admission stops at 8 MiB per record, 128 MiB or 100,000 records per session. Existing evidence is preserved; start a new session when a limit is reached.

## Instructions, Skills and Context

### Instructions

The static system prompt is separate from instruction discovery. Both `AGENTS.md` and `KALASH.md` load from user and ancestor directories, broadest first. Within the same directory, native `KALASH.md` comes last. Scoped guidance is loaded before the first change under a subdirectory.

### Skills

Skills are folders containing a `SKILL.md` with YAML `name` and `description`.

- Only metadata loads initially; the `skill` tool loads a body and optional bounded references.
- Precedence: project definitions, then user definitions, then plugins, then the 12 bundled workflow skills.
- Bodies and individual resources load on demand.
- Loaded skill bodies and scoped guidance survive history compaction.

### Extension tools

Extension tools are discovered with `tool_search`; only selected MCP/plugin schemas enter requests. Optional artifact workflows cover PDF, DOCX, XLSX, PPTX, images, archives and structured data. See [capability support and loading](docs/CAPABILITIES.md) for execution paths, installation commands and format limits.

### Context assembly

- Identity, catalog and root guidance stay stable.
- Environment and bounded memory are labeled separately from conversation.
- Near the actual model window, the runtime first shortens older observations by reference. If necessary, it summarizes complete older exchanges using a charged model request, with a labeled extractive fallback.
- User instructions remain verbatim; recent tool exchanges stay paired.
- Oversized observations can be retrieved through `expand`.
- Runtime command/file receipts survive summaries as bounded structured facts.
- Ordinary observations are scrubbed before retention and before outgoing requests.
- Notes and observation indexes have bounded prompt projections; `expand` retrieves omitted originals. Its `grep` parameter now matches literal text, case-insensitively. Use `search` for regular expressions.
- A request that cannot fit its output allowance after reduction is refused with a context breakdown. It does not silently lower the chosen allowance.

See [the design and acceptance gates](docs/HARNESS_DESIGN.md) for remaining security, context and evaluation work.

## Memory

The default backend is SQLite with FTS keyword retrieval.

- Records carry scope, provenance and confidence; scope filtering happens before retrieval limits.
- Exact deduplication, updates, history and FTS changes are transactional.
- Turn capture saves bounded explicit preferences.
- Agent-written memories carry low-trust inferred provenance. Recall includes source, scope and time; saved data cannot grant permissions. Automatic preference extraction excludes common quotation, code-fence and negation cases and is still a heuristic.
- Recall has a rendered token limit and is labeled as data.
- Existing database records are kept.

The `task` tool delegates read-only analysis with separate conversation context.
Children share the workspace and skip hooks and extensions; they cannot apply edits.
Writing agents need a caller-owned separate workspace and a reviewed integration step.
Durable parent sessions retain child IDs, partial outcomes and charged usage.

Filesystem reads acquire at most 16 MiB and return at most 1 MiB. Search bounds
aggregate output and reaps its process group on cancellation. Fetch accepts bounded,
uncompressed HTTPS text and same-origin redirects; a destination change requires an
explicit fetch. These limits do not establish DNS/private-address isolation.
- Custom providers can implement the memory protocol and register an entry point.

> **Not included:** vector search, graph retrieval, a mem0 adapter and a background extraction queue.

## MCP and Hooks

### MCP configuration

Settings merge from `~/.kalash/settings/mcp.json` and `.kalash/settings/mcp.json`, with project entries taking precedence.

```json
{
  "servers": {
    "local": {
      "transport": "stdio",
      "command": "python",
      "args": ["server.py"],
      "env": {"TOKEN": "${env:MY_SERVER_TOKEN}"}
    },
    "remote": {
      "transport": "streamable-http",
      "url": "https://example.com/mcp",
      "headers": {"Authorization": "Bearer ${env:MY_SERVER_TOKEN}"}
    }
  }
}
```

| Command | Purpose |
| --- | --- |
| `kalash mcp trust` | Authorize manually reviewed project configuration |
| `kalash mcp add` | Add a server and authorize the configuration it writes |
| `kalash mcp test NAME` | Explicitly check a selected server |

Changing the file invalidates its trust. HTTP connections initialize the protocol and retain session headers. Legacy SSE is also supported but lacks the same transport regression coverage.

### Hooks

Review project hooks, then authorize their current contents with `kalash hooks trust`. Changed files stop running until authorized again.

### Security notes

- Network permissions come from user settings; project configuration cannot widen security settings.
- OAuth login is not implemented; token and header configuration is.

## Development

See [architecture](docs/ARCHITECTURE.md) for module responsibilities and [review](docs/HARNESS_REVIEW.md) for findings, fixes and remaining gates.

```bash
uv run pytest -q
uv run mypy src
uv run ruff check src tests
uv build
```

## Evaluation

Regression tests validate runtime contracts. Scripted evaluations validate runner mechanics. **Neither establishes a public coding benchmark score.**

The [fresh compact suite](docs/EVALS.md) uses independent hidden checks and one shared token allowance. The [2026-10-03 live results](docs/COMPACT_EVAL_RESULTS.md) record failed repairs and the fixes they exposed, within 205,069 conservatively charged tokens.

The performance reference is the [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness). Public comparison requires matched trials and an independent official grader.

## Documentation

| Document | Covers |
| --- | --- |
| [Architecture](docs/ARCHITECTURE.md) | Module responsibilities |
| [Capabilities](docs/CAPABILITIES.md) | Execution paths, installation commands, format limits |
| [Harness design](docs/HARNESS_DESIGN.md) | Design and acceptance gates |
| [Harness review](docs/HARNESS_REVIEW.md) | Findings, fixes and remaining gates |
| [Evaluations](docs/EVALS.md) | Fresh tasks, grading and shared token accounting |
| [Live results](docs/COMPACT_EVAL_RESULTS.md) | Measured outcomes, costs and failure analysis |
| [Codex build judge](docs/BUILD_JUDGE.md) | Build task, independent checks and actual Codex scorecards |
