# Model Gateway

> **Status:** normative.
>
> The gateway is the only path from Kalash to a language model. It normalizes wildly different provider
> APIs into one message format, one streaming event protocol, one usage accounting model, and one error
> taxonomy — so that `runtime/loop.py` contains no provider conditionals.

---

## 1. Design constraints

Three constraints shape everything here.

**Provider differences are not superficial.** They differ in message shape, system-prompt handling,
tool-call representation, reasoning-block semantics, cache control, and usage reporting. A thin wrapper
that papers over this leaks provider specifics into the loop, and the leak is discovered late — usually
when adding the third provider.

**Some provider data must survive round-trips without being understood.** Reasoning blocks carry
provider-controlled opaque state (signatures, encrypted content). If Kalash drops or rewrites it,
multi-turn reasoning breaks in ways that are hard to attribute. The normalized format must therefore
carry opaque payloads verbatim.

**Model output is untrusted input** (T-H). The gateway validates structure; it does not trust the
provider to return well-formed tool calls, sane token counts, or honest stop reasons.

---

## 2. Normalized message format

```python
class Role(StrEnum):
    USER = "user"; ASSISTANT = "assistant"; SYSTEM = "system"

@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    blocks: tuple[ContentBlock, ...]
    seq: int                            # monotonic per session (I-021)
```

### 2.1 Content blocks

| Block | Direction | Fields |
|---|---|---|
| `TextBlock` | both | `text`, `citations: tuple[Citation, ...]` |
| `ThinkingBlock` | out | `text \| None`, `opaque: OpaquePayload`, `redacted: bool` |
| `ToolUseBlock` | out | `call_id`, `provider_call_id`, `name`, `args: dict`, `complete: bool` |
| `ToolResultBlock` | in | `call_id`, `envelope: ToolEnvelope`, `is_error: bool` |
| `ImageBlock` | in | `data \| url`, `media_type`, `detail` |
| `DocumentBlock` | in | `data \| url`, `media_type`, `title`, `extracted_text \| None` |
| `MemoryBlock` | in | rendered `<memory>` content, `trust`, `record_ids` — a distinct type so I-020 framing cannot be lost by string concatenation |
| `OpaqueBlock` | both | `provider`, `kind`, `payload` — anything a provider emits that we do not model |

`MemoryBlock` and `OpaqueBlock` are the two non-obvious ones. `MemoryBlock` exists so that memory
injection is structurally distinguishable from user text at every layer, which is what lets I-020
framing be enforced by the renderer rather than remembered by a caller. `OpaqueBlock` exists so an
unrecognized block type from a provider is preserved rather than dropped — dropping it silently corrupts
conversation state.

### 2.2 Opaque payloads

```python
@dataclass(frozen=True, slots=True)
class OpaquePayload:
    provider: str          # payload is only valid for this provider
    model_family: str      # and only this family
    data: Mapping[str, Any]
```

Rules:

- Stored verbatim, never rewritten or normalized.
- Replayed verbatim on subsequent turns to the **same** provider and family.
- **Dropped on provider or family switch.** An Anthropic thinking signature is meaningless to OpenAI and
  including it is an error, not a compatibility problem. When a fallback changes provider, opaque
  payloads are stripped and the switch is recorded in the turn metadata (I-037).

That last rule is the reason model fallback is a turn-boundary operation (§7): opaque state cannot cross
a provider boundary mid-response.

### 2.3 Tool call identity

Two IDs per call, deliberately:

- `call_id` — Kalash's own ULID. Stable, used everywhere internally: persistence, audit, hooks,
  permissions, correlation.
- `provider_call_id` — the provider's string. Used only on the wire when returning results.

Providers differ on ID format and some do not supply one at all. Returning a result keyed by Kalash's
ID would fail on providers that match on their own; keying everything by the provider's ID would make
internal records unstable across a fallback. Keeping both, with a mapping in the turn record, avoids
both problems.

