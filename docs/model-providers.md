# Model Providers & Routing

> **Status:** normative.
>
> This document specifies how Kalash discovers, connects, routes between, and manages LLM providers.
> The goal: the user picks providers from a catalog, connects them with one command or TUI interaction,
> and Kalash routes requests across them — primary, fallback, per-task, per-cost — without touching
> code. Adding a provider is a config change, not a feature request.

---

## 1. Design intent

The screenshot is the vision: a TUI dialog listing **Popular** providers at the top (recommended,
proven, well-tested) and an alphabetical **Providers** list below with every OpenAI-compatible endpoint
and aggregator in the ecosystem. One selection, one auth flow, and it works.

Kalash is **provider-agnostic by construction**. The gateway normalizes every provider into one message
format, one streaming protocol, one usage model ([`model-gateway.md`](./model-gateway.md)). A user can
run:

- One provider (Anthropic only, offline Ollama only)
- Multiple simultaneously with routing rules
- An aggregator that already federates (OpenRouter, AI-ROUTER, 302.AI)
- A self-hosted stack (vLLM, TGI, llama.cpp) behind the OpenAI-compatible adapter

No provider is privileged in the code. No import of `anthropic` or `openai` lives outside
`models/providers/`.

---

## 2. Provider registry

### 2.1 The catalog

Shipped as `kalash/models/catalog.toml`, updateable via `kalash provider update-catalog` (pulls from a
pinned URL, verified by digest). Contains every known provider with connection metadata:

