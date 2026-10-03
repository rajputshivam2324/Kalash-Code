# Runtime map

## Main path

```text
CLI / TUI / SDK / scheduler
  → runtime.agent.build_agent
  → Agent.prepare (resume, optional MCP setup)
  → Agent.send (serialized conversation turn)
  → runtime.loop.AgentLoop
      → context.ContextAssembler
      → request.request → models.gateway → provider adapter
      → execution.ToolExecutor → toolhost.ToolHost → tools.registry
      → transcript.Transcript → storage.repositories.sessions
```

Each module owns one step. Clients render results and events; provider adapters
own wire formats; tools own local operations. Permission checks precede effects.

| Area | Files | Responsibility |
| --- | --- | --- |
| Assembly | `runtime/agent.py`, `bootstrap.py` | Provider, config, session, tools, setup and cleanup |
| Conversation | `runtime/loop.py` | Iteration and termination; no provider TPM policy |
| Requests | `runtime/request.py`, `models/gateway.py` | Run preflight, usage, cancellation, transient retries |
| Tools | `runtime/execution.py`, `toolhost.py`, `tools/registry.py` | Ordered batches, gates, capability checks and timeouts |
| Static prompt | `runtime/prompt.py` | Identity, workflow and build/plan behavior |
| Guidance | `runtime/instructions.py` | Hierarchical discovery, sources and scoped loading |
| Context | `runtime/context.py`, `history.py`, `compaction.py`, `evidence.py` | Stable prefix, token estimate, closed batches and summaries |
| Persistence | `runtime/transcript.py`, `serialize.py`, `storage/` | Intent recording, per-tool receipts and unknown-outcome recovery |
| Delegation | `runtime/delegation.py`, `tools/task.py`, `agents/loader.py` | Fresh child context, restricted tools and charged usage |
| Skills | `skills/loader.py`, `tools/skill.py`, `core/frontmatter.py` | Metadata, on-demand bodies and bounded references |
| Filesystem | `tools/fs/{read,write,edit,listing,common}.py` | File operations, unique edits, digests and path checks |
| Model selection | `models/catalog.py`, `resolve.py`, `auth_store.py` | Provider metadata and local selection; independent of TUI |
| Local memory | `memory/providers/local/{store,queries,records,schema}.py` | Transactions, scoped SQL, conversion and schema |
| Memory runtime | `memory/{service,router,session}.py`, `pipeline/` | Configuration, provider routing and bounded recall/capture |
| MCP | `mcp/{registry,load,manager,client,http,tools,auth}.py` | Configuration, lifecycle, transport and tool adaptation |
| Trust | `core/trust.py` | Hash pinning of executable project configuration |
| Terminal | `tui/{app,commands,events,formatting,messages}.py` | App lifecycle, commands, event rendering and widgets |
| Verification | `tests/`, `evals/benchmark_workspace.py` | Regression contracts and independent grading mechanics |

## Prompt ownership

1. Static system contract and selected mode are built once.
2. Sorted skill metadata is a catalog, not a collection of full bodies.
3. Root/ancestor guidance is injected once by the assembler, with user request
   priority and scope made explicit.
4. Environment facts follow stable instructions.
5. Compacted observations and recent conversation follow the system message.
6. Scoped guidance, skill bodies absent from retained history, and bounded notes
   are late user messages. They cannot grant runtime permissions.

Tool schemas use the API tools field; they are not repeated as prose in the
system prompt. Core schemas are immediate; selected plugin/MCP schemas append
after `tool_search` discovery and persist through compaction. Discovery is
filtered by capabilities, mode and network policy; it does not grant permissions.
Runtime budgets do not decide which instructions to omit.

The 12 bundled workflow skills load metadata first, then bodies and individual
resources. Optional artifact inspection runs as `python -m kalash.artifacts`
through the existing shell sandbox. See [capability support](CAPABILITIES.md).

## Child ownership

A child gets the delegated brief, selected file paths and role instructions.
It has a unique session, scratchpad, history and no persistent memory. Its tools,
capabilities, plan/build mode, network policy, workspace restriction and extension
setting cannot widen its parent's. The parent reserves tokens/cost and receives
actual usage even when the child fails. Children return bounded findings, not
full transcripts. A child never closes a provider owned by its parent.

Named definitions with a model override require standalone `kalash agents run`;
tool delegation inherits the active model. This prevents an implicit change of
provider or pricing inside a parent run.

## Memory ownership

Memory services are reused only for the same database path and memory config.
SQLite is the default authority. The router applies scope, expiry and confidence
before rank fusion. Write/update/forget transactions keep records and FTS in sync.
No unused vector, contradiction-learning or write-ledger layer sits in the path.
Old migration tables remain for database compatibility, not as active features.

## Limits

Token counts are conservative character estimates rather than model tokenizers.
A transcript provides interruption recovery, not full event replay or exactly-once
external effects. Trusted hooks/MCP are host extensions; they are disabled in the
hermetic evaluation profile. See the review for the proof still needed.

## Recovery and evidence

Tool intents are persisted before effects; each settled result is persisted before
another dependent write. Failed receipt storage stops the batch and joins read
siblings. Runtime SQLite paths own separate connections and migrations atomically
commit DDL with their receipts. Unknown effects remain unknown on recovery.

Built-in command/file receipts survive trimming and compaction separately from
model-written summaries. Ordinary observation text is scrubbed before deferral,
transcript storage and requests; event subscribers receive scrubbed copies. These
are tested boundaries, not a complete DLP or exactly-once effect guarantee. See
[the harness design and gates](HARNESS_DESIGN.md) for limits and priorities.
