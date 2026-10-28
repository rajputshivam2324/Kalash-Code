"""OpenAI Responses API wire format (OpenAI, xAI): ``POST {base_url}/responses``.

Requests are stateless (``store: false``): the whole history is resent every turn. The model's own
output items (reasoning with ``encrypted_content``, ``phase``-tagged messages, function calls) are
kept verbatim on ``Message.replay`` and sent back unchanged, but only to the route that produced
them. Any other route rebuilds the turn from its text and tool-call blocks.

Facts this adapter relies on (OpenAI and xAI developer docs, checked 2026-10-23):

- The stream is typed by the JSON ``type`` field. ``response.output_item.done`` carries the final
  item; the ``.added`` copy of a reasoning item may still lack ``encrypted_content``.
- Usage and the final status arrive only on ``response.completed``/``incomplete``/``failed``.
- A mid-stream ``error`` event is documented flat (``code``/``message``) but observed nested
  (``{"error": {"type", "code", "message"}}``); both are accepted.
- ``prompt_cache_key`` is limited to 64 characters (longer values are rejected with HTTP 400).
- With ``store: false`` a reasoning item can only be replayed with its ``encrypted_content``, and a
  reasoning item must be followed by another output item; anything else is rejected with HTTP 400.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from typing import Any, cast

import httpx

from kalash.llm.catalog import ModelInfo
from kalash.llm.errors import OVERFLOW_PATTERN, ErrorCode, LLMError
from kalash.llm.http import SSEEvent, int_or_none, make_client, parse_json, post_sse
from kalash.llm.types import (
    Block,
    Done,
    FinishReason,
    ImageBlock,
    Message,
    ReasoningBlock,
    ReasoningDelta,
    Request,
    StreamEvent,
    TextBlock,
    TextDelta,
    ToolCallBlock,
    ToolCallStarted,
    ToolSpec,
    Usage,
)

REPLAY_KIND = "openai-responses"
MAX_CACHE_KEY_CHARS = 64

_TERMINAL = {
    "response.completed": "completed",
    "response.incomplete": "incomplete",
    "response.failed": "failed",
}

_AUTH_CODES = frozenset(
    {
        "invalid_api_key",
        "invalid_organization",
        "authentication_error",
        "permission_error",
        "permission_denied",
    }
)
# HTTP 400 for an organization that may not request reasoning summaries
# ("... must be verified to generate reasoning summaries", param ``reasoning.summary``).
_SUMMARY_REJECTED = re.compile(r"summar", re.IGNORECASE)
_QUOTA_MARKERS = ("quota", "spend_limit", "usage_limit", "credit", "billing")
_INVALID_CODES = frozenset(
    {"data_residency_mismatch", "unsupported_image_media_type", "empty_image_file"}
)


class OpenAIResponsesAdapter:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        api_key: str | None,
        context_window: int,
        info: ModelInfo | None = None,
        extra_headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.context_window = context_window
        self.info = info
        self.extra_headers = dict(extra_headers or {})
        self._client = client or make_client()
        self._origin = f"{provider}/{model}"
        self._summary_rejected = False

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- request -----------------------------------------------------------------------

    def build_body(self, request: Request) -> dict[str, Any]:
        info = self.info
        body: dict[str, Any] = {
            "model": request.model,
            "input": self.serialize(request),
            "stream": True,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if request.system:
            body["instructions"] = request.system
        if request.tools:
            strict = info.extra.get("strict_tools") if info is not None else None
            body["tools"] = [
                _tool(spec, strict if isinstance(strict, bool) else None) for spec in request.tools
            ]
            body["parallel_tool_calls"] = True
        if request.max_tokens is not None:
            body["max_output_tokens"] = request.max_tokens
        reasoning = self._reasoning(request.reasoning_effort)
        if reasoning is not None:
            body["reasoning"] = reasoning
        thinking = reasoning is not None and reasoning["effort"] != "none"
        sampling = info is None or info.sampling
        if request.temperature is not None and sampling and not thinking:
            body["temperature"] = request.temperature
        if request.session_id:
            body["prompt_cache_key"] = request.session_id[:MAX_CACHE_KEY_CHARS]
        return body

    def _reasoning(self, effort: str | None) -> dict[str, Any] | None:
        if effort is None:
            return None
        info = self.info
        if effort in ("off", "none"):
            # Models that cannot switch reasoning off reject "none" with HTTP 400: omit instead.
            if info is not None and "none" in info.efforts:
                return {"effort": "none"}
            return None
        reasoning: dict[str, Any] = {"effort": effort}
        summary = info.extra.get("reasoning_summary", "auto") if info is not None else "auto"
        if isinstance(summary, str) and summary and not self._summary_rejected:
            reasoning["summary"] = summary
        return reasoning

    def serialize(self, request: Request) -> list[dict[str, Any]]:
        """The ``input`` items. Every function call is followed by exactly one result."""
        items: list[dict[str, Any]] = []
        pending: list[str] = []
        for message in request.messages:
            if message.role == "tool":
                call_id = message.tool_call_id
                if call_id is None or call_id not in pending:
                    raise self._invalid(
                        f"tool result {call_id!r} has no matching function call before it "
                        f"(or it repeats an earlier result)"
                    )
                pending.remove(call_id)
                items.append(_tool_output(call_id, message))
                continue
            if pending:
                raise self._invalid(
                    f"function call(s) {', '.join(pending)} have no result before the next message"
                )
            if message.role == "user":
                parts = _input_parts(message.content)
                if parts:
                    items.append({"role": "user", "content": parts})
                continue
            for item in self._assistant(message):
                if item.get("type") == "function_call":
                    call_id = item.get("call_id")
                    if not isinstance(call_id, str) or not call_id or call_id in pending:
                        raise self._invalid(
                            f"function call with a missing or repeated id: {call_id!r}"
                        )
                    pending.append(call_id)
                items.append(item)
        if pending:
            raise self._invalid(f"function call(s) {', '.join(pending)} have no result")
        return items

    def _assistant(self, message: Message) -> list[dict[str, Any]]:
        replay = message.replay
        if (
            isinstance(replay, dict)
            and replay.get("kind") == REPLAY_KIND
            and message.origin == self._origin
            and isinstance(replay.get("items"), list)
        ):
            return _sendable(replay["items"])
        items: list[dict[str, Any]] = []
        text: list[str] = []

        def flush() -> None:
            if text:
                items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "".join(text)}],
                    }
                )
                text.clear()

        for block in message.content:
            if isinstance(block, TextBlock):
                if block.text:
                    text.append(block.text)
            elif isinstance(block, ToolCallBlock):
                flush()
                items.append(
                    {
                        "type": "function_call",
                        "call_id": block.id,
                        "name": block.name,
                        "arguments": _object_arguments(block.arguments),
                    }
                )
        flush()
        return items

    def _invalid(self, message: str) -> LLMError:
        return LLMError(
            f"inconsistent conversation history: {message}",
            ErrorCode.INVALID_REQUEST,
            provider=self.provider,
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)
        return headers

    # -- response ----------------------------------------------------------------------

    async def stream(self, request: Request) -> AsyncIterator[StreamEvent]:
        """One attempt: a stream of events that ends with ``Done``, or an ``LLMError``.

        The single exception is a capability downgrade, not a retry: if the API answers HTTP 400
        because the account may not request reasoning summaries (OpenAI requires a verified
        organization), the request is sent once more without ``reasoning.summary`` and later
        requests on this adapter leave it out. That 400 arrives before any generation.
        """
        try:
            async for event in self._stream_once(request):
                yield event
            return
        except LLMError as error:
            if not self._rejects_summary(error, request):
                raise
        self._summary_rejected = True
        async for event in self._stream_once(request):
            yield event

    def _rejects_summary(self, error: LLMError, request: Request) -> bool:
        if error.status != 400 or not _SUMMARY_REJECTED.search(error.message):
            return False
        reasoning = self._reasoning(request.reasoning_effort)
        return reasoning is not None and "summary" in reasoning

    async def _stream_once(self, request: Request) -> AsyncIterator[StreamEvent]:
        url = f"{self.base_url}/responses"
        body = self.build_body(request)
        turn = _Turn(self.provider, self._origin)
        terminal: tuple[str, dict[str, Any]] | None = None
        events = cast(
            "AsyncGenerator[SSEEvent, None]",
            post_sse(self._client, url, body, self._headers(), self.provider),
        )
        try:
            async for sse in events:
                data = sse.data.strip()
                if not data:
                    continue
                if data == "[DONE]":
                    break
                payload = parse_json(data, self.provider)
                if not isinstance(payload, dict):
                    continue
                kind = payload.get("type")
                if not isinstance(kind, str):
                    kind = sse.event or ""
                if kind in _TERMINAL:
                    terminal = (kind, payload)
                    break
                for event in turn.feed(kind, payload):
                    yield event
        finally:
            await events.aclose()
        if terminal is None:
            raise LLMError(
                "stream ended before a terminal response event",
                ErrorCode.TRANSPORT,
                provider=self.provider,
            )
        yield turn.finish(*terminal)


@dataclass
class _Slot:
    """Everything streamed for one output item (keyed by ``output_index``)."""

    added: dict[str, Any] | None = None
    done: dict[str, Any] | None = None
    text: dict[int, list[str]] = field(default_factory=dict)
    refusal: dict[int, list[str]] = field(default_factory=dict)
    arguments: list[str] = field(default_factory=list)
    arguments_done: str | None = None
    announced: bool = False


class _Turn:
    """Accumulates one streamed response; ``finish`` turns it into the authoritative ``Done``."""

    def __init__(self, provider: str, origin: str) -> None:
        self.provider = provider
        self.origin = origin
        self.slots: dict[int, _Slot] = {}
        self.by_item_id: dict[str, int] = {}
        self.started = 0

    # -- events ------------------------------------------------------------------------

    def feed(self, kind: str, event: dict[str, Any]) -> list[StreamEvent]:
        out: list[StreamEvent] = []
        if kind == "error":
            raise self._stream_error(event)
        if kind in ("response.output_item.added", "response.output_item.done"):
            item = event.get("item")
            if not isinstance(item, dict):
                return out
            slot = self._slot(event, item)
            if kind.endswith(".added"):
                slot.added = item
            else:
                slot.done = item
            out.extend(self._announce(slot))
        elif kind == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                slot = self._slot(event)
                slot.text.setdefault(_int(event.get("content_index")), []).append(delta)
                out.append(TextDelta(delta))
        elif kind == "response.refusal.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                slot = self._slot(event)
                slot.refusal.setdefault(_int(event.get("content_index")), []).append(delta)
                out.append(TextDelta(delta))
        elif kind == "response.function_call_arguments.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                self._slot(event).arguments.append(delta)
        elif kind == "response.function_call_arguments.done":
            arguments = event.get("arguments")
            if isinstance(arguments, str):
                self._slot(event).arguments_done = arguments
        elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                out.append(ReasoningDelta(delta))
        elif kind == "response.reasoning_summary_part.added" and _int(event.get("summary_index")):
            out.append(ReasoningDelta("\n\n"))
        return out  # everything else (lifecycle, content_part.*, hosted tools, ...) is ignored

    def _slot(self, event: dict[str, Any], item: dict[str, Any] | None = None) -> _Slot:
        index = event.get("output_index")
        if not isinstance(index, int) or isinstance(index, bool):
            item_id = event.get("item_id") or (item or {}).get("id")
            index = self.by_item_id.get(item_id) if isinstance(item_id, str) else None
            if index is None:
                index = len(self.slots)
        if item is not None and isinstance(item.get("id"), str):
            self.by_item_id[item["id"]] = index
        return self.slots.setdefault(index, _Slot())

    def _announce(self, slot: _Slot) -> list[StreamEvent]:
        item = slot.done or slot.added or {}
        call_id = item.get("call_id")
        name = item.get("name")
        if (
            slot.announced
            or item.get("type") != "function_call"
            or not isinstance(call_id, str)
            or not call_id
            or not isinstance(name, str)
            or not name
        ):
            return []
        slot.announced = True
        started = ToolCallStarted(self.started, call_id, name)
        self.started += 1
        return [started]

    # -- final message -----------------------------------------------------------------

    def finish(self, kind: str, event: dict[str, Any]) -> Done:
        response = event.get("response")
        response = response if isinstance(response, dict) else {}
        status = _TERMINAL[kind]
        if status == "completed" and response.get("status") in ("incomplete", "failed"):
            status = str(response["status"])
        if status == "failed":
            raise self._failure(response.get("error"))
        usage = _usage(response.get("usage"))
        items = self._final_items(response, status)
        blocks, kept, refused = self._assemble(items, status)
        message = Message(
            "assistant",
            blocks,
            origin=self.origin,
            replay={"kind": REPLAY_KIND, "items": kept},
        )
        finish: FinishReason
        if status == "incomplete":
            details = response.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            finish = "content_filter" if reason == "content_filter" else "max_tokens"
        elif any(isinstance(b, ToolCallBlock) for b in blocks):
            finish = "tool_calls"
        elif refused:
            finish = "refusal"
        elif any(isinstance(b, TextBlock) and b.text for b in blocks):
            finish = "stop"
        else:
            raise LLMError(
                "model returned an empty response", ErrorCode.EMPTY_RESPONSE, provider=self.provider
            )
        return Done(message, finish, usage)

    def _final_items(self, response: dict[str, Any], status: str) -> list[dict[str, Any]]:
        """Final output items in order: ``output_item.done`` first, then the terminal response.

        The terminal ``output`` fills items whose events were missed and supplies
        ``encrypted_content`` when a done item lacks it; as a last resort an item is rebuilt from
        its ``added`` copy and the streamed deltas.
        """
        output = response.get("output")
        terminal = output if isinstance(output, list) else []
        items: list[dict[str, Any]] = []
        for index in sorted(set(self.slots) | set(range(len(terminal)))):
            slot = self.slots.get(index) or _Slot()
            late = terminal[index] if index < len(terminal) else None
            late = late if isinstance(late, dict) else None
            item = slot.done
            if item is None:
                item = late or _synthesize(slot, status)
            elif (
                late is not None
                and item.get("type") == "reasoning"
                and not item.get("encrypted_content")
                and late.get("encrypted_content")
            ):
                item = late
            if item is None:
                continue
            if item.get("type") == "function_call":
                item = _with_arguments(item, slot)
            items.append(item)
        return items

    def _assemble(
        self, items: list[dict[str, Any]], status: str
    ) -> tuple[list[Block], list[dict[str, Any]], bool]:
        blocks: list[Block] = []
        kept: list[dict[str, Any]] = []
        seen: set[str] = set()
        refused = False
        for item in items:
            kind = item.get("type")
            if kind == "reasoning":
                signature = item.get("encrypted_content")
                extra = {"id": item["id"]} if isinstance(item.get("id"), str) else None
                blocks.append(
                    ReasoningBlock(
                        _reasoning_text(item),
                        signature if isinstance(signature, str) and signature else None,
                        extra=extra,
                    )
                )
            elif kind == "message":
                for text, is_refusal in _message_texts(item):
                    refused = refused or is_refusal
                    blocks.append(TextBlock(text))
            elif kind == "function_call":
                call_id = item.get("call_id") or item.get("id")
                name = item.get("name")
                if not isinstance(call_id, str) or not call_id or not isinstance(name, str):
                    if status == "incomplete":
                        continue  # cut off before the call was identified: nothing to answer
                    raise LLMError(
                        "function call without a name or call_id",
                        ErrorCode.MALFORMED_RESPONSE,
                        provider=self.provider,
                    )
                if call_id in seen:
                    raise LLMError(
                        "duplicate tool call ids in one response",
                        ErrorCode.MALFORMED_RESPONSE,
                        provider=self.provider,
                    )
                seen.add(call_id)
                arguments = item.get("arguments")
                extra = {"item_id": item["id"]} if isinstance(item.get("id"), str) else None
                blocks.append(
                    ToolCallBlock(
                        call_id, name, arguments if isinstance(arguments, str) else "", extra
                    )
                )
            kept.append(item)
        return blocks, kept, refused

    # -- errors ------------------------------------------------------------------------

    def _stream_error(self, event: dict[str, Any]) -> LLMError:
        nested = event.get("error")
        if isinstance(nested, dict):
            return self._api_error(nested.get("code"), nested.get("type"), nested.get("message"))
        if isinstance(nested, str):
            return self._api_error(event.get("code"), None, nested)
        return self._api_error(event.get("code"), None, event.get("message"))

    def _failure(self, error: Any) -> LLMError:
        if isinstance(error, dict):
            return self._api_error(error.get("code"), error.get("type"), error.get("message"))
        return self._api_error(None, None, "the response failed without an error object")

    def _api_error(self, code: Any, kind: Any, message: Any) -> LLMError:
        code_text = code if isinstance(code, str) and code else None
        kind_text = kind if isinstance(kind, str) and kind else None
        text = message if isinstance(message, str) and message else "the stream reported an error"
        if code_text:
            text = f"{text} (code: {code_text})"
        return LLMError(text, _classify(code_text, kind_text, text), provider=self.provider)


def _classify(code: str | None, kind: str | None, message: str) -> ErrorCode:
    """Map an in-stream error to a typed failure by its code, then its type, then its text."""
    for raw in (code, kind):
        if raw is None:
            continue
        value = raw.lower()
        if "context_length" in value:
            return ErrorCode.CONTEXT_OVERFLOW
        if any(marker in value for marker in _QUOTA_MARKERS):
            return ErrorCode.QUOTA
        if "rate_limit" in value or value == "slow_down":
            return ErrorCode.RATE_LIMIT
        if value in _AUTH_CODES:
            return ErrorCode.AUTH
        if value.startswith(("invalid_", "image_")) or "policy" in value or value in _INVALID_CODES:
            return ErrorCode.INVALID_REQUEST
    if OVERFLOW_PATTERN.search(message):
        return ErrorCode.CONTEXT_OVERFLOW
    return ErrorCode.SERVER


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _usage(raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    inputs = raw.get("input_tokens_details")
    outputs = raw.get("output_tokens_details")
    inputs = inputs if isinstance(inputs, dict) else {}
    outputs = outputs if isinstance(outputs, dict) else {}
    return Usage(
        input_tokens=int_or_none(raw.get("input_tokens")),
        output_tokens=int_or_none(raw.get("output_tokens")),
        cache_read_tokens=int_or_none(inputs.get("cached_tokens")) or 0,
        cache_write_tokens=int_or_none(inputs.get("cache_write_tokens")) or 0,
        reasoning_tokens=int_or_none(outputs.get("reasoning_tokens")) or 0,
    )


def _with_arguments(item: dict[str, Any], slot: _Slot) -> dict[str, Any]:
    """The item's own arguments win; otherwise the ``.done`` event, then the joined deltas."""
    arguments = item.get("arguments")
    if isinstance(arguments, str) and arguments:
        return item
    streamed = slot.arguments_done if slot.arguments_done is not None else "".join(slot.arguments)
    if streamed or not isinstance(arguments, str):
        return {**item, "arguments": streamed}
    return item


