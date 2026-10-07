# Headless output accounting

`kalash -p ... --output-format json` returns one JSON object. `stream-json`
returns ordered NDJSON envelopes with schema version `1.0`, a sequence number,
timestamp, session ID, event name and data. Diagnostics go to stderr.

`usage` contains the invocation's observed input, output, cache-read,
cache-write and reasoning counters. Cache reads are a subset of input, and
reasoning is a subset of output. On incomplete accounting, `usage_unknown` is
true and `source` is `observed_partial`; these counters are observations, not a
claim that the missing usage was zero. Otherwise `source` is `provider_reported`.
`cost_usd` is null when rates are unavailable or usage is incomplete.

`budget` describes cumulative session tokens and provider turns against the
actual configured ceilings. A null token ceiling means uncapped spending;
operational guards remain active. Invocation usage and cumulative session
budget usage intentionally have different scope.

Results retain `completion_status`; a normally stopped invocation can return
exit zero with `stopped_unverified`. Exit zero alone does not establish a
verified software artifact. Failure output retains observed accounting,
budget state, partial response and completion status. Budget stops exit 4;
cancellation exits 5.

NDJSON `tool.started`, `tool.denied` and `tool.completed` retain runtime call IDs.
Completion retains available error codes, remediation and zero-valued evidence.
`tool.output` carries the original source-bounded chunk, call ID and stream.
The CLI applies no extra 500-character slice. Tool acquisition/output bounds
still apply; this does not promise arbitrary-size complete process logs.

Runtime envelopes now include `run_id`; provider events expose logical
`attempt_id`, physical `dispatch_id`, fallback transitions and settled usage.
CLI and SDK use the same public event names/data. CLI `seq` orders all stdout
envelopes, including text/lifecycle output; `runtime_seq` preserves the source
event sequence. The pre-run `session.started` envelope can have a null run ID.

Results retain attempted models/fallback chain, file-operation metadata and
caller-owned `verification` receipts. `files_changed` describes successful
observed tool operations; it is not an authoritative filesystem diff and does
not include arbitrary writes hidden inside shell commands. Completion status
and independent verification remain the authority for artifact success.
