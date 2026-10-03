"""Bounded factual receipts carried across semantic compaction.

An exit code records a command outcome, not correctness or permission to retry.
The full transcript retains older receipts. This tail keeps at most 32 / 6k chars.
"""

from __future__ import annotations

import json
from typing import Any

from kalash.models.normalize import Message, ToolResultBlock

_START = "\n<runtime_receipts>\n"
_END = "\n</runtime_receipts>"


def split_receipts(summary: str) -> tuple[str, list[dict[str, Any]], int]:
    text, marker, tail = summary.partition(_START)
    if not marker or not tail.endswith(_END):
        return text, [], 0
    try:
        payload = json.loads(tail[: -len(_END)])
        records = payload["records"]
        omitted = payload["omitted"]
        if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
            return text, [], 0
        if not isinstance(omitted, int) or omitted < 0:
            return text, [], 0
        return text, records, omitted
    except (ValueError, KeyError, TypeError):
        return text, [], 0


def receipt_tail(messages: list[Message], previous: str) -> str:
    """Build from runtime fields only; never infer outcomes from assistant prose."""
    _, records, omitted = split_receipts(previous)
    seen = {record.get("call_id") for record in records}
    for message in messages:
        for block in message.content:
            if (
                isinstance(block, ToolResultBlock)
                and block.evidence
                and block.tool_use_id not in seen
            ):
                records.append({**block.evidence, "call_id": block.tool_use_id})
                seen.add(block.tool_use_id)
    if not records:
        return ""
    while True:
        payload = json.dumps({"omitted": omitted, "records": records}, ensure_ascii=False)
        if len(records) <= 32 and len(payload) <= 6_000:
            break
        records.pop(0)
        omitted += 1
    return _START + payload + _END