When a provider omits an ID, the adapter synthesizes one deterministically from position and name, and
records that it was synthesized.

---

## 3. Streaming protocol

One event stream, regardless of provider transport (SSE, chunked JSON, websocket).

```python
class StreamEvent: ...

MessageStart(model_id, provider, request_id)
BlockStart(index, block_type, partial: ContentBlock)
BlockDelta(index, delta: TextDelta | ArgsDelta | ThinkingDelta)
BlockStop(index, block: ContentBlock)              # block is now complete
UsageUpdate(usage: Usage)                          # may arrive multiple times
MessageStop(stop_reason: StopReason, usage: Usage)
StreamError(error: ModelError, partial: PartialResponse)
```

### 3.1 Stop reasons

| Normalized | Meaning |
|---|---|
| `END_TURN` | Model finished; no tool calls pending |
| `TOOL_USE` | Model finished and requests tool execution |
| `MAX_TOKENS` | Output limit hit — response is **truncated** |
| `STOP_SEQUENCE` | A configured stop sequence matched |
| `CONTENT_FILTER` | Provider refused or filtered |
| `PAUSE_TURN` | Provider paused a long-running turn, expects continuation |
| `UNKNOWN` | Provider sent something unrecognized — treated as an error, not as `END_TURN` |

`MAX_TOKENS` is not a normal completion. If it lands while a `ToolUseBlock` is incomplete, the arguments
are truncated JSON and that call is unexecutable (§5.3). Treating `MAX_TOKENS` as a successful stop is a
correctness bug: it means executing a tool call whose arguments were cut in half.

`UNKNOWN` maps to an error deliberately. An unrecognized stop reason means the gateway does not know
whether the turn completed, and guessing "it finished" is the unsafe guess.

### 3.2 The accumulator

The gateway maintains a `PartialResponse` while streaming: blocks completed so far, the block currently
open, usage as reported. On `StreamError` it is attached to the error so the caller can make an informed
decision (§5) rather than only knowing that something failed.

A block is `complete` only after its `BlockStop`. For `ToolUseBlock`, the accumulated argument JSON is
parsed and schema-validated at `BlockStop`; a parse failure marks the block invalid rather than
propagating a malformed `args` dict.

---

## 4. Model capabilities

### 4.1 The matrix

```python
@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    model_id: str
    provider: str
    model_family: str

    context_window: int
    max_output_tokens: int
    default_max_output: int

    tool_use: bool
    parallel_tool_use: bool
    tool_choice_forced: bool
    streaming: bool
    vision: bool
    documents: bool
    structured_output: bool
    json_schema_strict: bool
    reasoning: ReasoningSupport        # NONE | IMPLICIT | BUDGETED | EFFORT_LEVEL
    prompt_caching: CachingStyle       # NONE | AUTOMATIC_PREFIX | EXPLICIT_BREAKPOINT
    system_prompt: SystemPromptStyle   # TOP_LEVEL_PARAM | SYSTEM_MESSAGE | DEVELOPER_MESSAGE | NONE
    stop_sequences: bool
    temperature: bool                  # some reasoning models reject it

    pricing: Pricing | None
    knowledge_cutoff: date | None
```

### 4.2 Discovery is static, not probed

Capabilities come from a versioned registry file shipped with Kalash
(`kalash/models/registry/*.toml`), overridable by user config.

Runtime probing is rejected: it costs money on every startup, it is unreliable (a 400 tells you
something was wrong, not what), and it cannot discover a context window at all. A stale registry entry
is a better failure mode than a slow, expensive, partially-wrong probe.

For OpenAI-compatible endpoints, self-hosted models, and Ollama, capabilities are declared in config
with conservative defaults — `tool_use: false`, small context window — because an incorrect optimistic
default produces confusing mid-session failures while a conservative one produces a clear startup
message.

