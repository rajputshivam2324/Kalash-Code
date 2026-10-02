"""Anthropic provider adapter.

Maps Kalash normalized messages to/from the Anthropic Messages API.
Uses lazy import of the anthropic package to avoid import-time cost.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from kalash.core.budget import Pricing, Usage

from ..normalize import (
    BlockDelta,
    BlockStart,
    BlockStop,
    ContentBlock,
    ImageBlock,
    Message,
    MessageStart,
    MessageStop,
    ModelCapabilities,
    ModelResponse,
    OpaqueBlock,
    OpaquePayload,
    StopReason,
    StreamError,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UsageUpdate,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Anthropic stop reason mapping
# ---------------------------------------------------------------------------

_STOP_REASON_MAP: dict[str, StopReason] = {
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.STOP_SEQUENCE,
}


def _map_stop_reason(reason: str | None) -> StopReason:
    if reason is None:
        return StopReason.UNKNOWN
    return _STOP_REASON_MAP.get(reason, StopReason.UNKNOWN)


# ---------------------------------------------------------------------------
# Message serialization
# ---------------------------------------------------------------------------


def _serialize_content_block(block: ContentBlock) -> dict[str, Any]:
    """Convert a normalized content block to Anthropic wire format."""
    match block:
        case TextBlock(text=text):
            return {"type": "text", "text": text}

        case ToolUseBlock(id=id_, name=name, input=input_):
            return {"type": "tool_use", "id": id_, "name": name, "input": input_}

        case ToolResultBlock(tool_use_id=id_, content=content, is_error=is_err):
            if isinstance(content, str):
                result_content = [{"type": "text", "text": content}]
            else:
                result_content = [_serialize_content_block(b) for b in content]
            return {
                "type": "tool_result",
                "tool_use_id": id_,
                "content": result_content,
                "is_error": is_err,
            }

        case ThinkingBlock(thinking=thinking, signature=sig):
            block_data: dict[str, Any] = {"type": "thinking", "thinking": thinking}
            if sig:
                block_data["signature"] = sig
            return block_data

        case ImageBlock(source_type=src_type, media_type=media_type, data=data):
            if src_type == "url":
                return {
                    "type": "image",
                    "source": {"type": "url", "url": data},
                }
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data,
                },
            }

        case OpaqueBlock(payload=payload) if payload.provider == "anthropic":
            # Round-trip opaque blocks back to their original form
            return payload.data

        case _:
            # Best effort: serialize as text
            return {"type": "text", "text": str(block)}


def _serialize_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert normalized messages to Anthropic format."""
    result = []
    for msg in messages:
        content = [_serialize_content_block(b) for b in msg.content]
        result.append({"role": msg.role.value, "content": content})
    return result


# ---------------------------------------------------------------------------
# Response deserialization
# ---------------------------------------------------------------------------


def _deserialize_content_block(block: dict[str, Any]) -> ContentBlock:
    """Convert an Anthropic content block to normalized form."""
    block_type = block.get("type", "")

    match block_type:
        case "text":
            return TextBlock(text=block["text"])

        case "tool_use":
            return ToolUseBlock(
                id=block["id"],
                name=block["name"],
                input=block.get("input", {}),
            )

        case "thinking":
            return ThinkingBlock(
                thinking=block.get("thinking", ""),
                signature=block.get("signature"),
            )

        case _:
            # Carry unknown blocks as opaque
            return OpaqueBlock(
                payload=OpaquePayload(
                    provider="anthropic",
                    model_family="claude",
                    data=block,
                )
            )


def _normalize_usage(raw_usage: dict[str, Any]) -> Usage:
    """Normalize Anthropic usage.

    Anthropic reports input_tokens which includes cache reads.
    We normalize so input_tokens excludes cache reads.
    """
    input_tokens = raw_usage.get("input_tokens", 0)
    cache_read = raw_usage.get("cache_read_input_tokens", 0)
    cache_write = raw_usage.get("cache_creation_input_tokens", 0)

    return Usage(
        input_tokens=input_tokens - cache_read,
        output_tokens=raw_usage.get("output_tokens", 0),
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        reasoning_tokens=0,
        source="provider_reported",
        provider_raw=raw_usage,
    )


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------


