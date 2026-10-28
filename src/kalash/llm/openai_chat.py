"""OpenAI Chat Completions wire format (OpenAI, Gemini, Groq, OpenRouter, Sarvam, Ollama, ...)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from kalash.llm.catalog import ModelInfo, effort_is_off, resolve_effort
from kalash.llm.errors import OVERFLOW_PATTERN, ErrorCode, LLMError, classify_status, error_message
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


@dataclass
class OpenAIDialect:
    """Per-provider differences inside the Chat Completions format."""

    token_param: str = "max_tokens"
    reasoning_replay_field: str | None = None  # overrides ModelInfo.replay_field
    stream_usage: bool = True
    auth: str = "bearer"  # "bearer" | "none" (a provider-specific key header is in extra_headers)
    reasoning_style: str = "effort"  # "effort": reasoning_effort=x  |  "object": reasoning={effort}
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Call:
    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""
    extra: dict[str, Any] | None = None
    announced: bool = False


_FINISH: dict[str, FinishReason] = {
    "stop": "stop",
    "end_turn": "stop",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "length": "max_tokens",
    "max_tokens": "max_tokens",
    "content_filter": "content_filter",
    "safety": "content_filter",
}


class OpenAIChatAdapter:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        api_key: str | None,
        context_window: int,
        dialect: OpenAIDialect | None = None,
        info: ModelInfo | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.info = info
        self.provider = provider
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.context_window = context_window
        self.dialect = dialect or OpenAIDialect()
        self._client = client or make_client()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- request -----------------------------------------------------------------------

    def build_body(self, request: Request) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": self.serialize(request),
            "stream": True,
        }
        stream_usage = self.dialect.stream_usage and (self.info is None or self.info.stream_usage)
        if stream_usage:
            body["stream_options"] = {"include_usage": True}
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in request.tools
            ]
        if request.max_tokens is not None:
            body[self.dialect.token_param] = request.max_tokens
        body.update(self._reasoning_params(request.reasoning_effort))
        if request.temperature is not None and (self.info is None or self.info.sampling):
            body["temperature"] = request.temperature
        body.update(self.dialect.extra_body)
        return body

    def _reasoning_params(self, requested: str | None) -> dict[str, Any]:
        if requested is None:
            return {}
        if effort_is_off(requested) and self.info is not None:
            off_body = self.info.extra.get("off_body")
            if isinstance(off_body, dict):
                return dict(off_body)  # e.g. Sarvam V2 DeepSeek: chat_template_kwargs
        effort = resolve_effort(self.info, requested)
        if effort is None:
            return {}
        if self.dialect.reasoning_style == "object":
            return {"reasoning": {"effort": effort}}
        return {"reasoning_effort": effort}

    def serialize(self, request: Request) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = []
        if request.system:
            wire.append({"role": "system", "content": request.system})
        for message in request.messages:
            if message.role == "tool":
                wire.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id,
                        "content": message.text or "(no output)",
                    }
                )
            elif message.role == "user":
                content = _user_content(message.content)
                if content:
                    wire.append({"role": "user", "content": content})
            else:
                item = self._assistant(message)
                if item is not None:
                    wire.append(item)
        return wire

    def _assistant(self, message: Message) -> dict[str, Any] | None:
        own = (message.origin or "").split("/", 1)[0] == self.provider
        text = message.text
        calls = message.tool_calls
        reasoning = "".join(b.text for b in message.content if isinstance(b, ReasoningBlock))
        if not text and not calls:
            return None
        item: dict[str, Any] = {"role": "assistant", "content": text or None}
        if calls:
            item["tool_calls"] = []
            for call in calls:
                wire_call: dict[str, Any] = {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": _valid_json_object(call.arguments),
                    },
                }
                if own and call.extra and "extra_content" in call.extra:
                    wire_call["extra_content"] = call.extra["extra_content"]
                item["tool_calls"].append(wire_call)
        field_name = self.dialect.reasoning_replay_field or (
            self.info.replay_field if self.info else None
        )
        if field_name and own and reasoning:
            item[field_name] = reasoning
        return item

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.api_key and self.dialect.auth == "bearer":
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.dialect.extra_headers)
        return headers

    # -- response ----------------------------------------------------------------------

    async def stream(self, request: Request) -> AsyncIterator[StreamEvent]:
        url = f"{self.base_url}/chat/completions"
        body = self.build_body(request)
        text: list[str] = []
        reasoning: list[str] = []
        calls: list[_Call] = []
        by_index: dict[int, _Call] = {}
        finish_raw: str | None = None
        usage = Usage()
        async for sse in post_sse(self._client, url, body, self._headers(), self.provider):
            if sse.data.strip() == "[DONE]":
                break
            chunk = parse_json(sse.data, self.provider)
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):
                raise self._stream_error(chunk)
            if isinstance(chunk.get("usage"), dict):
                usage = _usage(chunk["usage"])
            for choice in chunk.get("choices") or []:
                if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                    continue
                delta = choice.get("delta") or {}
                piece = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(piece, str) and piece:
                    reasoning.append(piece)
                    yield ReasoningDelta(piece)
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    text.append(piece)
                    yield TextDelta(piece)
                for raw in delta.get("tool_calls") or []:
                    call = _merge_call(raw, calls, by_index)
                    if call is not None and not call.announced and call.id and call.name:
                        call.announced = True
                        yield ToolCallStarted(call.index, call.id, call.name)
                if choice.get("finish_reason"):
                    finish_raw = str(choice["finish_reason"])
                    if isinstance(choice.get("usage"), dict):
                        usage = _usage(choice["usage"])
        try:
            yield self._finish(text, reasoning, calls, finish_raw, usage)
        except LLMError as error:
            if usage.input_tokens is not None or usage.output_tokens is not None:
                error.usage = usage
            raise

    def _finish(
        self,
        text: list[str],
        reasoning: list[str],
        calls: list[_Call],
        finish_raw: str | None,
        usage: Usage,
    ) -> Done:
        if finish_raw is None:
            raise LLMError(
                "stream ended before a finish reason", ErrorCode.TRANSPORT, provider=self.provider
            )
        if finish_raw == "insufficient_system_resource":
            raise LLMError(
                "provider ran out of capacity mid-response",
                ErrorCode.SERVER,
                provider=self.provider,
            )
        finish = _FINISH.get(finish_raw)
        if finish is None:
            raise LLMError(
                f"unknown finish reason {finish_raw!r}",
                ErrorCode.MALFORMED_RESPONSE,
                provider=self.provider,
            )
        blocks: list[Block] = []
        if reasoning:
            blocks.append(ReasoningBlock("".join(reasoning)))
        if text:
            blocks.append(TextBlock("".join(text)))
        for call in calls:
            if not call.name:
                raise LLMError(
                    "tool call without a name", ErrorCode.MALFORMED_RESPONSE, provider=self.provider
                )
            blocks.append(
                ToolCallBlock(
                    call.id or f"call_{call.index}", call.name, call.arguments, call.extra
                )
            )
        has_calls = any(isinstance(b, ToolCallBlock) for b in blocks)
        if finish == "stop" and has_calls:
            finish = "tool_calls"
        if finish == "tool_calls" and not has_calls:
            finish = "stop"
        if finish == "stop" and not any(isinstance(b, TextBlock | ToolCallBlock) for b in blocks):
            raise LLMError(
                "model returned an empty response", ErrorCode.EMPTY_RESPONSE, provider=self.provider
            )
        ids = [b.id for b in blocks if isinstance(b, ToolCallBlock)]
        if len(ids) != len(set(ids)):
            raise LLMError(
                "duplicate tool call ids in one response",
                ErrorCode.MALFORMED_RESPONSE,
                provider=self.provider,
            )
        message = Message("assistant", blocks, origin=f"{self.provider}/{self.model}")
        return Done(message, finish, usage)

    def _stream_error(self, chunk: dict[str, Any]) -> LLMError:
        err = chunk["error"]
        message = error_message(chunk, json.dumps(err)[:500])
        status = err.get("code") if isinstance(err, dict) else None
        status_int = status if isinstance(status, int) else None
        if OVERFLOW_PATTERN.search(message):
            code = ErrorCode.CONTEXT_OVERFLOW
        else:
            code = classify_status(status_int or 500, message)
        return LLMError(message, code, status=status_int, provider=self.provider)


def _merge_call(raw: Any, calls: list[_Call], by_index: dict[int, _Call]) -> _Call | None:
    """Assemble streamed tool-call fragments. Identity fields are set once, never appended."""
    if not isinstance(raw, dict):
        return None
    index = raw.get("index")
    call_id = raw.get("id") if isinstance(raw.get("id"), str) else ""
    call: _Call | None = None
    if isinstance(index, int):
        call = by_index.get(index)
        if call is not None and call_id and call.id and call_id != call.id:
            call = None  # same index reused for a different call (seen on some servers)
    elif call_id:
        call = next((c for c in calls if c.id == call_id), None)
    elif calls and not (raw.get("function") or {}).get("name"):
        call = calls[-1]  # an anonymous continuation fragment of the previous call
    if call is None:
        call = _Call(index=len(calls))
        calls.append(call)
        if isinstance(index, int):
            by_index[index] = call
    if call_id and not call.id:
        call.id = call_id
    function = raw.get("function") or {}
    name = function.get("name")
    if isinstance(name, str) and name and not call.name:
        call.name = name
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        call.arguments += arguments
    elif isinstance(arguments, dict):
        call.arguments += json.dumps(arguments)
    extra = raw.get("extra_content")
    if isinstance(extra, dict):
        call.extra = {"extra_content": extra}
    return call


def _usage(raw: dict[str, Any]) -> Usage:
    prompt = raw.get("prompt_tokens_details") or {}
    completion = raw.get("completion_tokens_details") or {}
    cached = int_or_none(prompt.get("cached_tokens") if isinstance(prompt, dict) else None)
    if cached is None:
        cached = int_or_none(raw.get("prompt_cache_hit_tokens"))
    reasoning = int_or_none(
        completion.get("reasoning_tokens") if isinstance(completion, dict) else None
    )
    return Usage(
        input_tokens=int_or_none(raw.get("prompt_tokens")),
        output_tokens=int_or_none(raw.get("completion_tokens")),
        cache_read_tokens=cached or 0,
        reasoning_tokens=reasoning or 0,
    )


def _user_content(blocks: list[Block]) -> str | list[dict[str, Any]]:
    if all(isinstance(b, TextBlock) for b in blocks):
        return "".join(b.text for b in blocks if isinstance(b, TextBlock))
    parts: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, TextBlock) and block.text:
            parts.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock):
            url = f"data:{block.media_type};base64,{block.data}"
            parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def _valid_json_object(arguments: str) -> str:
    """Replay arguments verbatim when they are a JSON object; otherwise ``{}``."""
    try:
        value = json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        return "{}"
    return arguments if isinstance(value, dict) and arguments.strip() else "{}"