```toml
[models."my-local/qwen3-coder"]
provider = "openai_compatible"
base_url = "http://localhost:11434/v1"
context_window = 262144
max_output_tokens = 16384
tool_use = true
parallel_tool_use = false
system_prompt = "system_message"
```

### 4.3 Capability mismatch

Resolved before the request, never discovered from a 400.

| Situation | Behaviour |
|---|---|
| Tools requested, `tool_use: false` | Refuse at assembly. Clear error naming the model. No silent degradation to a text-only agent |
| Parallel calls requested, unsupported | Serialize automatically; note in turn metadata |
| Image in context, `vision: false` | Replace with a text placeholder describing what was omitted; warn once per session |
| Document in context, `documents: false` | Substitute `extracted_text` if available, else placeholder |
| `temperature` set, unsupported | Drop the parameter, log at DEBUG |
| Requested output tokens > `max_output_tokens` | Clamp, warn |
| Assembled context > `context_window` | Not the gateway's problem to solve silently — raise `KALASH_MODEL_CONTEXT_EXCEEDED` to the caller, which compacts and retries. See [`context-budget.md`](./context-budget.md) |

The last row matters: the gateway does not truncate context on its own. Truncation policy belongs to
the context budget allocator, which knows what is safe to drop. A gateway that quietly trimmed the
oldest messages could discard the user's original request.

---

## 5. Errors, retries, and partial streams

### 5.1 Error classification

Every provider error maps to one class. The class determines the response.

| Class | Examples | Retry same model | Fallback |
|---|---|---|---|
| `TRANSIENT_NETWORK` | connection reset, DNS failure, TLS handshake | yes, backoff | after N |
| `TIMEOUT_CONNECT` | no response headers | yes, backoff | after N |
| `TIMEOUT_READ` | stream stalled past idle timeout | yes, backoff | after N |
| `RATE_LIMIT` | 429 | yes, honour `Retry-After` | if wait > threshold |
| `OVERLOADED` | 529, 503 | yes, backoff + jitter | after N |
| `SERVER_ERROR` | 500, 502 | yes, backoff | after N |
| `AUTH_FAILED` | 401, 403 | **no** | yes, immediately |
| `QUOTA_EXHAUSTED` | billing/credit exhausted | **no** | yes, immediately |
| `MODEL_NOT_FOUND` | 404 on model id | **no** | yes, immediately |
| `CONTEXT_EXCEEDED` | input too large | **no** | no — caller compacts |
| `INVALID_REQUEST` | 400 on malformed request | **no** | no — our bug, fail loudly |
| `CONTENT_FILTER` | provider refusal | **no** | no — surface to user |
| `MALFORMED_RESPONSE` | unparseable stream, bad JSON, `UNKNOWN` stop | once | after that |
| `STREAM_INTERRUPTED` | connection dropped mid-stream | §5.3 | §5.3 |
| `CANCELLED` | user interrupt | **no** | **no** |

Three deliberate choices in this table:

`AUTH_FAILED` never retries. Retrying a rejected credential wastes time and, on some providers,
contributes to lockout. It also invalidates the cached credential so a rotated key is picked up
(see [`security.md`](./security.md) §3.4).

`INVALID_REQUEST` never falls back. A 400 means the gateway built a bad request. Falling back would
send the same bad request elsewhere and convert a clear bug into a confusing multi-provider failure.
Fail loudly with the request digest.

`CONTEXT_EXCEEDED` never falls back. Falling back to a model with a *smaller* window because the
context was too large for a bigger one is nonsensical, and falling back to a larger one hides a
compaction bug. The caller compacts and retries.

### 5.2 Retry policy

```
attempt 1  immediate
attempt 2  1s   ± jitter
attempt 3  4s   ± jitter
attempt 4  12s  ± jitter
max 4 attempts per model, total retry budget 60s per turn
```

Full jitter (`random() * base`), not fixed — synchronized retries across concurrent subagents produce a
thundering herd against the same rate limit. `Retry-After` always wins over computed backoff. The retry
budget is per turn, so a turn cannot spend minutes retrying while the user waits with no output.