```toml
# kalash/models/catalog.toml  (shape)

[meta]
version = "2026-08-01"
providers_count = 87

# ─── Popular ───────────────────────────────────────────────────────────────

[providers.anthropic]
display_name = "Anthropic"
category = "popular"
auth_type = "api_key"
auth_env = "ANTHROPIC_API_KEY"
base_url = "https://api.anthropic.com"
adapter = "anthropic"
docs_url = "https://docs.anthropic.com"
models = ["claude-sonnet-4-5-20250514", "claude-haiku-4-20250414", "claude-opus-4-20250514"]
default_model = "claude-sonnet-4-5-20250514"
features = ["tool_use", "vision", "streaming", "reasoning", "prompt_caching"]
pricing_source = "registry"

[providers.openai]
display_name = "OpenAI"
category = "popular"
auth_type = "api_key"
auth_env = "OPENAI_API_KEY"
base_url = "https://api.openai.com/v1"
adapter = "openai"
models = ["gpt-5.2", "gpt-4.1", "o3", "o4-mini"]
default_model = "gpt-5.2"
features = ["tool_use", "vision", "streaming", "structured_output", "reasoning"]

[providers.google]
display_name = "Google"
category = "popular"
auth_type = "api_key"
auth_env = "GOOGLE_API_KEY"
base_url = "https://generativelanguage.googleapis.com/v1beta"
adapter = "google"
models = ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"]
default_model = "gemini-2.5-flash"
features = ["tool_use", "vision", "streaming", "reasoning"]

[providers.github_copilot]
display_name = "GitHub Copilot"
category = "popular"
auth_type = "oauth"
auth_env = "GITHUB_TOKEN"
adapter = "openai_compatible"
base_url = "https://api.githubcopilot.com"
features = ["tool_use", "streaming"]
note = "Requires GitHub Copilot subscription"

[providers.ollama]
display_name = "Ollama (Local)"
category = "popular"
auth_type = "none"
base_url = "http://localhost:11434/v1"
adapter = "openai_compatible"
features = ["tool_use", "streaming"]
note = "Runs locally, no API key needed"
local = true

[providers.openrouter]
display_name = "OpenRouter"
category = "popular"
auth_type = "api_key"
auth_env = "OPENROUTER_API_KEY"
base_url = "https://openrouter.ai/api/v1"
adapter = "openai_compatible"
features = ["tool_use", "vision", "streaming"]
note = "Access 200+ models via one key"

# ─── Aggregators & Platforms ───────────────────────────────────────────────

[providers."302.ai"]
display_name = "302.AI"
category = "aggregator"
auth_type = "api_key"
auth_env = "THREE02_API_KEY"
base_url = "https://api.302.ai/v1"
adapter = "openai_compatible"

[providers.ai_router]
display_name = "AI-ROUTER"
category = "aggregator"
auth_type = "api_key"
base_url = "https://api.ai-router.com/v1"
adapter = "openai_compatible"

[providers.abacus]
display_name = "Abacus"
category = "provider"
auth_type = "api_key"
base_url = "https://api.abacus.ai/v1"
adapter = "openai_compatible"

[providers.together]
display_name = "Together AI"
category = "provider"
auth_type = "api_key"
auth_env = "TOGETHER_API_KEY"
base_url = "https://api.together.xyz/v1"
adapter = "openai_compatible"
features = ["tool_use", "streaming", "vision"]

[providers.groq]
display_name = "Groq"
category = "provider"
auth_type = "api_key"
auth_env = "GROQ_API_KEY"
base_url = "https://api.groq.com/openai/v1"
adapter = "openai_compatible"
features = ["tool_use", "streaming"]
note = "Ultra-low latency inference"

[providers.fireworks]
display_name = "Fireworks AI"
category = "provider"
auth_type = "api_key"
auth_env = "FIREWORKS_API_KEY"
base_url = "https://api.fireworks.ai/inference/v1"
adapter = "openai_compatible"
features = ["tool_use", "streaming"]

[providers.deepseek]
display_name = "DeepSeek"
category = "provider"
auth_type = "api_key"
auth_env = "DEEPSEEK_API_KEY"
base_url = "https://api.deepseek.com/v1"
adapter = "openai_compatible"
features = ["tool_use", "streaming", "reasoning"]

[providers.mistral]
display_name = "Mistral"
category = "provider"
auth_type = "api_key"
auth_env = "MISTRAL_API_KEY"
base_url = "https://api.mistral.ai/v1"
adapter = "openai_compatible"
features = ["tool_use", "streaming", "vision"]

[providers.cerebras]
display_name = "Cerebras"
category = "provider"
auth_type = "api_key"
base_url = "https://api.cerebras.ai/v1"
adapter = "openai_compatible"
note = "Ultra-fast inference"

[providers.sambanova]
display_name = "SambaNova"
category = "provider"
auth_type = "api_key"
base_url = "https://api.sambanova.ai/v1"
adapter = "openai_compatible"

[providers.perplexity]
display_name = "Perplexity"
category = "provider"
auth_type = "api_key"
auth_env = "PERPLEXITY_API_KEY"
base_url = "https://api.perplexity.ai"
adapter = "openai_compatible"
features = ["streaming"]
note = "Search-augmented generation"

[providers.aws_bedrock]
display_name = "AWS Bedrock"
category = "cloud"
auth_type = "aws"
adapter = "bedrock"
features = ["tool_use", "vision", "streaming"]
note = "Uses AWS credentials (IAM/SSO)"

[providers.azure_openai]
display_name = "Azure OpenAI"
category = "cloud"
auth_type = "azure"
adapter = "openai_compatible"
features = ["tool_use", "vision", "streaming"]
note = "Requires deployment name and endpoint"

[providers.vertex_ai]
display_name = "Google Vertex AI"
category = "cloud"
auth_type = "gcloud"
adapter = "google"
features = ["tool_use", "vision", "streaming"]
note = "Uses GCP credentials"

# ... 60+ more providers follow the same shape
```

### 2.2 Categories

| Category | Meaning | TUI placement |
|---|---|---|
| `popular` | First-party adapters, well-tested, recommended | Top section, always visible |
| `aggregator` | Routes to many models via one key (OpenRouter, 302.AI, AI-ROUTER) | Second section |
| `cloud` | Enterprise cloud (Bedrock, Azure, Vertex) | Third section |
| `provider` | Direct access to a specific model provider | Alphabetical list |
| `local` | Runs on the user's machine, no network | Highlighted separately |

### 2.3 Custom providers

Users can add any OpenAI-compatible endpoint without it being in the catalog:

```
kalash provider add --name "my-vllm" \
  --base-url "http://gpu-box:8000/v1" \
  --adapter openai_compatible \
  --model "qwen3-coder-30b"
```

