"""One sanitized runtime event projection for public consumers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kalash.core.privacy import scrub_metadata

if TYPE_CHECKING:
    from kalash.core.events import Event

SCHEMA_VERSION = "1.0"
_NAMES = {"tool.start": "tool.started", "tool.complete": "tool.completed"}


def public_event(event: Event) -> dict[str, Any]:
    data = scrub_metadata(event.data)
    if "tool_name" in data:
        data["name"] = data["tool_name"]
    return {
        "schema_version": SCHEMA_VERSION,
        "seq": event.sequence,
        "ts": event.timestamp,
        "session_id": event.session_id,
        "run_id": event.run_id,
        "event": _NAMES.get(event.type.value, event.type.value),
        "data": data,
    }