Retries are counted against the turn's wallclock budget (I-026) so retry storms cannot extend a run
past its ceiling.

### 5.3 Partial streams — the explicit answer

The review asked: *should a partially completed model response ever be retried on another model?*

> **No. A partial response is never continued, merged, or completed by a different model, and never by a
> second call to the same model.** A partial is either discarded and the turn re-requested from its
> starting state, or the turn fails. There is no path that stitches two responses together.

Continuation would produce duplicated tool calls (the model re-emits what it already emitted, which the
first stream may have already surfaced), inconsistent reasoning state across opaque payloads that are
not portable (§2.2), and a transcript that does not correspond to any single model invocation — making
I-037 replay a fiction.

Handling depends on what had been emitted:

| State at interruption | Handling |
|---|---|
| No blocks completed | Discard. Retry same model, unchanged request. Fully safe |
| Text blocks only, no tool calls | Discard the partial text. Retry same model. Safe — nothing was executed, and the user has seen streamed text that will be replaced (the UI marks it as retried rather than leaving stale text on screen) |
| One or more `ToolUseBlock` **complete**, stream then dropped before `MessageStop` | Discard. Do **not** execute. Retry same model |
| `ToolUseBlock` incomplete (truncated args) | Discard. Never executable |
| `MessageStop` received with `MAX_TOKENS` and an incomplete tool block | Not an interruption — a truncated turn. Complete blocks are kept, the incomplete block is dropped, and the turn continues with a note to the model that output was truncated |

**Tool calls execute only after `MessageStop` with a stop reason of `TOOL_USE` or `END_TURN`.** This is
the rule that makes retry safe: if nothing executed, re-requesting has no side effects.

**One exception, narrowly scoped.** For latency, a tool whose `side_effect` is `none` or `read` may
execute speculatively at `BlockStop`, before `MessageStop`. If the stream then fails, its results are
discarded and a retry may re-execute it. This is safe precisely because such tools are idempotent and
side-effect-free. Tools with `side_effect` of `write` or `exec` **never** execute before `MessageStop`.

Retry attempts are recorded in the turn record, including discarded partials by digest, so an
investigation can see that a retry happened without the transcript containing phantom content.

---

## 6. Timeouts and cancellation

| Timeout | Default | Behaviour on expiry |
|---|---|---|
| Connect | 10s | `TIMEOUT_CONNECT` |
| Time to first byte | 60s | `TIMEOUT_READ` — long for reasoning models that think before emitting |
| Inter-chunk idle | 90s | `TIMEOUT_READ`. This is the one that matters: a stalled stream with an open connection is invisible to a total-duration timeout until it is far too late |
| Total request | 900s | `TIMEOUT_READ` |

Idle timeout is measured between chunks, reset on every byte. Reasoning models legitimately produce
long silences, which is why the first-byte timeout is separate and generous.

Cancellation (I-029): the request task is cancelled, the connection closed rather than drained, the
partial discarded, and `CANCELLED` returned without retry or fallback. Tokens consumed before
cancellation are still recorded — the provider bills for them, so the accounting must reflect it.

---

## 7. Fallback

### 7.1 Configuration

```toml
[model]
primary  = "anthropic/claude-sonnet-4-5"
fallback = ["openai/gpt-5.2", "ollama/qwen3-coder:30b"]

[model.fallback_policy]
on_auth_failure     = true
on_quota_exhausted  = true
on_rate_limit_after = "30s"    # fall back rather than wait longer than this
on_transient_after  = 3        # attempts
require_capabilities = true    # skip candidates lacking capabilities this turn needs
notify_user         = true
```

### 7.2 The algorithm

