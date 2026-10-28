# Kalash Code

A terminal coding agent: one loop, a durable session log, and tools that run inside an OS sandbox.

```bash
uv sync --extra dev
uv run kalash doctor                      # checks Python, sandbox, ripgrep, keys
uv run kalash provider anthropic          # store a key (or set ANTHROPIC_API_KEY etc.)
uv run kalash models                      # what each provider offers, with context and price
uv run kalash model anthropic/claude-sonnet-5-5
uv run kalash                             # interactive UI
uv run kalash -p "fix the failing test"   # one-shot, scriptable
```

Python 3.12+ (tested on 3.12, 3.13 and 3.14). On Linux install `bubblewrap` (and `ripgrep` for
faster search); without a working sandbox Kalash refuses to run shell commands in confined modes
instead of running them unconfined. To use the command on your PATH from a checkout:
`pip install --user -U -e .` (the UI needs Textual 8, which this pulls in).

## Providers

Plain HTTP, no vendor SDKs. A model is `provider/model`; keys come from the environment first,
then `kalash provider ID` / `/provider` (encrypted in `~/.kalash/auth.json`). Each provider speaks the
wire format its current API requires - not a lowest-common-denominator one:

| Provider | Wire format | Key variable |
| --- | --- | --- |
| anthropic | Messages API: adaptive thinking + `effort`, prompt caching | `ANTHROPIC_API_KEY` |
| openai | Responses API (tools + reasoning; Chat Completions can't do both on current models) | `OPENAI_API_KEY` |
| google | native Gemini API with thought signatures | `GEMINI_API_KEY` / `GOOGLE_API_KEY` |
| deepseek | Messages-compatible `/anthropic` endpoint, thinking replayed | `DEEPSEEK_API_KEY` |
| xai | Responses API | `XAI_API_KEY` |
| moonshot, zai, sarvam | Chat Completions (reasoning fields handled per provider) | `MOONSHOT_API_KEY`, `ZAI_API_KEY`, `SARVAM_API_KEY` |
| minimax | Messages-compatible | `MINIMAX_API_KEY` |
| openrouter, groq, together, fireworks, cerebras | Chat Completions | `<NAME>_API_KEY` |
| ollama | Chat Completions, local | none (`OLLAMA_HOST`) |

`kalash models [provider]` lists the built-in catalog (context window, output limit, price, notes,
supported reasoning efforts); `kalash models --refresh` asks providers for their live lists and
caches them for a day. Retired ids are refused with the replacement named instead of failing with a
provider 404. Reasoning effort (`/effort`, `reasoning_effort`) is mapped to what each model accepts;
parameters a model rejects (sampling on reasoning models, for example) are never sent.

Any other OpenAI- or Anthropic-compatible endpoint: add it under `providers` in settings
(`kind`, `base_url`, `models`, `api_key_env`, `context_window`).

Errors are classified by HTTP status, never by digits in message text. Rate limits, 5xx and dropped
connections retry with backoff (honouring `Retry-After`); auth, quota, bad-request and
malformed-response errors do not. A context-overflow error triggers compaction and one retry. An
empty completion is retried once; if the model then stays silent *after a tool result*, it is asked
once, in words, for a summary - and only then reported as `EMPTY_RESPONSE`.

**Reasoning effort.** `/effort` opens a picker of exactly the levels the current model accepts
(`Ctrl+T` cycles them; `kalash --effort high` or `reasoning_effort` in settings sets it up front;
`/effort default` returns to the model's own default). `/effort auto` is opt-in: it sends the model's
lowest level for plain tool-result steps and its full level for the first step of a turn, after a
failed tool call and after an empty-reply nudge (changing the level between requests can cost cache
hits on providers that cache by request shape; the effect on cost is not measured). A
model with no reasoning control says so instead of pretending. The choice is saved with the session
and restored on resume.

> **Verification status.** Adapters are tested against wire payloads and a loopback server.
> Live trials have exercised Sarvam 105B, Sarvam GLM 5.3 Flash and Groq GPT-OSS 20B,
> including tools, context compaction and memory retrieval. Some trials failed; successful
> repairs used feedback and independent artifact checks. These runs do not establish a
> reliability rate or verify every provider/model combination.

## How a turn works

1. The user message is appended to the **session log** (`~/.kalash/sessions/<project>/<id>.jsonl`).
   Model history is derived from that log and nothing else. The message may start with
   `<system-reminder>` blocks - workspace instructions, the skill list, a runtime snapshot (working
   directory, permission mode, sandbox, date) - each sent only when it changed since last time. The
   **system prompt itself is static**, so a provider's prompt cache survives mode changes, edits to
   `AGENTS.md` and new days.
2. The model streams a response; it is committed to the log only when complete. A response cut
   off by the output limit never executes its half-written tool calls.
3. Tool calls run in model order. Read-only tools run concurrently. Every call gets exactly one
   result - if the process dies mid-call, resume writes "outcome unknown" for it.
4. The turn ends with an honest reason: `completed`, `max_tokens`, `stuck`, `cancelled`,
   `error` or `refused`. `completed` means the model stopped, not that the task is verified.

Guard rails against the failure shapes seen on real models (they live in `agent/guards.py` and
`tools/matching.py`, and are plain text appended to tool results, so they are logged):

- **Repeated failure.** The same command (or edits to the same file) failing with the same error
  - whatever else happens in between - draws a "that is not the cause" note on the 3rd failure and
  a "stop and report" note on the 5th; on the 6th identical failure the turn ends as `stuck`
  so the user can steer a genuinely unchanging failure. A failure that changes is never
  counted as a repeat, and a success resets the count.
- **Stale task list.** If the `todo_write` list still has open items after 8 tool calls, a reminder
  is appended so the visible progress stays honest.
- **Edit feedback.** A missed `edit` names the cause (copied `N: ` prefixes, indentation) and shows
  the closest text with line numbers; a successful one shows the changed region, so neither needs a
  re-read.

Context is measured from the provider's own token usage plus an estimate for what was added since.
At 80% of the window older messages are summarised into a checkpoint (the originals stay in the
log); the newest ~16% is kept verbatim and a tool call is never separated from its results.
Long histories are summarized incrementally, with each summary request sized for the current
context window. Oversized exchanges are reduced only in the summary copy; the exact originals
remain retrievable. Checkpoints also carry the original and latest user requests, current task
list and working notes independently of what the summarizer remembers to include.
When verbose tool documentation crowds a small window, Kalash shortens that documentation while
keeping all tool names, parameters and validation rules available. The selected schemas are logged.

## Tools

`read`, `write`, `edit`, `glob`, `grep`, `bash` (+ `job_output`, `job_kill`), `todo_write`,
`web_fetch`, `skill`, `task` (subagent), `memory`, `session_search`, and tools from MCP servers.

- Files: `read` returns `N: text` lines. Editing or overwriting an existing file requires having
  read it, and fails if it changed on disk since - tracked by Kalash, the model never echoes hashes.
  Your own consecutive edits need no re-read.
- `bash` returns combined output and `[exit code: N]`. Long output keeps head and tail and is saved
  to a file. A command that outlives its timeout keeps running as a background job.
- `web_fetch` blocks private, loopback and link-local addresses (re-checked on every redirect).
- `memory` remembers, recalls or forgets named notes. Session notes survive compaction and resume;
  project notes carry stable facts into new sessions in the same project. Notes include their
  source session and event number and remain model-authored data, not instructions or proof.
  Forget removes a note from active memory; the append-only source events remain in the log.
- `session_search` searches original messages, tool calls and tool results, including compacted
  history. It supports literal queries, event-number ranges and character pagination, so a smaller
  context window can recover exact evidence when a summary omits it.

## MCP servers

Local (stdio) and remote (Streamable HTTP) servers; their tools appear as `mcp__NAME__tool`.

```bash
kalash mcp add deepwiki https://mcp.deepwiki.com/mcp
kalash mcp add docs https://example.com/mcp -H "Authorization: Bearer ${DOCS_TOKEN}"
kalash mcp add time -- uvx mcp-server-time
kalash mcp test deepwiki        # connect and list tools: the quickest "does it work"
kalash mcp list | trust NAME [--off] | remove NAME
```

A remote server is probed for the current stateless protocol (`server/discover`, 2026-07-28) and
falls back to the earlier `initialize` + session-id handshake, so both kinds of public server work;
replies may be JSON or an event stream. HTTPS is required except for loopback, redirects are not
followed (they would carry your headers elsewhere), responses are capped at 16 MiB, and `${VAR}` in a
header is read from the environment so the token is not written to settings. Stdio servers get a
scrubbed environment (`--env NAME=value` adds to it). `mcp_servers` is honoured from user and local
settings only, never from a committed project file.

MCP tools **ask first** until you trust the server: `--trust` when adding, `kalash mcp trust NAME`,
or `/mcp trust NAME` for the current session. In *Auto* mode an untrusted MCP tool is denied and
the denial says which command to run. `/mcp` shows each server, its transport and its tools.

## Safety model

| Layer | What it does |
| --- | --- |
| Sandbox (`bwrap` / Seatbelt) | Commands see the filesystem read-only; only the workspace, temp and `sandbox.writable_roots` are writable. PID namespace, secrets masked, optional no-network. `.git/hooks`, `.git/config`, `.kalash` are read-only inside it. |
| Permissions | allow / ask / deny per tool call from declared effects. Writes outside the workspace are denied; protected paths ask; credentials are never readable. |
| Approval | `ask` prompts you in the UI; headless it is denied (or `--approval never`). Denials are reported in the result and exit code. |
| Environment | Secret-looking variables are removed from commands and hooks unless listed in `sandbox.pass_env`. |
| Settings trust | Committed `.kalash/settings.json` can only set model and behaviour keys; permissions, hooks, MCP and provider endpoints are ignored there (use `~/.kalash/settings.json` or `.kalash/settings.local.json`). |

Commands are deliberately **not** classified by parsing shell text. Add your own rules:
`permissions.deny` / `permissions.ask`, e.g. `["bash git push*", "write secrets/*"]`.

Not covered: the sandbox does not stop network exfiltration while `sandbox.network` is true;
`web_fetch` resolves DNS before connecting (a rebinding race is theoretically possible); MCP
servers and hooks run unsandboxed with your privileges (MCP tools ask first unless `trust: true`).
The macOS Seatbelt profile is generated and unit-tested but has not been run on a Mac here.

## Headless

```bash
kalash -p "task" [-m provider/model] [--output-format text|json|stream-json]
      [-c | -r SESSION] [--sandbox MODE] [--approval ask|never] [--cwd DIR]
echo "task" | kalash -m openai/gpt-5.6-sol
```

| Exit | Meaning |
| --- | --- |
| 0 | completed |
| 1 | provider or runtime error |
| 2 | usage or configuration error |
| 3 | completed, but some actions were denied |
| 4 | a configured limit stopped the run |
| 5 | stopped early (output limit, refusal, or `stuck`: one command failed 6 times with the same output) |
| 130 | cancelled (Ctrl+C) |

`json` prints one object: `ok`, `exit_code`, `reason`, `result`, `error`, `session_id`, `model`,
`steps`, `tool_calls`, `denials`, `usage`, `cost_usd`. `stream-json` emits one event per line and
ends with the same object. Cost is `null` unless you configure `pricing`.

## Interactive UI

A Textual 8 terminal UI: streamed Markdown answers, collapsible tool blocks with coloured diffs,
a status bar (mode, model, context meter, git branch, cost), and a themeable palette (`/theme`;
`kalash-dark` and `kalash-light` plus Textual's built-in schemes; the terminal's own colours with
`ansi-dark`).

| Input | Does |
| --- | --- |
| `Enter` / `Ctrl+J` | send (queued while the agent works) / new line |
| `Esc` | interrupt the turn; queued messages return to the prompt instead of being sent |
| `Shift+Tab` | cycle mode: **Ask** -> **Auto** -> **Plan** |
| `/` | the complete, scrollable command menu in sections (modes first); keep typing to filter |
| `Tab` / `Enter` in the menu | `Tab` completes the command word; `Enter` runs it |
| `@` | file-path completion (`Tab` or `Enter` inserts) |
| `!cmd` | run a shell command yourself, in the same sandbox |
| `Ctrl+O` | show or hide tool output, reasoning and folded "Explored" rows |
| `Ctrl+T` | cycle reasoning effort (levels the model accepts) |
| `Up` / `Down` | prompt history (persisted across sessions) |
| `PgUp` / `PgDn` / `Ctrl+End` | scroll / jump back to the newest output |
| `Ctrl+C` | copy selection, else interrupt, else clear; twice to quit |
| `?` or `F1` | help |

Large pastes collapse to `[Pasted text #1 +40 lines]` and expand when sent.

The transcript shows paths relative to the project, folds a run of finished reads/searches into one
"Explored N files" row (click it or `Ctrl+O` to open), flags a write that only changed whitespace,
shows the first line of the model's reasoning while it thinks, and ends a substantial turn with a
footer such as `✓ 31s · 19 tool calls · 181.0k in · 4.4k out · edited 1 file +61 −21`. When you
have scrolled up while output keeps arriving, a "new output" marker offers `Ctrl+End`.

**Modes** (`/permissions`, `Shift+Tab`): *Ask* edits inside the workspace and asks before risky
actions; *Auto* never prompts (protected paths and escalations are denied); *Plan* is read-only
research that ends with a plan you approve, optionally switching to Auto; *Read-only* refuses every
change; *Full access* disables the sandbox and prompts - choose it explicitly with `/full`
(it asks you to confirm). The model is told about a mode change at the start of the next message, never mid-turn.

**Undo.** `/undo` restores the files the agent wrote or edited in its last turn, but only those
unchanged since the agent touched them. Changes made through `bash` are not tracked.

Each mode has its own command - `/ask`, `/auto`, `/plan`, `/read-only`, `/full` - and the menu marks
the current one. `/full` asks you to confirm first. Other commands: `/permissions`, `/model`, `/new`, `/resume`, `/compact`, `/undo`, `/diff`,
`/init`, `/effort`, `/cost`, `/context`, `/status`, `/todos`, `/copy`, `/export`, `/rename`,
`/theme`, `/provider`, `/memory`, `/mcp` (`/mcp trust NAME`), `/skills`, `/verbose`, `/doctor`, `/help`, `/quit`.
Your own live in `.kalash/commands/<name>.md` (front matter `description`, body with
`$ARGUMENTS`).

## Settings

Order (later wins): defaults, `~/.kalash/settings.json`, `<project>/.kalash/settings.json`,
`<project>/.kalash/settings.local.json`, `KALASH_MODEL`, command line. Invalid values fail with the
file and key. `kalash config show` prints the effective result; `kalash config set KEY VALUE
[--scope user|project|local]` validates before saving.

```json
{
  "model": "anthropic/claude-sonnet-5-5",
  "permissions": {"sandbox": "workspace-write", "approval": "ask", "deny": ["bash git push*"]},
  "pricing": {"anthropic/claude-sonnet-5-5": {"input_per_mtok": 3, "output_per_mtok": 15}},
  "hooks": {"PreToolUse": [{"matcher": "^bash$", "command": "./scripts/check-command.sh"}]},
  "mcp_servers": {
    "git": {"command": "uvx", "args": ["mcp-server-git"], "trust": false},
    "deepwiki": {"url": "https://mcp.deepwiki.com/mcp"}
  }
}
```

Sessions have no harness-imposed token, dollar or step caps. Old `limits` and `budget` settings
are ignored, including settings inherited from earlier installs. Usage and known cost remain
visible as telemetry. Context-window and per-response output sizes describe actual request
capacity; they do not end a session because it has spent too much or taken too many steps.
Hooks (`PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`) get JSON on stdin; exit 2 blocks.
Skills are `<project>/.kalash/skills/<name>/SKILL.md` (or `~/.kalash/skills`) with `name` and
`description` frontmatter; run one with `/skill-name [task]`. Project guidance is read from
`AGENTS.md`, `CLAUDE.md` and `KALASH.md` (plus `*.local.md` overlays) in every directory from the
project root down to where you started, and from `~/.kalash/AGENTS.md`; identical files are read once.
It is delivered as guidance that does not override you or the system prompt, and an `AGENTS.md` in a
subfolder is picked up when the model reads or edits a file there.

## Python API

```python
from kalash.sdk import Kalash
from kalash.agent.events import TextChunk

async with Kalash(".", model="anthropic/claude-sonnet-5-5") as agent:
    result = await agent.run("fix the failing test")        # TurnResult
    async for event in agent.stream("explain this repo"):   # ends with TurnEnd
        if isinstance(event, TextChunk):
            print(event.text, end="")
```

## Layout

```
src/kalash/
  llm/       types, errors, SSE transport, adapters (Anthropic, OpenAI Responses, Gemini, Chat
             Completions), model catalog, providers, discovery, retry
  session/   append-only JSONL log, crash repair
  agent/     loop, compaction, prompt, runtime builder, subagent task tool
  tools/     read/write/edit/glob/grep, bash + jobs, web_fetch/todo/skill, file observation
  permissions.py  sandbox.py  hooks.py  mcp.py  skills.py  config.py  credentials.py  sdk.py
  modes.py  checkpoints.py  commands.py  history.py  gitinfo.py
  cli/  tui/ (app, widgets, screens, theme, app.tcss)
tests/
  harness/    ScriptedAdapter (raises when the script runs out), loopback model server, wire helpers
  unit/       adapters over raw wire payloads, tools, sandbox, permissions, MCP, session, config
  contract/   the real loop + tools + permissions + log over a scripted model
  integration/  SDK       process/  the real `kalash` command and the built wheel
  tui/        Textual pilot tests       live/  real providers (opt-in, costs money)
```

## Development

```bash
uv run pytest -q                 # everything except live and wheel tests
uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src
uv build && KALASH_WHEEL="$PWD"/dist/*.whl uv run pytest tests/process/test_wheel.py
KALASH_LIVE=1 ANTHROPIC_API_KEY=... uv run pytest tests/live    # real model, opt-in
uv run python scripts/tui_screenshots.py conversation approval   # render the real UI to PNG
```

Tests mock only the model (at the adapter boundary or over loopback HTTP); the loop, tools,
permissions, sandbox, storage and CLI are real. The suite blocks non-loopback sockets.
