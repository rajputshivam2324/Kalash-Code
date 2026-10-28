"""Native Gemini API adapter (``models/{model}:streamGenerateContent?alt=sse``).

Why native instead of the OpenAI-compatible endpoint: Google marks that endpoint beta, and Gemini
3/4 *require* ``thoughtSignature`` to be echoed on function calls (a missing one is an HTTP 400).
Wire facts below come from ai.google.dev (generate-content docs, API reference), read 2026-10-23.

Replay rules implemented here (see ``serialize``):

* A model turn is rebuilt from blocks. Signatures are only replayed to the route that produced
  them (``Message.origin``) and always on the *same part* they arrived on; parts that carry a
  signature are never merged with other parts.
* Thought summaries (``thought: true``) are never sent back; a signature that rode on one is moved
  onto the next part (or, if that part is already signed, sent as an empty-text part, a shape the
  API itself emits).
* Parallel calls: only the first ``functionCall`` has a signature; results follow as one user
  content in call order (FC1+sig, FC2 then FR1, FR2).
* Gemini 3/4 (``info.thinking == "level"``) validate the first call of each step. A call that has
  no signature (history from another model) gets Google's documented dummy value so it does not 400.
* ``contents`` never ends with a ``model`` turn (prefill is rejected since Gemini 3.6); a short
  user turn is appended instead. Both synthetic values are pure functions of the session log.

Errors are classified from the HTTP status and the Google ``error`` object (``status``,
``details``): per-minute 429s are retryable and carry ``RetryInfo.retryDelay``; daily quota,
spend-cap and prepay exhaustion are not.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import httpx

from kalash.llm.catalog import ModelInfo
from kalash.llm.errors import (
    OVERFLOW_PATTERN,
    ErrorCode,
    LLMError,
    classify_status,
    error_message,
    parse_retry_after,
)
from kalash.llm.http import SSEEvent, int_or_none, make_client, parse_json
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
    Usage,
)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DUMMY_THOUGHT_SIGNATURE = "skip_thought_signature_validator"
CONTINUE_PROMPT = "Continue."
# Gemini 4 Argon documents "at least 20 minutes per call" for client timeouts; httpx applies the
# read timeout per network read, so this is also the stream idle timeout.
STREAM_IDLE_TIMEOUT_S = 1200.0

_BUDGETS = {"low": 1024, "medium": 8192, "high": 24576}
_RANK = {"none": 0, "off": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6}
_LEVELS = ("minimal", "low", "medium", "high")

_FINISH: dict[str, FinishReason] = {
    "STOP": "stop",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "LANGUAGE": "content_filter",
    "IMAGE_SAFETY": "content_filter",
    "IMAGE_PROHIBITED_CONTENT": "content_filter",
    "IMAGE_RECITATION": "content_filter",
    "ESCALATION": "content_filter",
    "PUP_LIMITED_DISABLED": "content_filter",
}
_MALFORMED_FINISH = frozenset(
    {
        "MALFORMED_FUNCTION_CALL",
        "UNEXPECTED_TOOL_CALL",
        "TOO_MANY_TOOL_CALLS",
        "MALFORMED_RESPONSE",
    }
)

# ---------------------------------------------------------------------------------------------
# Tool schema sanitising: JSON Schema -> the OpenAPI 3.0 subset of ``Schema``
# ---------------------------------------------------------------------------------------------

_MAX_SCHEMA_DEPTH = 40
# Formats the Schema accepts per type; others are dropped (the description still carries them).
_FORMATS = {
    "string": frozenset({"enum", "date-time"}),
    "integer": frozenset({"int32", "int64"}),
    "number": frozenset({"float", "double"}),
}
_JSON_TYPE_OF = {bool: "boolean", int: "integer", float: "number", str: "string"}


def sanitize_schema(schema: Any) -> dict[str, Any]:
    """Convert a JSON Schema tool-parameter schema to Gemini's accepted OpenAPI subset.

    Pure and idempotent; the input is never mutated. Keeps ``type``, ``description``,
    ``properties``, ``required`` (only names that exist), ``items``, string ``enum``,
    ``nullable``, ``anyOf``, ``format`` (whitelisted per type) and the min/max/pattern
    constraints. Drops ``additionalProperties``, ``$schema``/``$id``, ``title``, ``default``,
    ``example(s)``, ``exclusiveMin/Max``, ``multipleOf``, ``uniqueItems`` and similar. Inlines
    ``$ref`` (cycles become an opaque object), merges ``allOf``, turns ``oneOf`` into ``anyOf``,
    folds ``null`` branches / ``type: [..., "null"]`` into ``nullable``, ``const`` into a
    one-value ``enum``. Enums that are not all strings are dropped (Gemini enums are strings
    only) and listed in the description. A node with no usable type becomes ``string`` and an
    array without ``items`` gets string items, because the API rejects both.
    """
    root = schema if isinstance(schema, dict) else {}
    out = _Sanitizer(root).convert(root, (), 0)
    if "properties" in out:
        out.setdefault("type", "object")
    return out


Refs = tuple[str, ...]


class _Sanitizer:
    def __init__(self, root: dict[str, Any]) -> None:
        self.root = root

    def convert(
        self, node: Any, refs: Refs, depth: int, *, fallback: bool = True
    ) -> dict[str, Any]:
        if not isinstance(node, dict):
            return {"type": "string"} if fallback else {}
        if depth > _MAX_SCHEMA_DEPTH:
            return {"type": "object"}
        node, refs = self._deref(node, refs)
        node = self._merge_all_of(node, refs)
        branches: list[Any] = []
        for key in ("anyOf", "oneOf"):
            value = node.get(key)
            if isinstance(value, list):
                branches.extend(value)
        if branches:
            return self._convert_branches(node, branches, refs, depth)
        return self._convert_plain(node, refs, depth, fallback=fallback)

    # -- $ref / allOf -------------------------------------------------------------------

    def _lookup(self, ref: str) -> dict[str, Any] | None:
        if not ref.startswith("#"):
            return None
        target: Any = self.root
        for raw in ref[1:].split("/"):
            if raw == "":
                continue
            key = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(target, dict) and key in target:
                target = target[key]
            else:
                return None
        return target if isinstance(target, dict) else None

    def _deref(self, node: dict[str, Any], refs: Refs) -> tuple[dict[str, Any], Refs]:
        for _ in range(_MAX_SCHEMA_DEPTH):
            ref = node.get("$ref")
            if not isinstance(ref, str):
                return node, refs
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            target = self._lookup(ref)
            if target is None or ref in refs:  # unresolvable or recursive: opaque object
                return {"type": "object", **siblings}, refs
            node, refs = {**target, **siblings}, (*refs, ref)
        return {"type": "object"}, refs

    def _merge_all_of(self, node: dict[str, Any], refs: Refs) -> dict[str, Any]:
        subs = node.get("allOf")
        if not isinstance(subs, list):
            return node
        merged = {k: v for k, v in node.items() if k != "allOf"}
        for sub in subs:
            if not isinstance(sub, dict):
                continue
            sub, _ = self._deref(sub, refs)
            sub = self._merge_all_of(sub, refs)
            for key, value in sub.items():
                if key == "properties" and isinstance(value, dict):
                    existing = merged.get("properties")
                    merged["properties"] = {
                        **(existing if isinstance(existing, dict) else {}),
                        **value,
                    }
                elif key == "required" and isinstance(value, list):
                    existing_req = merged.get("required")
                    names = list(existing_req) if isinstance(existing_req, list) else []
                    merged["required"] = names + [n for n in value if n not in names]
                else:
                    merged.setdefault(key, value)
        return merged

    # -- anyOf / oneOf ------------------------------------------------------------------

    def _convert_branches(
        self, node: dict[str, Any], branches: list[Any], refs: Refs, depth: int
    ) -> dict[str, Any]:
        converted = [self.convert(b, refs, depth + 1) for b in branches]
        nulls = [c for c in converted if c.get("type") == "null"]
        rest = [c for c in converted if c.get("type") != "null"]
        own_node = {k: v for k, v in node.items() if k not in ("anyOf", "oneOf")}
        own = self._convert_plain(own_node, refs, depth, fallback=False)
        nullable = bool(nulls) or own.pop("nullable", False)
        if len(rest) == 1:
            out = {**rest[0], **own}
        elif rest:
            out = {"anyOf": rest, **{k: v for k, v in own.items() if k != "type"}}
        else:
            out = {"type": "null", **own} if nullable else {"type": "string", **own}
            nullable = False
        if nullable:
            out["nullable"] = True
        return out

    # -- a single (non-union) schema node -------------------------------------------------

    def _convert_plain(
        self, node: dict[str, Any], refs: Refs, depth: int, *, fallback: bool
    ) -> dict[str, Any]:
        raw = node.get("type")
        names = raw if isinstance(raw, list) else [raw]
        types = [t.lower() for t in names if isinstance(t, str)]
        nullable = node.get("nullable") is True or "null" in types
        types = list(dict.fromkeys(t for t in types if t != "null"))
        description = node.get("description")
        text = description if isinstance(description, str) else ""

        if len(types) > 1:  # e.g. ["string", "integer"] -> anyOf with one schema per type
            shared = {k: v for k, v in node.items() if k not in ("type", "description")}
            variants = [
                self._convert_plain({**shared, "type": kind}, refs, depth, fallback=True)
                for kind in types
            ]
            out: dict[str, Any] = {"anyOf": variants}
            if text:
                out["description"] = text
            if nullable:
                out["nullable"] = True
            return out

        enum_values, enum_nullable = self._enum_values(node)
        nullable = nullable or enum_nullable
        kind = types[0] if types else self._infer_type(node, enum_values)
        structural = any(k in node for k in ("properties", "items", "enum", "const"))
        if kind is None and "null" in [n for n in names if isinstance(n, str)] and not structural:
            return {"type": "null", **({"description": text} if text else {})}
        if kind is None and fallback:
            kind = "string"

        out = {}
        if kind is not None:
            out["type"] = kind
        if kind == "object" or (kind is None and "properties" in node):
            self._object_fields(node, out, refs, depth)
        elif kind == "array":
            self._array_fields(node, out, refs, depth)
        elif kind == "string":
            self._copy_ints(node, out, ("minLength", "maxLength"))
            if isinstance(node.get("pattern"), str):
                out["pattern"] = node["pattern"]
        elif kind in ("integer", "number"):
            for key in ("minimum", "maximum"):
                value = node.get(key)
                if isinstance(value, int | float) and not isinstance(value, bool):
                    out[key] = value
        if kind in _FORMATS and node.get("format") in _FORMATS[kind]:
            out["format"] = node["format"]
        if enum_values:
            if all(isinstance(v, str) for v in enum_values):
                out["enum"] = list(dict.fromkeys(enum_values))
                out.setdefault("type", "string")
            else:  # Gemini enums are strings only; keep the type and say what is allowed
                shown = ", ".join(json.dumps(v, ensure_ascii=False) for v in enum_values)
                text = f"{text} Allowed values: {shown}.".strip()
        if text:
            out["description"] = text
        if nullable:
            out["nullable"] = True
        return out

    @staticmethod
    def _enum_values(node: dict[str, Any]) -> tuple[list[Any], bool]:
        values: list[Any] = []
        if isinstance(node.get("enum"), list):
            values = list(node["enum"])
        elif "const" in node:
            values = [node["const"]]
        return [v for v in values if v is not None], any(v is None for v in values)

    @staticmethod
    def _infer_type(node: dict[str, Any], enum_values: list[Any]) -> str | None:
        if "properties" in node:
            return "object"
        if "items" in node:
            return "array"
        if enum_values:
            kinds = {_JSON_TYPE_OF.get(type(v), "string") for v in enum_values}
            return kinds.pop() if len(kinds) == 1 else "string"
        return None

    @staticmethod
    def _copy_ints(node: dict[str, Any], out: dict[str, Any], keys: tuple[str, ...]) -> None:
        for key in keys:
            value = node.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                out[key] = value

    def _object_fields(
        self, node: dict[str, Any], out: dict[str, Any], refs: Refs, depth: int
    ) -> None:
        raw = node.get("properties")
        props: dict[str, Any] = {}
        if isinstance(raw, dict):
            for name, sub in raw.items():
                props[str(name)] = self.convert(sub, refs, depth + 1)
        if props:  # an OBJECT with empty `properties` is rejected, so the key is omitted
            out["properties"] = props
            required = node.get("required")
            if isinstance(required, list):  # `required` may only name existing properties
                names = [n for n in dict.fromkeys(required) if isinstance(n, str) and n in props]
                if names:
                    out["required"] = names
        self._copy_ints(node, out, ("minProperties", "maxProperties"))

    def _array_fields(
        self, node: dict[str, Any], out: dict[str, Any], refs: Refs, depth: int
    ) -> None:
        items = node.get("items")
        if isinstance(items, list):  # tuple form: accept any of the listed schemas
            alternatives = [self.convert(i, refs, depth + 1) for i in items]
            if len(alternatives) == 1:
                out["items"] = alternatives[0]
            else:
                out["items"] = {"anyOf": alternatives} if alternatives else {"type": "string"}
        elif isinstance(items, dict):
            out["items"] = self.convert(items, refs, depth + 1)
        else:
            out["items"] = {"type": "string"}  # `items` is required for ARRAY
        self._copy_ints(node, out, ("minItems", "maxItems"))


# ---------------------------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------------------------

_GRPC_HTTP = {
    "INVALID_ARGUMENT": 400,
    "FAILED_PRECONDITION": 400,
    "OUT_OF_RANGE": 400,
    "UNAUTHENTICATED": 401,
    "PERMISSION_DENIED": 403,
    "NOT_FOUND": 404,
    "ABORTED": 409,
    "ALREADY_EXISTS": 409,
    "RESOURCE_EXHAUSTED": 429,
    "CANCELLED": 499,
    "UNKNOWN": 500,
    "INTERNAL": 500,
    "DATA_LOSS": 500,
    "UNIMPLEMENTED": 501,
    "UNAVAILABLE": 503,
    "DEADLINE_EXCEEDED": 504,
}
_AUTH_REASONS = frozenset(
    {"ACCESS_TOKEN_EXPIRED", "CREDENTIALS_MISSING", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}
)
_AUTH_MESSAGE = re.compile(
    r"api key (?:not valid|expired|was reported as leaked)|invalid api key", re.IGNORECASE
)
_SIGNATURE_MESSAGE = re.compile(r"missing (?:a |the )?`?thought[_ ]?signature", re.IGNORECASE)
_GOOGLE_OVERFLOW = re.compile(
    r"input token count|exceeds the maximum number of tokens|token count .{0,40}exceeds",
    re.IGNORECASE,
)
# Terminal until a human acts: a retry (even with backoff) cannot succeed.
_TERMINAL_QUOTA = re.compile(
    r"spend(?:ing)? cap|prepa(?:y|id)|credit balance|credits? (?:are |is |have been )?"
    r"(?:depleted|exhausted)|billing account has exceeded|exceeded its monthly|out of credits"
    r"|insufficient (?:funds|balance|credits?)|exhausted your (?:capacity|daily|monthly|quota)",
    re.IGNORECASE,
)
_DAILY_QUOTA = re.compile(r"per[_ ]?day|daily", re.IGNORECASE)
_RETRY_IN_MESSAGE = re.compile(r"retry in (\d+(?:\.\d+)?)\s*s", re.IGNORECASE)
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s*$")


def _duration(value: Any) -> float | None:
    """A protobuf ``Duration`` as JSON (``"10s"``, ``"33.6s"`` or ``{seconds, nanos}``)."""
    if isinstance(value, str):
        match = _DURATION.match(value)
        return float(match.group(1)) if match else None
    if isinstance(value, dict):
        seconds, nanos = value.get("seconds", 0), value.get("nanos", 0)
        if isinstance(seconds, int | float) and isinstance(nanos, int | float):
            return float(seconds) + float(nanos) / 1e9
    return None


def google_error(
    status: int | None, payload: Any, text: str, headers: Any, provider: str
) -> LLMError:
    """Build an ``LLMError`` from a Google ``{"error": {code, message, status, details}}`` body.

    ``status`` is the HTTP status (``None`` for an error delivered inside a 200 SSE stream, in
    which case ``error.code`` / ``error.status`` stand in for it).
    """
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        payload = payload[0]  # non-SSE streaming wraps the error object in a JSON array
    raw = payload.get("error") if isinstance(payload, dict) else None
    err = raw if isinstance(raw, dict) else {}
    message = error_message(payload, text or f"HTTP {status}")
    grpc = str(err.get("status") or "")
    code_field = err.get("code")
    reported = status or (code_field if isinstance(code_field, int) else 0)
    http = reported or _GRPC_HTTP.get(grpc, 500)

    quota_ids: list[str] = []
    retry_delay: float | None = None
    reason = ""
    for item in err.get("details") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("@type", ""))
        if kind.endswith("QuotaFailure"):
            for violation in item.get("violations") or []:
                if isinstance(violation, dict):
                    quota_ids.extend(
                        v
                        for v in (violation.get("quotaId"), violation.get("quotaMetric"))
                        if isinstance(v, str)
                    )
        elif kind.endswith("RetryInfo"):
            retry_delay = _duration(item.get("retryDelay"))
        elif kind.endswith("ErrorInfo") and isinstance(item.get("reason"), str):
            reason = item["reason"]
    retry_after = parse_retry_after(headers)
    if retry_after is None:
        retry_after = retry_delay
    if retry_after is None:
        in_message = _RETRY_IN_MESSAGE.search(message)
        retry_after = float(in_message.group(1)) if in_message else None

    haystack = " ".join([*quota_ids, message])
    if reason.startswith("API_KEY") or reason in _AUTH_REASONS or _AUTH_MESSAGE.search(message):
        code = ErrorCode.AUTH
    elif _SIGNATURE_MESSAGE.search(message):
        code = ErrorCode.INVALID_REQUEST
    elif http == 402:
        code = ErrorCode.QUOTA
    elif http == 429 or grpc == "RESOURCE_EXHAUSTED":
        # Gemini words per-minute limits as "exceeded your current quota ... billing details",
        # so the message alone cannot decide; quotaId / RetryInfo / known terminal texts do.
        terminal = (
            _DAILY_QUOTA.search(" ".join(quota_ids))
            or _TERMINAL_QUOTA.search(message)
            or (_DAILY_QUOTA.search(haystack) and retry_delay is None)
        )
        code = ErrorCode.QUOTA if terminal else ErrorCode.RATE_LIMIT
    elif http == 504 or grpc == "DEADLINE_EXCEEDED":
        code = ErrorCode.TIMEOUT
    elif http == 499:
        code = ErrorCode.TRANSPORT
    elif http < 500 and (_GOOGLE_OVERFLOW.search(message) or OVERFLOW_PATTERN.search(message)):
        code = ErrorCode.CONTEXT_OVERFLOW
    else:
        code = classify_status(http, message)
    return LLMError(message, code, status=http, retry_after=retry_after, provider=provider)


# ---------------------------------------------------------------------------------------------
# Streamed response assembly
# ---------------------------------------------------------------------------------------------


@dataclass
class _Segment:
    kind: Literal["reasoning", "text", "call"]
    text: str = ""
    signature: str | None = None
    call_id: str = ""
    name: str = ""
    arguments: str = "{}"
    wire_id: str | None = None


class _Response:
    """Accumulates ``GenerateContentResponse`` chunks into ordered segments.

    Unsigned runs of the same kind merge; a part that carries a signature is its own segment
    (never merged), and so is every function call. A signature may arrive on an empty-text part
    at the end, so nothing is final until the stream ends.
    """

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.segments: list[_Segment] = []
        self.finish_reason: str | None = None
        self.finish_message: str | None = None
        self.block_reason: str | None = None
        self.usage_raw: dict[str, Any] | None = None
        self._calls = 0
        self._ids: set[str] = set()

    def feed(self, chunk: dict[str, Any]) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        feedback = chunk.get("promptFeedback")
        if isinstance(feedback, dict):
            block = feedback.get("blockReason")
            if isinstance(block, str) and block and block != "BLOCK_REASON_UNSPECIFIED":
                self.block_reason = block
        usage = chunk.get("usageMetadata")
        if isinstance(usage, dict):
            self.usage_raw = usage  # cumulative: the last one wins
        candidates = chunk.get("candidates")
        for candidate in candidates if isinstance(candidates, list) else []:
            if not isinstance(candidate, dict) or candidate.get("index", 0) != 0:
                continue
            content = candidate.get("content")
            parts = content.get("parts") if isinstance(content, dict) else None
            for part in parts if isinstance(parts, list) else []:
                events.extend(self._part(part))
            reason = candidate.get("finishReason")
            if isinstance(reason, str) and reason and reason != "FINISH_REASON_UNSPECIFIED":
                self.finish_reason = reason
                message = candidate.get("finishMessage")
                self.finish_message = message if isinstance(message, str) and message else None
        return events

    def _part(self, part: Any) -> list[StreamEvent]:
        if not isinstance(part, dict):
            return []
        signature = part.get("thoughtSignature", part.get("thought_signature"))
        signature = signature if isinstance(signature, str) and signature else None
        call = part.get("functionCall", part.get("function_call"))
        if isinstance(call, dict):
            return [self._call(call, signature)]
        text = part.get("text")
        if not isinstance(text, str) or (not text and signature is None):
            return []  # inlineData, executableCode, ... or an empty unsigned text part
        kind: Literal["reasoning", "text"] = "reasoning" if part.get("thought") is True else "text"
        last = self.segments[-1] if self.segments else None
        if signature is None and last and last.kind == kind and last.signature is None:
            last.text += text
        else:
            self.segments.append(_Segment(kind, text, signature))
        if not text:
            return []
        return [ReasoningDelta(text) if kind == "reasoning" else TextDelta(text)]

    def _call(self, call: dict[str, Any], signature: str | None) -> ToolCallStarted:
        name = call.get("name")
        if not isinstance(name, str) or not name:
            raise LLMError(
                "function call without a name", ErrorCode.MALFORMED_RESPONSE, provider=self.provider
            )
        args = call.get("args")
        wire_id = call.get("id") if isinstance(call.get("id"), str) and call.get("id") else None
        index = self._calls
        self._calls += 1
        base = f"call_{index}_{name}"
        call_id = wire_id or base
        attempt = 1
        while call_id in self._ids:  # ids must be unique within one response
            attempt += 1
            call_id = f"{base}_{attempt}"
        self._ids.add(call_id)
        self.segments.append(
            _Segment(
                "call",
                signature=signature,
                call_id=call_id,
                name=name,
                arguments=json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False),
                wire_id=wire_id,
            )
        )
        return ToolCallStarted(index, call_id, name)

    def blocks(self) -> list[Block]:
        blocks: list[Block] = []
        for seg in self.segments:
            if seg.kind == "reasoning":
                if seg.text or seg.signature:
                    blocks.append(ReasoningBlock(seg.text, seg.signature))
            elif seg.kind == "text":
                extra = {"thoughtSignature": seg.signature} if seg.signature else None
                blocks.append(TextBlock(seg.text, extra))
            else:
                extra = {}
                if seg.signature:
                    extra["thoughtSignature"] = seg.signature
                if seg.wire_id:
                    extra["id"] = seg.wire_id
                blocks.append(ToolCallBlock(seg.call_id, seg.name, seg.arguments, extra or None))
        return blocks


def _usage(raw: dict[str, Any] | None) -> Usage:
    if not raw:
        return Usage()
    prompt = int_or_none(raw.get("promptTokenCount"))
    candidates = int_or_none(raw.get("candidatesTokenCount"))
    thoughts = int_or_none(raw.get("thoughtsTokenCount"))
    output = (
        None if candidates is None and thoughts is None else (candidates or 0) + (thoughts or 0)
    )
    return Usage(
        input_tokens=prompt,
        output_tokens=output,
        cache_read_tokens=int_or_none(raw.get("cachedContentTokenCount")) or 0,
        reasoning_tokens=thoughts or 0,
    )


# ---------------------------------------------------------------------------------------------
# Request building helpers
# ---------------------------------------------------------------------------------------------


@dataclass
class _CallRef:
    name: str
    wire_id: str | None
    order: int


def _signature(extra: dict[str, Any] | None, direct: str | None = None) -> str | None:
    """The thought signature stored with a block (also the one the OpenAI-compat route kept)."""
    if direct:
        return direct
    if not extra:
        return None
    value = extra.get("thoughtSignature")
    if isinstance(value, str) and value:
        return value
    compat = extra.get("extra_content")
    google = compat.get("google") if isinstance(compat, dict) else None
    value = google.get("thought_signature") if isinstance(google, dict) else None
    return value if isinstance(value, str) and value else None


def _args(arguments: str) -> dict[str, Any]:
    try:
        value = json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _user_parts(blocks: list[Block]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, TextBlock) and block.text:
            parts.append({"text": block.text})
        elif isinstance(block, ImageBlock):
            parts.append({"inlineData": {"mimeType": block.media_type, "data": block.data}})
    return parts


def _append(contents: list[dict[str, Any]], role: str, parts: list[dict[str, Any]]) -> None:
    if not parts:
        return
    if contents and contents[-1]["role"] == role:
        contents[-1]["parts"].extend(parts)
    else:
        contents.append({"role": role, "parts": parts})


def _pick_level(effort: str, available: tuple[str, ...]) -> str | None:
    """The requested thinking level, or the nearest one this model accepts."""
    levels = [e for e in available if e in _LEVELS]
    rank = _RANK.get(effort.lower())
    if not levels or rank is None:
        return None
    if effort.lower() in levels:
        return effort.lower()
    return min(levels, key=lambda level: (abs(_RANK[level] - rank), _RANK[level]))


def _budget_for(effort: str, *, can_disable: bool) -> int | None:
    key = effort.lower()
    rank = _RANK.get(key)
    if rank is None:
        return None
    if rank == 0:  # never send 0 to a model that cannot turn thinking off
        return 0 if can_disable else _BUDGETS["low"]
    if key in _BUDGETS:
        return _BUDGETS[key]
    return _BUDGETS["low"] if rank < _RANK["low"] else _BUDGETS["high"]


class GeminiAdapter:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        api_key: str | None,
        context_window: int,
        info: ModelInfo | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key
        self.context_window = context_window
        self.info = info
        self._client = client or make_client(idle_timeout=STREAM_IDLE_TIMEOUT_S)

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def _origin(self) -> str:
        return f"{self.provider}/{self.model}"

    # -- request -----------------------------------------------------------------------

    def build_body(self, request: Request) -> dict[str, Any]:
        body: dict[str, Any] = {"contents": self.serialize(request)}
        if request.system:
            body["systemInstruction"] = {"parts": [{"text": request.system}]}
        if request.tools:
            declarations: list[dict[str, Any]] = []
            for tool in request.tools:
                declaration: dict[str, Any] = {
                    "name": tool.name,
                    "description": tool.description or tool.name,
                }
                parameters = sanitize_schema(tool.parameters)
                if parameters.get("properties"):  # an OBJECT with no properties is a 400
                    declaration["parameters"] = parameters
                declarations.append(declaration)
            body["tools"] = [{"functionDeclarations": declarations}]
        config: dict[str, Any] = {}
        if request.max_tokens is not None:
            limit = self.info.max_output if self.info and self.info.max_output > 0 else None
            # Above the per-request cap Gemini 4 starts long decode (CONTINUATION), unsupported.
            config["maxOutputTokens"] = min(request.max_tokens, limit or request.max_tokens)
        thinking = self._thinking_config(request.reasoning_effort)
        if thinking:
            config["thinkingConfig"] = thinking
        # Gemini 3.6+ ignore temperature/topP/topK and future models reject them with a 400.
        if request.temperature is not None and (self.info is None or self.info.sampling):
            config["temperature"] = request.temperature
        if config:
            body["generationConfig"] = config
        return body

    def _thinking_config(self, effort: str | None) -> dict[str, Any] | None:
        info = self.info
        if info is None or not effort:
            return None
        if info.thinking == "level":
            level = _pick_level(effort, info.efforts)
            if level is None:
                return None
            return {"thinkingLevel": level.upper(), "includeThoughts": True}
        if info.thinking == "budget":
            budget = _budget_for(effort, can_disable=info.can_disable_thinking)
            if budget is None:
                return None
            return {"thinkingBudget": budget, "includeThoughts": True}
        return None

    def serialize(self, request: Request) -> list[dict[str, Any]]:
        """Build ``contents`` from the session messages."""
        contents: list[dict[str, Any]] = []
        calls: dict[str, _CallRef] = {}  # calls of the most recent model turn
        messages = request.messages
        index = 0
        while index < len(messages):
            message = messages[index]
            if message.role == "tool":
                run: list[Message] = []
                while index < len(messages) and messages[index].role == "tool":
                    run.append(messages[index])
                    index += 1
                _append(contents, "user", self._function_responses(run, calls))
                continue
            index += 1
            if message.role == "user":
                _append(contents, "user", _user_parts(message.content))
            else:
                calls = {}
                _append(contents, "model", self._model_parts(message, calls))
        for content in contents:  # results lead any other parts of the same user turn
            if content["role"] == "user":
                content["parts"].sort(key=lambda p: 0 if "functionResponse" in p else 1)
        if contents and contents[-1]["role"] == "model":
            contents.append({"role": "user", "parts": [{"text": CONTINUE_PROMPT}]})
        return contents

    def _model_parts(self, message: Message, calls: dict[str, _CallRef]) -> list[dict[str, Any]]:
        same = message.origin == self._origin
        parts: list[dict[str, Any]] = []
        pending: str | None = None  # signature of a dropped thought summary
        for block in message.content:
            part: dict[str, Any]
            if isinstance(block, ReasoningBlock):
                sig = _signature(block.extra, block.signature) if same else None
                if sig:
                    if pending:
                        parts.append({"text": "", "thoughtSignature": pending})
                    pending = sig
                continue
            if isinstance(block, TextBlock):
                sig = _signature(block.extra) if same else None
                if not block.text and not sig:
                    continue
                part = {"text": block.text}
            elif isinstance(block, ToolCallBlock):
                sig = _signature(block.extra) if same else None
                wire_id = block.extra.get("id") if same and block.extra else None
                call: dict[str, Any] = {"name": block.name, "args": _args(block.arguments)}
                if isinstance(wire_id, str) and wire_id:
                    call["id"] = wire_id
                else:
                    wire_id = None
                calls[block.id] = _CallRef(block.name, wire_id, len(calls))
                part = {"functionCall": call}
            else:
                continue
            if sig:
                part["thoughtSignature"] = sig
            if pending:
                if "thoughtSignature" in part:
                    parts.append({"text": "", "thoughtSignature": pending})
                else:
                    part["thoughtSignature"] = pending
                pending = None
            parts.append(part)
        if self.info is not None and self.info.thinking == "level":
            first = next((p for p in parts if "functionCall" in p), None)
            if first is not None and "thoughtSignature" not in first:
                first["thoughtSignature"] = DUMMY_THOUGHT_SIGNATURE
        return parts

    @staticmethod
    def _function_responses(run: list[Message], calls: dict[str, _CallRef]) -> list[dict[str, Any]]:
        keyed: list[tuple[int, int, dict[str, Any]]] = []
        for position, message in enumerate(run):
            ref = calls.get(message.tool_call_id or "")
            name = message.name or (ref.name if ref else None) or "tool"
            output = message.text or "(no output)"
            response: dict[str, Any] = {"error" if message.is_error else "output": output}
            result: dict[str, Any] = {"name": name, "response": response}
            if ref is not None and ref.wire_id:
                result = {"id": ref.wire_id, **result}
            order = ref.order if ref is not None else len(calls) + position
            keyed.append((order, position, {"functionResponse": result}))
        keyed.sort(key=lambda item: (item[0], item[1]))  # results in the order calls were made
        return [part for _, _, part in keyed]

    def _url(self, request: Request) -> str:
        model = (request.model or self.model).removeprefix("models/")
        return f"{self.base_url}/models/{quote(model, safe='')}:streamGenerateContent?alt=sse"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.api_key:
            headers["x-goog-api-key"] = self.api_key
        return headers

    # -- response ----------------------------------------------------------------------

    async def _post(self, url: str, body: dict[str, Any]) -> AsyncIterator[SSEEvent]:
        """Like ``http.post_sse`` but keeps the error body: Google puts the retry delay and the
        violated quota in ``error.details``, which decide whether a 429 may be retried."""
        try:
            async with self._client.stream(
                "POST", url, json=body, headers=self._headers()
            ) as response:
                if response.status_code >= 400:
                    raw = await response.aread()
                    text = raw.decode("utf-8", "replace")
                    try:
                        payload: Any = json.loads(text)
                    except ValueError:
                        payload = None
                    raise google_error(
                        response.status_code, payload, text, response.headers, self.provider
                    )
                event: str | None = None
                data: list[str] = []
                async for line in response.aiter_lines():
                    if line == "":
                        if data:
                            yield SSEEvent(event, "\n".join(data))
                        event, data = None, []
                        continue
                    if line.startswith(":"):
                        continue
                    name, _, value = line.partition(":")
                    value = value.removeprefix(" ")
                    if name == "data":
                        data.append(value)
                    elif name == "event":
                        event = value
                if data:
                    yield SSEEvent(event, "\n".join(data))
        except httpx.TimeoutException as error:
            raise LLMError(
                f"request timed out ({type(error).__name__})",
                ErrorCode.TIMEOUT,
                provider=self.provider,
            ) from error
        except httpx.TransportError as error:
            raise LLMError(
                f"connection failed: {error}", ErrorCode.TRANSPORT, provider=self.provider
            ) from error

    async def stream(self, request: Request) -> AsyncIterator[StreamEvent]:
        response = _Response(self.provider)
        async for sse in self._post(self._url(request), self.build_body(request)):
            data = sse.data.strip()
            if not data or data == "[DONE]":
                continue
            chunk = parse_json(data, self.provider)
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):  # delivered inside an HTTP 200 stream
                raise google_error(None, chunk, json.dumps(chunk)[:500], None, self.provider)
            for event in response.feed(chunk):
                yield event
        yield self._finish(response)

    def _fail(self, message: str, code: ErrorCode) -> LLMError:
        return LLMError(message, code, provider=self.provider)

    def _finish(self, response: _Response) -> Done:
        reason = response.finish_reason
        if response.block_reason is not None:
            finish: FinishReason = "content_filter"
        elif reason is None:
            raise self._fail("stream ended before a finish reason", ErrorCode.TRANSPORT)
        else:
            detail = f": {response.finish_message}" if response.finish_message else ""
            if reason in _FINISH:
                finish = _FINISH[reason]
            elif reason == "MISSING_THOUGHT_SIGNATURE":
                raise self._fail(
                    f"request is missing a thought signature (finishReason {reason}){detail}",
                    ErrorCode.INVALID_REQUEST,
                )
            elif reason == "CONTINUATION":
                raise self._fail(
                    "finishReason CONTINUATION (long decode past the per-request output cap) "
                    "is not supported; lower max_tokens to the model's output limit",
                    ErrorCode.MALFORMED_RESPONSE,
                )
            elif reason in _MALFORMED_FINISH:
                raise self._fail(
                    f"model stopped with finishReason {reason}{detail}",
                    ErrorCode.MALFORMED_RESPONSE,
                )
            else:
                raise self._fail(
                    f"unknown finishReason {reason!r}{detail}", ErrorCode.MALFORMED_RESPONSE
                )
        blocks = response.blocks()
        has_calls = any(isinstance(b, ToolCallBlock) for b in blocks)
        if finish == "stop":
            if has_calls:
                finish = "tool_calls"
            elif not any(isinstance(b, TextBlock) and b.text for b in blocks):
                raise self._fail("model returned an empty response", ErrorCode.EMPTY_RESPONSE)
        message = Message("assistant", blocks, origin=self._origin)
        return Done(message, finish, _usage(response.usage_raw))