def _synthesize(slot: _Slot, status: str) -> dict[str, Any] | None:
    """Rebuild an item whose ``done`` event and terminal copy are both missing."""
    base = slot.added
    if base is None:
        return None
    final = "incomplete" if status == "incomplete" else "completed"
    if base.get("type") == "function_call":
        return _with_arguments({**base, "status": final}, slot)
    if base.get("type") == "message":
        parts: list[tuple[int, dict[str, Any]]] = [
            (i, {"type": "output_text", "text": "".join(v), "annotations": []})
            for i, v in slot.text.items()
        ]
        parts += [(i, {"type": "refusal", "refusal": "".join(v)}) for i, v in slot.refusal.items()]
        if parts:
            parts.sort(key=lambda part: part[0])
            return {**base, "content": [part for _, part in parts], "status": final}
    return None


def _texts(parts: Any) -> list[str]:
    if not isinstance(parts, list):
        return []
    return [
        p["text"]
        for p in parts
        if isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"]
    ]


def _reasoning_text(item: dict[str, Any]) -> str:
    """Summary parts joined as paragraphs; raw ``reasoning_text`` content if there is no summary."""
    return "\n\n".join(_texts(item.get("summary")) or _texts(item.get("content")))


def _message_texts(item: dict[str, Any]) -> list[tuple[str, bool]]:
    """(text, is_refusal) for each output_text/refusal part of a message item."""
    content = item.get("content")
    if isinstance(content, str):
        return [(content, False)] if content else []
    found: list[tuple[str, bool]] = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "output_text":
            text, is_refusal = part.get("text"), False
        elif part.get("type") == "refusal":
            text, is_refusal = part.get("refusal"), True
        else:
            continue
        if isinstance(text, str) and text:
            found.append((text, is_refusal))
    return found