class AnthropicProvider:
    """Adapter for the Anthropic Messages API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 8192,
        base_url: str | None = None,
        thinking: bool = False,
        thinking_budget: int | None = None,
        cache_prompt: bool = True,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._default_max_tokens = max_tokens
        self._base_url = base_url
        self._thinking = thinking
        self._thinking_budget = thinking_budget
        # On by default: the system prompt and tool schemas are stable within a
        # session, so caching them is a straight cost reduction with no
        # behavioural change.
        self._cache_prompt = cache_prompt
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazy-initialize the Anthropic async client."""
        if self._client is None:
            import anthropic

            kwargs: dict[str, Any] = {}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = anthropic.AsyncAnthropic(**kwargs)
        return self._client

    @property
    def name(self) -> str:
        return f"anthropic/{self._model}"

    @property
    def capabilities(self) -> ModelCapabilities:
        # Capabilities vary by model, but these are sane defaults for Claude 3.5+
        return ModelCapabilities(
            context_window=200_000,
            max_output_tokens=8192,
            tool_use=True,
            vision=True,
            reasoning=self._thinking,
            streaming=True,
            json_mode=False,
            system_messages=True,
            stop_sequences=True,
            cache_control=True,
            parallel_tool_use=True,
            extended_thinking=self._thinking,
            max_thinking_tokens=self._thinking_budget,
        )

    @property
    def pricing(self) -> Pricing:
        # Default pricing for Claude Sonnet 4
        return Pricing(
            input_per_mtok=Decimal("3.00"),
            output_per_mtok=Decimal("15.00"),
            cache_read_per_mtok=Decimal("0.30"),
            cache_write_per_mtok=Decimal("3.75"),
        )

    def _build_request(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        stop_sequences: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build the Anthropic API request body."""
        request: dict[str, Any] = {
            "model": self._model,
            "messages": _serialize_messages(messages),
            "max_tokens": max_tokens or self._default_max_tokens,
        }

        # Prompt caching. Anthropic caches a stable prefix only when a
        # breakpoint is declared explicitly — it never happens implicitly — and
        # this was previously never set, so every turn paid full input price for
        # the whole system prompt and tool schema block. Cache reads bill at a
        # fraction of input, and both of these are byte-stable within a session,
        # which makes them the two highest-value breakpoints available.
        if system:
            request["system"] = (
                [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                if self._cache_prompt
                else system
            )

        if tools:
            if self._cache_prompt:
                # The breakpoint goes on the final tool, so the whole tool block
                # is covered by one marker.
                cached = [dict(tool) for tool in tools]
                cached[-1]["cache_control"] = {"type": "ephemeral"}
                request["tools"] = cached
            else:
                request["tools"] = tools

        if temperature is not None:
            request["temperature"] = temperature

        if stop_sequences:
            request["stop_sequences"] = stop_sequences

        if self._thinking and self._thinking_budget:
            request["thinking"] = {
                "type": "enabled",
                "budget_tokens": self._thinking_budget,
            }

        # Pass through any extra kwargs (e.g., metadata)
        for key, value in kwargs.items():
            if value is not None:
                request[key] = value

        return request

    async def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        stop_sequences: list[str] | None = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Send a completion request to Anthropic."""
        client = self._get_client()
        request = self._build_request(
            messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            stop_sequences=stop_sequences,
            **kwargs,
        )

        response = await client.messages.create(**request)

        # Deserialize content blocks
        content = [_deserialize_content_block(block.model_dump()) for block in response.content]

        # Normalize usage
        usage = _normalize_usage(response.usage.model_dump())

        return ModelResponse(
            id=response.id,
            model=response.model,
            content=content,
            stop_reason=_map_stop_reason(response.stop_reason),
            usage=usage,
        )

    async def stream(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        stop_sequences: list[str] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a response from Anthropic."""
        client = self._get_client()
        request = self._build_request(
            messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            stop_sequences=stop_sequences,
            **kwargs,
        )

        try:
            async with client.messages.stream(**request) as stream:
                block_index = 0

                async for event in stream:
                    match event.type:
                        case "message_start":
                            msg = event.message
                            yield MessageStart(id=msg.id, model=msg.model)
                            if hasattr(msg, "usage") and msg.usage:
                                raw = (
                                    msg.usage.model_dump()
                                    if hasattr(msg.usage, "model_dump")
                                    else {}
                                )
                                if raw:
                                    yield UsageUpdate(
                                        input_tokens=raw.get("input_tokens", 0),
                                        cache_read_tokens=raw.get("cache_read_input_tokens", 0),
                                        cache_write_tokens=raw.get(
                                            "cache_creation_input_tokens", 0
                                        ),
                                    )

                        case "content_block_start":
                            cb = event.content_block
                            cb_type = cb.type if hasattr(cb, "type") else "text"

                            if cb_type == "tool_use":
                                yield BlockStart(
                                    index=block_index,
                                    block_type="tool_use",
                                    tool_use_id=cb.id if hasattr(cb, "id") else None,
                                    tool_name=cb.name if hasattr(cb, "name") else None,
                                )
                            elif cb_type == "thinking":
                                yield BlockStart(index=block_index, block_type="thinking")
                            else:
                                yield BlockStart(index=block_index, block_type="text")

                        case "content_block_delta":
                            delta = event.delta
                            delta_type = delta.type if hasattr(delta, "type") else ""

                            if delta_type == "text_delta":
                                yield BlockDelta(index=block_index, delta=delta.text)
                            elif delta_type == "input_json_delta":
                                yield BlockDelta(
                                    index=block_index,
                                    delta=delta.partial_json,
                                )
                            elif delta_type == "thinking_delta":
                                yield BlockDelta(index=block_index, delta=delta.thinking)

                        case "content_block_stop":
                            yield BlockStop(index=block_index)
                            block_index += 1

                        case "message_delta":
                            delta = event.delta
                            if hasattr(delta, "stop_reason") and delta.stop_reason:
                                yield MessageStop(stop_reason=_map_stop_reason(delta.stop_reason))
                            if hasattr(event, "usage") and event.usage:
                                raw = (
                                    event.usage.model_dump()
                                    if hasattr(event.usage, "model_dump")
                                    else {}
                                )
                                if raw.get("output_tokens"):
                                    yield UsageUpdate(output_tokens=raw["output_tokens"])

                        case "error":
                            error_data = event.error if hasattr(event, "error") else {}
                            msg = str(error_data) if error_data else "Unknown stream error"
                            yield StreamError(error=msg, code="anthropic_stream_error")
        except Exception as exc:
            yield StreamError(error=str(exc), code="anthropic_stream_error", recoverable=True)

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.close()
            self._client = None