Or via config:

```toml
[providers.my_vllm]
display_name = "My vLLM"
base_url = "http://gpu-box:8000/v1"
adapter = "openai_compatible"
models = ["qwen3-coder-30b"]
default_model = "qwen3-coder-30b"
local = true
```

---

## 3. Adapters

Five adapters cover the entire ecosystem. Adding a provider almost never means adding an adapter.

| Adapter | Covers | Wire protocol |
|---|---|---|
| `anthropic` | Anthropic direct | Messages API, native tool use, thinking blocks |
| `openai` | OpenAI direct | Chat Completions, native structured output |
| `openai_compatible` | **Everything else** — Together, Groq, Fireworks, DeepSeek, Mistral, Ollama, vLLM, TGI, OpenRouter, 302.AI, etc. | OpenAI-compatible Chat Completions |
| `google` | Google Gemini, Vertex AI | GenerativeLanguage / Vertex API |
| `bedrock` | AWS Bedrock | Bedrock Converse API with SigV4 |

The `openai_compatible` adapter is the escape hatch — any new provider that speaks the OpenAI
format works on day one with zero code. Its capabilities default conservatively (`tool_use: false`,
small context) and can be overridden per provider in config or catalog.

### 3.1 Adapter protocol

```python
class ModelProvider(Protocol):
    name: str
    async def stream(self, request: NormalizedRequest) -> AsyncIterator[StreamEvent]: ...
    async def count_tokens(self, request: NormalizedRequest) -> int | None: ...
    def capabilities(self, model_id: str) -> ModelCapabilities: ...
    async def health(self) -> ProviderHealth: ...
```

All network I/O through `core.egress` (I-003). No retries (gateway owns that). No database access.
No payload logging. Adapter is a pure translator.

---

## 4. Connection flow

### 4.1 TUI (`kalash` interactive)

```
┌─────────────────────────────────────────────────────────────┐
│  Connect a provider                                    esc  │
│                                                             │
│  ┌─────────────────────────────────────────────────┐       │
│  │ Search                                           │       │
│  └─────────────────────────────────────────────────┘       │
│                                                             │
│  Popular                                                    │
│  ✓ Anthropic (Connected)                                    │
│    OpenAI (ChatGPT Plus/Pro or API key)                     │
│    Google                                          ◀─────── │
│    GitHub Copilot                                           │
│    Ollama (Local, no key needed)                            │
│    OpenRouter (200+ models, one key)                        │
│                                                             │
│  Providers                                                  │
│    302.AI                                                   │
│    Abacus                                                   │
│    abliteration.ai                                          │
│    AI-ROUTER                                                │
│    AIHubMix                                                 │
│    AKI.IO                                                   │
│    Cerebras                                                 │
│    DeepSeek                                                 │
│    Fireworks AI                                             │
│    Groq                                                     │
│    ...                                                      │
│                                                             │
│  tab: filter  ↑↓: navigate  enter: connect  esc: close     │
└─────────────────────────────────────────────────────────────┘
```

On selection:

```
Connecting to Google...

  1. Enter your API key (or press Enter to use GOOGLE_API_KEY from env):
     ▶ ************************************

  2. Testing connection... ✓ gemini-2.5-flash responding
  3. Discovering models... found 5 models
  4. Stored credential in OS keyring

  ✓ Google connected. Set as:
    [p] primary   [f] fallback   [t] task-specific   [s] skip for now

  Choice: p

  ✓ Google (gemini-2.5-flash) is now your primary model.
    Previous primary (anthropic/claude-sonnet-4-5) moved to fallback.
```

### 4.2 CLI

```bash
# Connect interactively
kalash provider connect google

# Connect non-interactively (CI, scripting)
kalash provider connect google --api-key-env GOOGLE_API_KEY --role primary

# Connect a custom endpoint
kalash provider connect --name my-ollama \
  --base-url http://localhost:11434/v1 \
  --adapter openai_compatible \
  --model qwen3-coder:30b \
  --role fallback

# List connected
kalash provider ls

# Test a provider
kalash provider test google

# Disconnect
kalash provider disconnect google

# Browse catalog
kalash provider catalog [--search "fast"]
```