# -- request helpers --------------------------------------------------------------------


def _tool(spec: ToolSpec, strict: bool | None) -> dict[str, Any]:
    """An internally tagged function tool. ``strict`` is only sent when the model info asks."""
    tool: dict[str, Any] = {
        "type": "function",
        "name": spec.name,
        "description": spec.description,
        "parameters": spec.parameters,
    }
    if strict is not None:
        tool["strict"] = strict
    return tool


def _input_parts(blocks: list[Block]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, TextBlock) and block.text:
            parts.append({"type": "input_text", "text": block.text})
        elif isinstance(block, ImageBlock):
            url = f"data:{block.media_type};base64,{block.data}"
            parts.append({"type": "input_image", "image_url": url})
    return parts


def _tool_output(call_id: str, message: Message) -> dict[str, Any]:
    parts = _input_parts(message.content)
    output: str | list[dict[str, Any]]
    if any(part["type"] == "input_image" for part in parts):
        output = parts
    else:
        output = "".join(part["text"] for part in parts) or "(no output)"
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def _object_arguments(arguments: Any) -> str:
    """Replay arguments verbatim when they are a JSON object; otherwise ``{}``."""
    if isinstance(arguments, str) and arguments.strip():
        try:
            value = json.loads(arguments)
        except ValueError:
            return "{}"
        if isinstance(value, dict):
            return arguments
    return "{}"


def _sendable(raw_items: list[Any]) -> list[dict[str, Any]]:
    """The stored output items as ``input`` items, unchanged except where the API would reject.

    Dropped: reasoning without ``encrypted_content`` (it cannot be restored with ``store:
    false``), cut-off reasoning, and reasoning not followed by another item. Function-call
    arguments that are not a JSON object (a truncated or malformed call) are sent as ``{}``.
    """
    items: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "reasoning" and (
            not item.get("encrypted_content") or item.get("status") == "incomplete"
        ):
            continue
        if kind == "function_call":
            arguments = _object_arguments(item.get("arguments"))
            if arguments != item.get("arguments"):
                item = {**item, "arguments": arguments}
        items.append(item)
    while items and items[-1].get("type") == "reasoning":
        items.pop()
    return items
