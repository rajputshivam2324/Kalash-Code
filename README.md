# Kalash Code

A Python 3.12 coding-agent runtime with a terminal UI, headless CLI and SDK.
The runtime owns permissions, tools, session history, context and run limits.

## Start

```bash
uv sync --all-extras
uv run kalash doctor
uv run kalash
uv run kalash -p "Inspect this repository and fix the failing test"
```

Configure a provider with `kalash provider add` or its environment variable.
Provider credentials and user settings live under `~/.kalash/`; project guidance
and definitions live under `.kalash/`.

## Sarvam

Set `SARVAM_API_KEY` locally, then select `sarvam/glm5.3` with the model picker or:

```bash
uv run kalash -p "Review this repository" --model sarvam/glm5.3
```

Sarvam 105B uses the V1 chat route; GLM-5.3 and the other supported open models
use V2. GLM-5.3 requires access on the API key. The adapter preserves reasoning
and usage, uses the subscription-key header, and defaults to `max` reasoning
with a 32,768-token output allowance. This allowance is configurable and includes
reasoning. There is no automatic reasoning downgrade for provider quotas.
See [Sarvam's model contract](https://docs.sarvam.ai/api/getting-started/models/openweight/glm-5-3).

## Runtime behavior

- One loop: assemble context, request the model, execute tools, append results.
- The full permitted tool set stays available; TPM policies and tool profiles
  do not rewrite prompts or shorten replies.
- Run ceilings bound tokens, cost where prices are supplied, model requests,
  attempted tools and elapsed time. They also cover summary calls and child usage.
- Independent reads can overlap; writes and execution form ordering barriers.
- Plan mode and child capability/tool restrictions are enforced during dispatch.
- Shell commands require a working OS sandbox unless full access is explicitly
  configured. Subprocess environments exclude API keys by default.
- Interrupted tool calls retain their intent and receive an unknown-outcome
  result on resume. Inspect the workspace before retrying a mutation.

## Instructions, skills and context

The static system prompt is separate from instruction discovery. Both
`AGENTS.md` and `KALASH.md` load from user and ancestor directories, broadest
first. At the same directory, native `KALASH.md` comes last. Scoped guidance is
loaded before the first change under a subdirectory.

Skills are folders containing `SKILL.md` with YAML `name` and `description`.
Only metadata loads initially; the `skill` tool loads a body and optional bounded
references. Project definitions override user definitions, which override plugins.
Loaded skill bodies and scoped guidance survive history compaction.

Context assembly keeps identity, catalog and root guidance stable. Environment
and bounded memory are labeled separately from conversation. Near the actual
model window, the runtime summarizes complete older exchanges with a charged
model request and a labeled extractive fallback. User instructions remain
verbatim; recent tool exchanges stay paired. Oversized observations can be
retrieved through `expand`.

## Memory

The default backend is SQLite with FTS keyword retrieval. Records have scope,
provenance and confidence; scope filtering happens before retrieval limits.
Exact deduplication, updates, history and FTS changes are transactional.

Turn capture saves bounded explicit preferences. Recall has a rendered token
limit and is labeled as data. There is no built-in vector search, graph retrieval,
mem0 adapter or background extraction queue. Existing database records are kept.
Custom providers can implement the memory protocol and register an entry point.

## MCP and hooks

MCP settings merge from `~/.kalash/settings/mcp.json` and
`.kalash/settings/mcp.json`, with project entries taking precedence:

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

After reviewing manual project configuration, run `kalash mcp trust`.
`kalash mcp add` authorizes the configuration it writes. Changing the file
invalidates its trust. `kalash mcp test NAME` checks a selected server explicitly.
HTTP connections initialize the protocol and retain session headers. Legacy SSE
is also supported, but lacks the same transport regression coverage.

Review project hooks and authorize their current contents with
`kalash hooks trust`. Changed files stop running until authorized again.
Network permissions come from user settings; project configuration cannot widen
security settings. OAuth login is not implemented; token/header configuration is.

## Repository map and checks

See [architecture](docs/ARCHITECTURE.md) for module responsibilities and
[review](docs/HARNESS_REVIEW.md) for findings, fixes and remaining gates.

```bash
uv run pytest -q
uv run mypy src
uv run ruff check src tests
uv build
```

Regression tests validate runtime contracts. Scripted evaluations validate runner
mechanics. Neither establishes a public coding benchmark score. The performance
reference is the [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness).
Public comparison requires matched trials and an independent official grader.