```
for candidate in [primary, *fallback]:
    if not capability_compatible(candidate, turn_requirements):   continue   # e.g. needs tools
    if context_does_not_fit(candidate):
        if can_compact():  compact()                                        # then re-check
        else:              continue
    strip_opaque_payloads_if_provider_differs(candidate)
    result = attempt(candidate)            # includes §5.2 retries
    if result.ok:                          return result
    if not result.error.fallbackable:      raise result.error               # §5.1
    record_fallback(candidate, result.error)
raise AllModelsFailed(attempts)
```

### 7.3 Fallback is a turn-boundary operation

**Fallback happens only at the start of a model request, never mid-stream.** This follows directly from
§5.3 and §2.2: a partial cannot be continued, and opaque reasoning state cannot cross a provider
boundary. When a stream fails on the primary, the sequence is: discard partial → retry same model per
§5.2 → if exhausted, discard and issue a *fresh* request to the fallback with the same input state.

### 7.4 Consequences worth stating

**Context must be re-fit.** A fallback model frequently has a different context window. The assembled
context is re-measured against the candidate before the request. A 200k-token context does not fit a
32k local model, so either compaction runs or the candidate is skipped.

**Capabilities must be re-checked.** Falling back from a tool-capable model to one without tools would
turn a working agent into one that cannot act. `require_capabilities` skips such candidates rather
than degrading. This is why `on_auth_failure` fallback to a local model is often the wrong
configuration and worth warning about at config load.

**Model switches are visible.** The user is told (`notify_user`), the turn record captures it (I-037),
and the audit log records it. A user debugging why quality dropped mid-session needs to know the model
changed, and silently swapping models is a support nightmare.

**Cost profile changes.** The fallback may be more expensive than the primary. Budget ceilings (I-026)
are enforced against actual cost, so a fallback cannot exceed a limit the primary was within.

---

## 8. Usage and cost

### 8.1 Normalized usage

```python
@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    source: UsageSource = UsageSource.PROVIDER_REPORTED   # or ESTIMATED
    provider_raw: Mapping[str, Any] = field(default_factory=dict)
```

Normalization is not trivial. Providers differ on whether cached tokens are included in
`input_tokens` or reported separately, and whether reasoning tokens are counted in output. Each adapter
must normalize to this contract explicitly:

- `input_tokens` **excludes** cache reads and writes; those are separate fields
- `output_tokens` **includes** reasoning tokens; `reasoning_tokens` is a subset, reported for cost
  calculation where reasoning is priced differently

Getting this wrong produces cost figures that are wrong by a large factor and are only noticed when
compared against a provider invoice. A per-adapter conformance test asserts the normalization against
recorded fixtures.

`ESTIMATED` is used when a provider omits usage — some OpenAI-compatible endpoints do. Estimates come
from a tokenizer where one is available for the model family, and from a character heuristic otherwise.
Estimated usage is always marked, and never presented as exact.

### 8.2 Cost

```python
@dataclass(frozen=True, slots=True)
class Pricing:
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_per_mtok: Decimal | None
    cache_write_per_mtok: Decimal | None
    reasoning_per_mtok: Decimal | None
    currency: str = "USD"
    pricing_version: str = ""      # registry version this came from
    effective_from: date | None = None
```

`Decimal`, never `float` — floating-point accumulation over thousands of calls drifts, and cost is
compared against budget ceilings that must be exact.

Pricing lives in the versioned registry and the version used is recorded per turn. Costs of historical
sessions therefore remain explainable even after prices change, which matters when a user asks why last
month's usage totals differ from this month's for the same work.

**Missing pricing** — a self-hosted or custom endpoint — records `cost: null` rather than zero. Zero
would silently under-report and make cost ceilings ineffective; null makes the gap visible.
`kalash doctor` reports models lacking pricing.

### 8.3 Attribution

Usage attaches to the turn, the agent run, and the session. Subagent usage rolls up to the parent for
budget enforcement while remaining separable for reporting, so `kalash session show` can answer both
"what did this session cost" and "which subagent spent it."

---

## 9. Prompt caching

