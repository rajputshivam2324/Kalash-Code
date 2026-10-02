# 05 — Provider Adapters and Weak-Model Robustness

Provider APIs change; treat the specifics below as **patterns to verify against current docs** and pin behavior with adapter contract tests. Keep model names, prices and context sizes in config.

## Contents
1. Adapter contract and normalization
2. Provider notes: Anthropic, OpenAI, DeepSeek, local/vLLM
3. Reasoning models: handling and round-tripping
4. Tool-call repair
5. Weaker or local models: strategies
6. Error classification
7. Routing, fallback, circuit breaking
8. Adapter contract tests

---

## 1. Adapter contract and normalization

Every adapter implements `stream()` and yields normalized events:

```python
StreamEvent = TextDelta | ThinkingDelta | ToolCallStart(id,name) | ToolArgsDelta(id, json_fragment)
            | ToolCallEnd(id) | UsageUpdate | Finish(reason) | Error
```

and accumulates into one `ModelResponse`. Normalize:
- **Messages in**: core `Message` → provider format (system placement, role names, tool result placement, images, cache markers).
- **Tool schemas**: some providers support only a JSON Schema subset; the adapter *downgrades* (strip `$ref`, `oneOf`, `format`, `pattern`) and records which constraints were dropped so the executor re-validates locally.
- **Tool calls out**: unique ids (synthesize if missing), args parsed to dict; on invalid JSON keep `raw_args` + `parse_error` (do not raise).
- **Finish reasons** → `end_turn | tool_use | max_tokens | content_filter | error`.
- **Usage** → `Usage` incl. cache read/write and reasoning tokens (providers name them differently).
- **Errors** → `ModelError(kind, retryable, retry_after, status, request_id)`.
- Capture `request_id` and a `raw_ref` blob (redacted) for debugging and replay.

## 2. Provider notes

### Anthropic (Messages API)
- System prompt is a top-level field; messages alternate user/assistant; tool results are `tool_result` blocks inside a **user** message that immediately follows the assistant message with the `tool_use` blocks. **All results for parallel calls go in one user message**, every `tool_use_id` answered.
- Extended thinking: assistant `thinking` blocks carry signatures; during a tool loop they must be passed back **unmodified** with the tool_use turn. Store them in `Thinking.provider_state`. Don't edit or reorder them.
- Prompt caching: `cache_control` breakpoints (limit per request); place after tools/instructions and near the end of history. Track `cache_creation_input_tokens` vs `cache_read_input_tokens`.
- Streaming: `content_block_delta` with `input_json_delta` for tool args; assemble then parse.
- Tool choice: `auto | any | tool | none`.

### OpenAI-style (Chat Completions and Responses)
- Chat Completions: assistant message has `tool_calls[]` with `function.arguments` as a **JSON string** (can be invalid); results are `role="tool"` messages keyed by `tool_call_id`. Parallel calls are default; one `tool` message per call.
- Responses API: items (`message`, `function_call`, `function_call_output`, reasoning items); supports stateless operation (resend items) or server-side state (`previous_response_id`). For a harness that needs auditability and replay, prefer **stateless** and store items yourself. Preserve reasoning items (possibly encrypted) when the provider requires them across tool turns.
- Structured outputs / strict function schemas: enable `strict` where supported; still validate locally.

### DeepSeek (OpenAI-compatible API)
- Chat-Completions-compatible endpoint, so a generic OpenAI-compat adapter works with a base-URL swap; implement `DeepSeekAdapter` as a thin subclass for the differences.
- Reasoning models return the chain of thought in a separate field (`reasoning_content`) next to `content`. Handling differs **by model version** and by whether a tool-call loop is in progress: some versions reject requests that echo `reasoning_content` back, others expect it preserved within a single tool-calling turn and dropped on the next user turn. Encode this as `caps.reasoning` plus an adapter flag, and pin it with a contract test against the live API so a provider change fails CI instead of production.
- Context caching is automatic on repeated prefixes (disk-based); stable prompt layout (`04` §1) directly lowers cost. Read the cache hit/miss counts from usage.
- Function calling quality is lower than frontier closed models on complex schemas: simplify schemas, prefer `edit_file` over a patch DSL, enable tool-call repair (§4).
- Rate limits and busy periods are common; ensure jittered retry, concurrency limiting and a fallback model.

