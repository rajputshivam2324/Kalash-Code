# Python SDK output contract

`KalashClient` supports managed sessions and single-shot calls. Session calls
serialize per-call routing/tool overrides and restore their defaults afterwards.
Closing a session cleans up its agent and provider resources.

```python
from kalash.sdk import KalashClient

async with KalashClient().session() as session:
    response = await session.complete("Explain the failing test")
    print(response.text, response.completion_status, response.usage)
    async for chunk in session.stream("Inspect the relevant source", include_events=True):
        if chunk.event:
            print(chunk.event["event"], chunk.event["run_id"])
        if chunk.text:
            print(chunk.text, end="")
        if chunk.done:
            print(chunk.completion_status, chunk.error)
```

Completion returns the actual model, session/run IDs, invocation tokens,
input/output/cache/reasoning usage, nullable cost, events, tool attempts,
verification receipts and termination/completion status. Tool attempts include
refused calls; inspect completion events to determine their outcome. A normally
stopped answer is `stopped_unverified` unless caller-owned checks establish success.
`usage_unknown=True` preserves partial observed usage without asserting zero for
missing counters. This information does not reconcile provider invoices.
Usage is collected from gateway totals independently of the bounded event list,
so a missing event projection cannot turn settled provider usage into zero.

Streaming yields text early. Public event chunks are opt-in so existing consumers
can keep text-first behavior. The final chunk includes run identity, token total,
verification, error and completion status; settled provider events carry the
detailed usage breakdown. Closing the generator cancels and joins its producer.
Completion events are capped at 2,000 and streaming queues at 128 chunks. Exhaustion
stops the run with an explicit incomplete-output error; consume streaming promptly.

CLI and SDK share runtime event data through `core.public_events`. Observational
events cannot authorize execution; permissions and required hooks use explicit
enforcement paths. Resource, tool and output bounds still apply.
