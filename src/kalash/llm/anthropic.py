"""Anthropic Messages wire format, also used by DeepSeek's Messages-compatible endpoint."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from kalash.llm.catalog import ModelInfo, anthropic_style, effort_is_off, resolve_effort
from kalash.llm.errors import OVERFLOW_PATTERN, ErrorCode, LLMError
from kalash.llm.http import int_or_none, make_client, parse_json, post_sse
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

Dialect = Literal["anthropic", "deepseek", "compat"]

_STOP: dict[str, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "max_tokens",
    "model_context_window_exceeded": "max_tokens",
    "refusal": "refusal",
}
_ERROR_TYPES: dict[str, ErrorCode] = {
    "overloaded_error": ErrorCode.SERVER,
    "api_error": ErrorCode.SERVER,
    "rate_limit_error": ErrorCode.RATE_LIMIT,
    "authentication_error": ErrorCode.AUTH,
    "permission_error": ErrorCode.AUTH,
    "invalid_request_error": ErrorCode.INVALID_REQUEST,
    "not_found_error": ErrorCode.INVALID_REQUEST,
    "request_too_large": ErrorCode.CONTEXT_OVERFLOW,
}
_THINKING_BUDGET = {
    "minimal": 1024,
    "low": 2048,
    "medium": 8192,
    "high": 24576,
    "xhigh": 32000,
    "max": 32000,
}
# DeepSeek documents low|high|max and aliases the rest (minimal->low, medium/xhigh->high).
_DEEPSEEK_EFFORT = {
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "high",
    "max": "max",
}
DEFAULT_VERSION = "2023-06-01"


@dataclass
class _Block:
    kind: str
    text: str = ""
    signature: str = ""
    redacted: str = ""
    call_id: str = ""
    name: str = ""
    closed: bool = False


class AnthropicAdapter:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        api_key: str | None,
        context_window: int,
        default_max_tokens: int,
        dialect: Dialect = "anthropic",
        info: ModelInfo | None = None,
        version: str = DEFAULT_VERSION,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.info = info
        self.version = version
        self.provider = provider
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.context_window = context_window
        self.default_max_tokens = default_max_tokens
        self.dialect = dialect
        self._client = client or make_client()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- request -----------------------------------------------------------------------

    def build_body(self, request: Request) -> dict[str, Any]:
        max_tokens = request.max_tokens or self.default_max_tokens
        if self.info is not None:
            max_tokens = min(max_tokens, self.info.max_output)
        cache = self.dialect == "anthropic"
        thinking_on = not effort_is_off(request.reasoning_effort)
        body: dict[str, Any] = {
            "model": request.model,
            "max_tokens": max_tokens,
            "messages": self.serialize(request.messages, cache=cache, thinking_on=thinking_on),
            "stream": True,
        }
        if request.system:
            system: dict[str, Any] = {"type": "text", "text": request.system}
            if cache:
                system["cache_control"] = {"type": "ephemeral"}
            body["system"] = [system]
        if request.tools:
            tools = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in request.tools
            ]
            if cache:
                tools[-1]["cache_control"] = {"type": "ephemeral"}
            body["tools"] = tools
        body.update(self._thinking_params(request.reasoning_effort, max_tokens))
        sampling_ok = self.info is None or self.info.sampling
        if (
            request.temperature is not None and sampling_ok and "thinking" not in body
        ):  # temperature must be unset while thinking
            body["temperature"] = request.temperature
        return body

    def _thinking_params(self, requested: str | None, max_tokens: int) -> dict[str, Any]:
        if requested is None:
            return {}
        off = effort_is_off(requested)
        if self.dialect == "deepseek":
            if off:
                return {"thinking": {"type": "disabled"}}
            mapped = _DEEPSEEK_EFFORT.get(requested.lower(), "high")
            return {"thinking": {"type": "enabled"}, "output_config": {"effort": mapped}}
        if self.dialect == "compat":
            return {}
        style = self.info.thinking if self.info else anthropic_style(self.model)
        if style == "adaptive":
            # Claude 4.7+: thinking is adaptive by default; only effort (and, where allowed,
            # an explicit disable) is sent. budget_tokens would be a 400 here.
            if off:
                can_disable = self.info is None or self.info.can_disable_thinking
                return {"thinking": {"type": "disabled"}} if can_disable else {}
            effort = resolve_effort(self.info, requested)
            return {"output_config": {"effort": effort}} if effort else {}
        if style == "budget" and not off:
            budget = min(_THINKING_BUDGET.get(requested.lower(), 8192), max_tokens - 1024)
            if budget >= 1024:
                return {"thinking": {"type": "enabled", "budget_tokens": budget}}
        return {}

    def serialize(
        self, messages: list[Message], *, cache: bool = False, thinking_on: bool = True
    ) -> list[dict[str, Any]]:
        # Claude keeps thinking blocks only for the tool loop in progress: everything after the
        # last real user message. Earlier turns' blocks are stripped by the API anyway, and on
        # current models a replayed block is bound to its exact prefix (a 400 if it changed).
        loop_start = 0
        for index, message in enumerate(messages):
            if message.role == "user" and any(
                isinstance(b, TextBlock | ImageBlock) for b in message.content
            ):
                loop_start = index + 1
        wire: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            if message.role == "tool":
                content: list[dict[str, Any]] = [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id,
                        "content": message.text or "(no output)",
                        **({"is_error": True} if message.is_error else {}),
                    }
                ]
                role = "user"
            elif message.role == "user":
                content = _user_blocks(message.content)
                role = "user"
            else:
                content = self._assistant_blocks(message, index >= loop_start, thinking_on)
                role = "assistant"
            if not content:
                continue
            if wire and wire[-1]["role"] == role:
                wire[-1]["content"].extend(content)
            else:
                wire.append({"role": role, "content": content})
        for item in wire:  # tool results must lead their user message
            if item["role"] == "user":
                item["content"].sort(key=lambda b: 0 if b.get("type") == "tool_result" else 1)
        if cache and wire:
            last = wire[-1]["content"][-1]
            if last.get("type") in ("text", "tool_result", "image"):
                last["cache_control"] = {"type": "ephemeral"}
        return wire

    def _assistant_blocks(
        self, message: Message, in_current_loop: bool = True, thinking_on: bool = True
    ) -> list[dict[str, Any]]:
        same_model = message.origin == f"{self.provider}/{self.model}"
        blocks: list[dict[str, Any]] = []
        saw_reasoning = False
        for block in message.content:
            if isinstance(block, ReasoningBlock):
                saw_reasoning = True
                if self.dialect == "anthropic":
                    if not (same_model and in_current_loop):
                        continue
                    if block.redacted:
                        blocks.append({"type": "redacted_thinking", "data": block.redacted})
                    elif block.signature:
                        blocks.append(
                            {
                                "type": "thinking",
                                "thinking": block.text,
                                "signature": block.signature,
                            }
                        )
                elif same_model or self.dialect == "deepseek":
                    item: dict[str, Any] = {"type": "thinking", "thinking": block.text}
                    if same_model and block.signature:
                        item["signature"] = block.signature
                    blocks.append(item)
            elif isinstance(block, TextBlock) and block.text:
                blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, ToolCallBlock):
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": _tool_input(block.arguments),
                    }
                )
        if self.dialect == "deepseek" and thinking_on and not saw_reasoning and blocks:
            # DeepSeek rejects (HTTP 400) a thinking-mode request with tools when any earlier
            # assistant turn lacks its thinking block; an empty one satisfies the check.
            blocks.insert(0, {"type": "thinking", "thinking": ""})
        return blocks

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "anthropic-version": self.version,
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
            if self.dialect == "deepseek":
                headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    # -- response ----------------------------------------------------------------------

    async def stream(self, request: Request) -> AsyncIterator[StreamEvent]:
        url = f"{self.base_url}/v1/messages"
        blocks: dict[int, _Block] = {}
        order: list[int] = []
        stop: str | None = None
        usage = Usage()
        input_parts = {"input": 0, "read": 0, "write": 0}
        saw_input = False
        async for sse in post_sse(
            self._client, url, self.build_body(request), self._headers(), self.provider
        ):
            event = parse_json(sse.data, self.provider)
            if not isinstance(event, dict):
                continue
            kind = event.get("type") or sse.event
            if kind == "error":
                raise self._error(event)
            if kind == "message_start":
                raw = (event.get("message") or {}).get("usage") or {}
                saw_input = _merge_usage(raw, input_parts, usage) or saw_input
            elif kind == "content_block_start":
                index = event.get("index")
                block = event.get("content_block") or {}
                if not isinstance(index, int) or index in blocks:
                    raise LLMError(
                        "duplicate or invalid content block start",
                        ErrorCode.MALFORMED_RESPONSE,
                        provider=self.provider,
                    )
                state = _Block(kind=str(block.get("type", "")))
                if state.kind == "text":
                    state.text = block.get("text") or ""
                elif state.kind == "thinking":
                    state.text = block.get("thinking") or ""
                    state.signature = block.get("signature") or ""
                elif state.kind == "redacted_thinking":
                    state.redacted = block.get("data") or ""
                elif state.kind == "tool_use":
                    state.call_id = block.get("id") or ""
                    state.name = block.get("name") or ""
                    initial = block.get("input")
                    if isinstance(initial, dict) and initial:
                        state.text = json.dumps(initial)
                    yield ToolCallStarted(index, state.call_id, state.name)
                blocks[index] = state
                order.append(index)
                if state.kind == "text" and state.text:
                    yield TextDelta(state.text)
            elif kind == "content_block_delta":
                state_or_none = blocks.get(event.get("index", -1))
                if state_or_none is None or state_or_none.closed:
                    continue  # straggler for a closed/unknown block; ignore explicitly
                state = state_or_none
                delta = event.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta":
                    piece = delta.get("text") or ""
                    state.text += piece
                    if piece:
                        yield TextDelta(piece)
                elif dtype == "thinking_delta":
                    piece = delta.get("thinking") or ""
                    state.text += piece
                    if piece:
                        yield ReasoningDelta(piece)
                elif dtype == "signature_delta":
                    state.signature += delta.get("signature") or ""
                elif dtype == "input_json_delta":
                    state.text += delta.get("partial_json") or ""
            elif kind == "content_block_stop":
                closing = blocks.get(event.get("index", -1))
                if closing is not None:
                    closing.closed = True
            elif kind == "message_delta":
                delta = event.get("delta") or {}
                if delta.get("stop_reason"):
                    stop = str(delta["stop_reason"])
                raw = event.get("usage") or {}
                saw_input = _merge_usage(raw, input_parts, usage) or saw_input
        if stop is None:
            raise LLMError(
                "stream ended before a stop reason", ErrorCode.TRANSPORT, provider=self.provider
            )
        finish = _STOP.get(stop)
        if finish is None:
            raise LLMError(
                f"unknown stop reason {stop!r}",
                ErrorCode.MALFORMED_RESPONSE,
                provider=self.provider,
            )
        if saw_input:
            usage.input_tokens = input_parts["input"] + input_parts["read"] + input_parts["write"]
            usage.cache_read_tokens = input_parts["read"]
            usage.cache_write_tokens = input_parts["write"]
        content: list[Block] = []
        for index in order:
            state = blocks[index]
            if state.kind == "text" and state.text:
                content.append(TextBlock(state.text))
            elif state.kind == "thinking":
                content.append(ReasoningBlock(state.text, state.signature or None))
            elif state.kind == "redacted_thinking":
                content.append(ReasoningBlock("", redacted=state.redacted))
            elif state.kind == "tool_use":
                if not state.closed and finish != "max_tokens":
                    raise LLMError(
                        "tool call block was never closed",
                        ErrorCode.TRANSPORT,
                        provider=self.provider,
                    )
                content.append(ToolCallBlock(state.call_id, state.name, state.text or "{}"))
        has_calls = any(isinstance(b, ToolCallBlock) for b in content)
        if finish == "tool_calls" and not has_calls:
            finish = "stop"
        if finish == "stop" and has_calls:
            finish = "tool_calls"
        if finish == "stop" and not any(isinstance(b, TextBlock | ToolCallBlock) for b in content):
            raise LLMError(
                "model returned an empty response", ErrorCode.EMPTY_RESPONSE, provider=self.provider
            )
        message = Message("assistant", content, origin=f"{self.provider}/{self.model}")
        yield Done(message, finish, usage)

    def _error(self, event: dict[str, Any]) -> LLMError:
        err = event.get("error") or {}
        message = str(err.get("message") or err or "stream error")
        code = _ERROR_TYPES.get(str(err.get("type")), ErrorCode.SERVER)
        if OVERFLOW_PATTERN.search(message):
            code = ErrorCode.CONTEXT_OVERFLOW
        return LLMError(message, code, provider=self.provider)


def _merge_usage(raw: dict[str, Any], parts: dict[str, int], usage: Usage) -> bool:
    saw = False
    for key, name in (
        ("input_tokens", "input"),
        ("cache_read_input_tokens", "read"),
        ("cache_creation_input_tokens", "write"),
    ):
        value = int_or_none(raw.get(key))
        if value is not None:
            parts[name] = value
            saw = saw or key == "input_tokens"
    output = int_or_none(raw.get("output_tokens"))
    if output is not None:
        usage.output_tokens = output
    return saw


def _user_blocks(blocks: list[Block]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, TextBlock) and block.text:
            out.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock):
            out.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": block.media_type,
                        "data": block.data,
                    },
                }
            )
    return out


def _tool_input(arguments: str) -> dict[str, Any]:
    try:
        value = json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