`CachingStyle` determines what the adapter does with cache hints:

| Style | Adapter behaviour |
|---|---|
| `NONE` | Hints ignored |
| `AUTOMATIC_PREFIX` | Hints ignored; benefit comes from stable prefix ordering, which the assembler already guarantees |
| `EXPLICIT_BREAKPOINT` | Hints translated into provider cache-control markers |

The context assembler emits breakpoint hints at the boundaries defined in
[`context-budget.md`](./context-budget.md). The gateway translates or discards them; the assembler does
not need to know which provider is in use.

Cache effectiveness is observable: `cache_read_tokens` versus `input_tokens` per turn is logged, and a
sustained low hit rate on a caching-capable model indicates a prefix-stability regression. That metric
is what makes the snapshot tests on assembly order meaningful rather than theoretical — a regression
shows up as a cost increase.

---

## 10. Structured output

Where `structured_output` is supported, the gateway accepts a Pydantic model and handles provider
translation (JSON schema mode, tool-based coercion, or grammar constraints).

Used internally for memory extraction, compaction summaries, and reranking — never for the main agent
loop, where the model must be free to produce prose and tool calls together.

With `json_schema_strict: false`, the response is validated against the schema and, on failure, retried
once with the validation error appended. A second failure raises rather than returning partially valid
data. Providers without any structured-output support fall back to prompt-and-parse with the same
validate-retry-fail sequence.

---

## 11. Provider adapters

```python
class ModelProvider(Protocol):
    name: str
    async def stream(self, request: NormalizedRequest) -> AsyncIterator[StreamEvent]: ...
    async def count_tokens(self, request: NormalizedRequest) -> int | None: ...
    def capabilities(self, model_id: str) -> ModelCapabilities: ...
    async def health(self) -> ProviderHealth: ...
```

Adapter responsibilities, in both directions:

- Translate `NormalizedRequest` into the provider's wire format, including `SystemPromptStyle`
- Translate the provider's stream into `StreamEvent`s
- Preserve opaque payloads verbatim
- Normalize usage per §8.1
- Map errors to the §5.1 classes
- Route all network I/O through `core.egress` (I-003)

Adapters must **not**: retry (the gateway owns retry policy), decide fallback, log payloads, or touch
the database. Keeping retry in one place is what makes the retry budget and jitter behaviour uniform
across providers.

### Conformance suite

One shared suite every adapter passes, run against recorded fixtures in CI and live providers behind an
opt-in marker:

- Round-trip: text, tool calls, reasoning blocks, images, documents
- Opaque payload preservation across a multi-turn exchange
- Every stop reason, including `MAX_TOKENS` mid-tool-block
- Usage normalization against known-correct fixtures
- Every §5.1 error class produced and correctly classified
- Stream interruption at each of the §5.3 states
- Cancellation mid-stream leaves no open connection
- Malformed responses: truncated JSON, unknown block types, missing IDs, invalid tool args

---

## 12. Testing

| Concern | Approach |
|---|---|
| Adapters | Recorded fixtures. **No live calls in the default test run** — tests must be free, fast, and offline |
| Fallback algorithm | Fake providers scripted to fail with each error class; assert the decision matrix |
| Partial streams | Fixtures truncated at every byte boundary of a real stream; assert no tool execution from an incomplete stream |
| Usage/cost | Fixture-based, `Decimal` exactness, missing-pricing null propagation |
| Hostile provider | Adversarial fake per T-H: malformed calls, oversized responses, injected instructions in text, absurd usage numbers |
| Cache effectiveness | Snapshot assembly order; assert breakpoint positions are stable |

---

*See also: [`context-budget.md`](./context-budget.md) for assembly, budgeting, and compaction;
[`observability.md`](./observability.md) for the error taxonomy and trace model;
[`invariants.md`](./invariants.md) I-003, I-026, I-029, I-037;
[`threat-model.md`](./threat-model.md) T-H.*
