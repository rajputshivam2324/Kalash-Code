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

Ceilings bound tokens, cost (where prices are supplied), model requests, attempted tools and elapsed time. They also cover summary calls and child usage.

**Safety**

- Shell commands require a working OS sandbox unless full access is explicitly configured.
- Subprocess environments exclude API keys by default.

**Durability**

- Each settled tool result is saved before the next dependent write starts.
- SQLite operations own their transaction connections.
- Interrupted tool calls retain their intent and receive an unknown-outcome result on resume. **Inspect the workspace before retrying a mutation.**

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
- Near the actual model window, the runtime summarizes complete older exchanges using a charged model request, with a labeled extractive fallback.
- User instructions remain verbatim; recent tool exchanges stay paired.
- Oversized observations can be retrieved through `expand`.
- Runtime command/file receipts survive summaries as bounded structured facts.
- Ordinary observations are scrubbed before retention and before outgoing requests.

See [the design and acceptance gates](docs/HARNESS_DESIGN.md) for remaining security, context and evaluation work.

## Memory

The default backend is SQLite with FTS keyword retrieval.

- Records carry scope, provenance and confidence; scope filtering happens before retrieval limits.
- Exact deduplication, updates, history and FTS changes are transactional.
- Turn capture saves bounded explicit preferences.
- Recall has a rendered token limit and is labeled as data.
- Existing database records are kept.
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

The performance reference is the [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness). Public comparison requires matched trials and an independent official grader.

## Documentation

| Document | Covers |
| --- | --- |
| [Architecture](docs/ARCHITECTURE.md) | Module responsibilities |
| [Capabilities](docs/CAPABILITIES.md) | Execution paths, installation commands, format limits |
| [Harness design](docs/HARNESS_DESIGN.md) | Design and acceptance gates |
| [Harness review](docs/HARNESS_REVIEW.md) | Findings, fixes and remaining gates |