### 4.3 Auth types

| Type | Flow |
|---|---|
| `api_key` | Prompt or env var → stored in OS keyring (never config) |
| `oauth` | Browser flow → token stored in keyring → auto-refresh |
| `aws` | `~/.aws/credentials` / SSO / IAM role — uses `boto3` resolution |
| `azure` | Endpoint + deployment + key, or Azure AD token |
| `gcloud` | Application Default Credentials, or service account JSON |
| `none` | Local providers (Ollama, vLLM) — no auth needed |

Credentials are **never written to config files** (I-035). They go to the OS keyring via the
`keyring` library. `kalash doctor` reports when a credential is missing or expired.

---

## 5. Model routing

### 5.1 Roles

Every connected provider has a role:

| Role | Meaning |
|---|---|
| `primary` | First choice for all requests |
| `fallback` | Used when primary fails or is unavailable (ordered list) |
| `task` | Used for specific task types (embeddings, reranking, extraction) |
| `budget` | Used when cost ceiling is approaching — cheaper alternative |
| `disabled` | Connected but not active |

### 5.2 Configuration

```toml
[model]
primary = "google/gemini-2.5-flash"
fallback = ["anthropic/claude-sonnet-4-5", "ollama/qwen3-coder:30b"]
budget = "groq/llama-4-scout"       # used when cost approaches ceiling

[model.tasks]
embedding = "local/bge-small-en-v1.5"          # memory embeddings
extraction = "google/gemini-2.5-flash"          # memory fact extraction
rerank = "local/bge-reranker-v2-m3"            # recall reranking
compaction = "google/gemini-2.5-flash"          # context summarization
title = "groq/llama-4-scout"                    # session title generation

[model.routing]
strategy = "primary_with_fallback"              # or: cost_optimized | latency_optimized | manual

[model.routing.cost_optimized]
simple_tasks_model = "groq/llama-4-scout"      # file reads, simple edits
complex_tasks_model = "anthropic/claude-sonnet-4-5"   # multi-file changes, architecture
threshold = "auto"                              # or token count

[model.routing.latency_optimized]
prefer_local = true
max_remote_latency_ms = 3000
```

### 5.3 Routing strategies

| Strategy | Behaviour |
|---|---|
| `primary_with_fallback` | Default. Primary always; fallback on error per [`model-gateway.md`](./model-gateway.md) §7 |
| `cost_optimized` | Routes simple tasks to cheaper/faster models, complex tasks to capable ones. Task complexity estimated from: turn count, files touched, tool diversity, explicit user signal |
| `latency_optimized` | Prefer local models; fall back to remote only when local lacks a required capability (tool use, vision) |
| `manual` | User explicitly picks per session via `/model <name>` slash command |

### 5.4 The `<provider>/<model>` naming

```
anthropic/claude-sonnet-4-5
openai/gpt-5.2
google/gemini-2.5-flash
ollama/qwen3-coder:30b
my-vllm/qwen3-coder-30b
openrouter/anthropic/claude-sonnet-4-5    # aggregator routing
```

Slash-separated. The provider prefix is how the gateway knows which adapter to use. For aggregators
that proxy other providers, the naming can be nested — `openrouter/anthropic/claude-sonnet-4-5` routes
through OpenRouter's Anthropic endpoint.

### 5.5 Model switching mid-session

```
/model google/gemini-2.5-pro
```

Switches the model for subsequent turns in this session. Opaque payloads (reasoning blocks) are
stripped on switch per [`model-gateway.md`](./model-gateway.md) §2.2. Context is re-measured against
the new model's window. The switch is audited and visible in the session record (I-037).

---

## 6. Provider health and status

```
$ kalash provider ls

PROVIDER           MODEL                      ROLE       STATUS      LATENCY
anthropic          claude-sonnet-4-5         fallback   healthy     420ms
google             gemini-2.5-flash          primary    healthy     180ms
ollama             qwen3-coder:30b           fallback   healthy     95ms (local)
groq               llama-4-scout             budget     degraded    rate-limited
openrouter         —                          disabled   —           —

$ kalash provider test google
Testing google (gemini-2.5-flash)...
  auth          ✓ API key valid
  connection    ✓ 180ms to first byte
  tool_use      ✓ tool call round-trip OK
  streaming     ✓ chunks received
  vision        ✓ image described correctly
  tokens        ✓ usage reported: 127 in / 45 out

All checks passed.
```

