<p align="center">
  <strong>Kalash</strong><br/>
  Terminal-native AI coding agent — multi-provider, permissioned, and memory-aware.
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#why-kalash">Why Kalash</a> ·
  <a href="#features">Features</a> ·
  <a href="#providers--limits">Providers</a> ·
  <a href="#memory">Memory</a> ·
  <a href="#development">Development</a>
</p>

---

## Why Kalash

Most coding agents assume unlimited context and a single cloud provider. Real workflows hit **TPM ceilings**, **tool permission boundaries**, and **sessions that need to remember what you decided yesterday**.

Kalash is built for that reality: a fully agentic loop in your terminal that works across Anthropic, OpenAI, Groq, Google, and OpenAI-compatible endpoints — with sandboxed execution, explicit approvals, MCP tools, and a first-class memory layer.

Use it like Cursor or OpenCode, but **you own the runtime**: headless CI, interactive TUI, or Python SDK.

---

## Quick start

**Requirements:** Python 3.12+, [uv](https://docs.astral.sh/uv/)

```bash
git clone https://github.com/your-org/kalash-code.git
cd kalash-code
uv sync --all-extras
uv run kalash
```

Connect a provider in the TUI with `/connect`, or run headless:

```bash
export GROQ_API_KEY=gsk_...
kalash -p "explain this repo and suggest next steps"

echo "fix the failing test" | kalash
kalash -p "continue" --resume ses_abc123 --output-format json
```

Verify your setup:

```bash
uv run kalash doctor
uv run kalash status
```

---

## Features

### Interactive TUI
Rich terminal UI with live tool output, diff previews, session resume, model switching, and a status line that reflects **provider constraints** (context window, TPM, active tool profile).

### Headless & scriptable
Same agent core as the TUI — pipe prompts, stream JSON events, resume sessions, and pin model/sandbox/approval mode from the CLI.

```bash
kalash -p "task" --output-format stream-json
kalash -p "task" --model anthropic/claude-sonnet-4-5
kalash -p "task" --sandbox read-only --approval never
```

### Multi-provider, limit-aware
Per-model caps for output tokens, context window, and **tokens-per-minute** are resolved before each request. Throughput-limited models (e.g. Groq free tier) use **phased iterations**: fixed token budget per step, context rollup between steps, and a **final synthesis pass** when a long task completes.

### Permission gate & sandbox
Every tool call is classified, policy-checked, and optionally approved. Shell runs inside a Linux sandbox with configurable write roots and network grants for package managers.

### Tools & MCP
Built-in filesystem, shell, search, web, todo/plan, scratchpad, subagents, and skills. MCP servers extend the tool surface without forking the agent.

### Memory layer
Local SQLite + FTS recall/capture out of the box; optional mem0 backend. Memories inject at turn start and extract after turns — preferences, errors, and project facts persist across sessions.

### Sessions & scheduling
SQLite-backed transcripts, export/resume, grant persistence, cron-style scheduled runs, and a background serve daemon.

---

## CLI reference

| Command | Purpose |
|---------|---------|
| `kalash` | Interactive TUI |
| `kalash -p "…"` | Headless one-shot prompt |
| `kalash doctor` | Provider, sandbox, memory, tools health check |
| `kalash status` | Config and session snapshot |
| `kalash provider` | Manage model providers |
| `kalash session` | List, export, resume sessions |
| `kalash memory` | Add, search, export memories |
| `kalash agents` | Subagent definitions (`.kalash/agents/*.md`) |
| `kalash skills` | Project skills |
| `kalash hooks` | Pre/post tool hooks |
| `kalash mcp` | MCP server configuration |
| `kalash cron` / `kalash serve` | Scheduled agent runs |

---

## Project layout

```
.kalash/
  agents/       Subagent definitions (*.md + YAML frontmatter)
  skills/       SKILL.md files
  hooks/        Hook configs
  settings/     MCP and project settings

~/.kalash/      User credentials, defaults, global skills/agents
```

---

## Providers & limits

Kalash resolves model limits from a static catalog, **provider defaults** (e.g. Groq → 8k TPM), and **runtime learning** when an API returns an explicit ceiling. Tool profiles shrink automatically on small models; large models receive the full schema.

Supported provider families (via optional extras):

| Extra | Providers |
|-------|-----------|
| `anthropic` | Claude |
| `openai` | GPT, o-series |
| `google` | Gemini |
| `openai_compatible` | Groq, Together, local OpenAI-compatible servers |

Install everything:

```bash
uv sync --extra all
```

---

## Memory

Built-in engine comparable to mem0/supermemory:

- **Recall** at turn start — relevant memories inject into context
- **Capture** after turns — preferences, errors, tooling signals
- **Tools** — `recall`, `remember`, `forget` in the agent loop
- **CLI** — `kalash memory add|search|ls|forget|export|doctor`

Configure in `.kalash/settings.json`:

```json
{
  "memory": {
    "enabled": true,
    "primary": "local",
    "providers": ["local"],
    "recall_budget": 0.08
  }
}
```

Custom backends via the `kalash.memory_providers` entry point. Optional mem0: `uv sync --extra mem0`.

---

## Architecture (high level)

```
CLI / TUI / SDK
      │
      ▼
  Agent (build_agent)
      │
      ├── ModelGateway ──► Anthropic / OpenAI / Groq / …
      ├── ToolHost ──────► permissions → sandbox → tools / MCP
      ├── ContextAssembler
      ├── AgentLoop ─────► assemble → stream → tools → repeat
      │                      └── phased iterations + synthesis
      └── MemoryRouter ──► local FTS / mem0 / custom providers
```

---

## Development

```bash
uv run pytest -q          # 424+ tests
uv run ruff check src tests
```

---

## License

MIT