### Local / vLLM / SGLang / llama.cpp server
- Serve an OpenAI-compatible endpoint; use the same adapter with `caps` set explicitly (context window, tool reliability: low/medium).
- Tool calling depends on the **chat template and parser** (e.g. vLLM's tool-call parsers per model family). Verify with the contract test; mismatch shows up as tool calls emitted as plain text.
- Use **guided/constrained decoding** (JSON schema / grammar via the server's guided-decoding options, xgrammar/outlines) to force valid tool-call JSON when reliability is low.
- Enable prefix caching on the server. Watch KV-cache pressure with long contexts; compact earlier (e.g. L1 at 40%).
- Concurrency: bound parallel runs to the server's batch capacity; track queue time as a separate latency metric.

## 3. Reasoning models

Principles:
- Reasoning text is **model output, not evidence**. Log it (redacted) for debugging; never use it to authorize actions, as verification input, or in user-facing claims.
- Do not rely on its presence: some providers return summaries, some hide it, some encrypt it.
- Round-trip rules live in the adapter. Core only sees `Thinking(text, provider_state)`.
- Budget it: `max_output` must cover reasoning + answer; record `reasoning_tokens`; consider per-turn reasoning effort (`low/medium/high`) chosen by phase: high for planning and failure analysis, low for mechanical edits.
- During compaction, drop reasoning blocks (they are not part of the durable task state) unless the provider needs them for an in-flight tool turn.

## 4. Tool-call repair

Run in the adapter *after* accumulation, *before* handing to core. Each step is logged as `ToolCallRepaired{kind}` so you can measure model quality.

1. Strip code fences / leading prose around JSON.
2. `json.loads`; on failure try a tolerant parser (`json5`/`json_repair`): trailing commas, single quotes, unquoted keys, truncated brackets.
3. Coerce types to schema (`"5"→5`, `"true"→true`, string→list for single-element arrays) **only** when unambiguous.
4. Unknown tool name → fuzzy match against registered names (edit distance ≤ 2, unique) else error result listing valid names.
5. Missing required args → do not guess; return a schema-pointing error.
6. Tool call emitted as text (`<tool_call>…</tool_call>`, `{"name":…}` in content) → text-fallback parser per model family, enabled by `caps.tool_arg_reliability`.

Never repair in ways that change meaning of paths or commands.

## 5. Weaker or local models: strategies

| Problem | Mitigation |
|---|---|
| Forgets tool schema | Fewer tools (8–10), shorter descriptions with one example each, flatter schemas |
| Invalid JSON args | Constrained decoding where available; repair (§4); lower temperature (0–0.2) |
| Wrong edit format | Use `edit_file` replace strategy; add near-miss diagnostics; optional flexible-match mode |
| Doesn't stop / loops | Tighter `LoopDetector`, lower `max_iterations`, mandatory plan turn |
| Declares done early | Completion gate is essential |
| Context degradation at long lengths | Compact earlier (40–60%), keep summaries structured, put critical constraints in the volatile tail reminder |
| Over-calls tools in parallel | Cap `max_parallel_tools`; serialize writes |
| Ignores system prompt rules | Enforce in code (policy, gate); keep prompt short and imperative |

Maintain **model profiles** in config (not code branches):

```yaml
profiles:
  frontier:   {edit_strategy: both,    max_parallel_tools: 6, compaction: {l1: .60, l2: .80}, repair: minimal}
  mid_tier:   {edit_strategy: replace, max_parallel_tools: 3, compaction: {l1: .50, l2: .70}, repair: full}
  local_small:{edit_strategy: replace, max_parallel_tools: 1, compaction: {l1: .40, l2: .60}, repair: full,
               constrained_decoding: true, tools: [read_file, grep, glob, edit_file, bash, todo_write]}
```

## 6. Error classification

```python
def classify_model_error(e) -> ErrorClass:
    if e.status in (408, 409, 425, 429, 500, 502, 503, 504, 529) or e.kind in ("timeout","connection","overloaded"):
        return Transient(retry_after=e.retry_after)
    if e.status == 400 and "context" in e.message.lower() and "length" in e.message.lower():
        return ContextOverflow()
    if e.status in (401, 403): return Permanent("auth")
    if e.status == 400: return Permanent("bad_request")     # schema/ordering bugs: fix the adapter, don't retry
    if e.kind == "content_filter": return Permanent("filtered")
    return Unknown()
```

Quota/billing errors (402/insufficient_quota) are permanent for the run and should alert operators.

## 7. Routing, fallback, circuit breaking

- **Role-based routing** (config): `planner`, `executor`, `summarizer`, `explorer_subagent`, `verifier`. Use a cheaper/faster model for summarization and read-only exploration subagents; the strongest model for planning and hard failure analysis. Don't switch the main executor model mid-run unless escalating (cache invalidation).
- **Fallback chain** on `TransientModelError` after retries: secondary model with equal tool capabilities; emit `ModelFallback`. Do not fall back on permanent errors.
- **Circuit breaker** per provider+model: open after k consecutive failures or error rate >x% over window; half-open probe every 30 s. While open, route to fallback or fail fast.
- **Shared limiter** across concurrent runs: token bucket on requests/min and tokens/min per API key; queue with priority (interactive > batch).

## 8. Adapter contract tests (run on every adapter, live nightly + recorded per-PR)

- Plain text response; streaming deltas reassemble to the same text.
- Single tool call; multiple parallel tool calls; tool call with large args; invalid-JSON args surfaced as `parse_error`.
- Round trip: assistant tool call → tool result → final answer (validates message ordering rules).
- Reasoning round-trip within a tool loop and across user turns (per provider rules).
- `max_tokens` finish with and without a partial tool call.
- Usage fields populated; cache read tokens reported on second identical request.
- Error mapping: forced 429/500/401/400-context-overflow via a mock server → expected `ErrorClass`.
- Cancellation mid-stream closes the HTTP connection promptly (<1 s).
- Golden recorded fixtures (VCR-style, secrets stripped) for deterministic CI.