Provider health feeds the circuit breaker in `state-machines.md` §10 and the fallback decision in
`model-gateway.md` §7.

---

## 7. Cost-aware routing

When `strategy = "cost_optimized"`:

```
                    ┌──────────────────────┐
user message ──────▶│  Complexity estimator │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │   simple (< threshold)│────▶ budget model (cheap/fast)
                    │   complex             │────▶ primary model (capable)
                    │   requires vision     │────▶ vision-capable model
                    │   requires reasoning  │────▶ reasoning model
                    └──────────────────────┘
```

Complexity signals: multi-file references, architectural questions, "refactor", "redesign",
"migrate", explicit `/model` override, prior turn count in this task, number of tool calls in the
prior turn, and a learned threshold from session history.

The estimator is **conservative**: if in doubt, route to the capable model. Degrading quality to save
money without the user noticing is the failure mode, and it compounds — a cheap model that makes a
wrong plan costs more to correct than the capable model would have cost to run.

Budget ceiling proximity: when `cost / ceiling > 0.8`, all non-essential calls (title generation,
extraction for low-salience content) route to the budget model automatically. The user is warned.
At `> 0.95`, a hard prompt asks whether to continue at all.

---

## 8. Provider entry points (extensibility)

Third-party model providers ship as Python packages:

```toml
[project.entry-points."kalash.model_providers"]
my_provider = "my_package:MyModelProvider"
```

Same rules as memory providers: no direct HTTP clients (I-003), no DB access, no logging payloads.
Adapter protocol + conformance suite. Content-hash-pinned trust (I-031).

A new provider that speaks the OpenAI format needs zero code — just a catalog entry or a config
block using `adapter = "openai_compatible"`. Entry points are for providers with genuinely novel
wire protocols.

---

## 9. Slash commands for routing

Available in the TUI:

| Command | Effect |
|---|---|
| `/model <provider/model>` | Switch model for this session |
| `/model` | Show current model and routing config |
| `/provider connect` | Open the connection TUI |
| `/provider ls` | Show connected providers and health |
| `/provider test <name>` | Run the health check |
| `/cost` | Show session spend so far, ceiling, and projection |

---

## 10. Non-goals and honest limits

- **No model marketplace or billing.** Kalash connects to providers the user already has accounts
  with. It does not resell access or aggregate billing.
- **No automatic model selection per turn without the `cost_optimized` strategy explicitly enabled.**
  Silent model switching feels like inconsistency and is a support nightmare.
- **No training or fine-tuning integration.** Out of scope.
- **The `openai_compatible` adapter cannot cover providers with genuinely novel protocols.** For
  those, a dedicated adapter or entry-point package is needed. This is rare (Anthropic, Google, and
  Bedrock each needed one; everyone else uses OpenAI-compatible).

---

## 11. Testing

| Concern | Approach |
|---|---|
| Catalog parse | Every entry valid, no duplicate keys, all `adapter` values correspond to a real adapter |
| Connection flow | Scripted TUI test via `Pilot`: connect, auth, verify, set role |
| Routing strategies | Recorded fixtures: assert correct model selected per strategy under various conditions |
| Fallback | Fake providers scripted to fail each error class; assert the decision matrix from `model-gateway.md` §7 |
| Cost routing | Assert complexity estimator routes simple/complex correctly, and that budget-proximity triggers the switch |
| Health/circuit breaker | Match `state-machines.md` §10 |
| Custom providers | Add via config, assert usable without code changes |
| Entry points | Third-party adapter registered, loaded, passes conformance |

---

*See also: [`model-gateway.md`](./model-gateway.md) for normalization, streaming, and fallback;
[`context-budget.md`](./context-budget.md) for window re-fitting on model switch;
[`security.md`](./security.md) §3 for credential lifecycle;
[`operations.md`](./operations.md) for the model/pricing registry versioning.*
