"""OpenAI provider adapter.

Maps Kalash normalized messages to/from the OpenAI Chat Completions API.
Uses lazy import of the openai package.
"""

from __future__ import annotations

import json
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
    Role,
    StopReason,
    StreamError,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UsageUpdate,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stop reason mapping
# ---------------------------------------------------------------------------

_STOP_REASON_MAP: dict[str, StopReason] = {
    "stop": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.CONTENT_FILTER,
}


def _map_stop_reason(reason: str | None) -> StopReason:
    if reason is None:
        return StopReason.UNKNOWN
    return _STOP_REASON_MAP.get(reason, StopReason.UNKNOWN)


# ---------------------------------------------------------------------------
# Message serialization
# ---------------------------------------------------------------------------


def _serialize_content_block(block: ContentBlock) -> dict[str, Any] | str:
    """Convert a normalized content block to OpenAI content part."""
    match block:
        case TextBlock(text=text):
            return {"type": "text", "text": text}

        case ImageBlock(source_type=src_type, media_type=media_type, data=data):
            if src_type == "url":
                url = data
            else:
                url = f"data:{media_type};base64,{data}"
            return {"type": "image_url", "image_url": {"url": url}}

        case _:
            return {"type": "text", "text": str(block)}


def _serialize_messages(
    messages: list[Message], *, system: str | None = None
) -> list[dict[str, Any]]:
    """Convert normalized messages to OpenAI format."""
    result: list[dict[str, Any]] = []

    # System message goes first
    if system:
        result.append({"role": "system", "content": system})

    for msg in messages:
        if msg.role == Role.SYSTEM:
            # Additional system messages
            text = " ".join(
                b.text for b in msg.content if isinstance(b, TextBlock)
            )
            result.append({"role": "system", "content": text})
            continue

        if msg.role == Role.ASSISTANT:
            # Check for tool calls
            tool_calls = [b for b in msg.content if isinstance(b, ToolUseBlock)]
            text_parts = [b for b in msg.content if isinstance(b, TextBlock)]
            text = "".join(b.text for b in text_parts) or None

            if tool_calls:
                oai_tool_calls = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.input),
                        },
                    }
                    for tc in tool_calls
                ]
                msg_dict: dict[str, Any] = {
                    "role": "assistant",
                    "tool_calls": oai_tool_calls,
                }
                if text:
                    msg_dict["content"] = text
                result.append(msg_dict)
            else:
                if text:
                    result.append({"role": "assistant", "content": text})
                else:
                    # Multi-part content
                    content = [_serialize_content_block(b) for b in msg.content]
                    result.append({"role": "assistant", "content": content})
            continue

        if msg.role == Role.USER:
            # Check for tool results
            tool_results = [b for b in msg.content if isinstance(b, ToolResultBlock)]
            other_blocks = [b for b in msg.content if not isinstance(b, ToolResultBlock)]

            # Tool results become separate messages in OpenAI format
            for tr in tool_results:
                content = tr.content if isinstance(tr.content, str) else json.dumps(tr.content)
                result.append({
                    "role": "tool",
                    "tool_call_id": tr.tool_use_id,
                    "content": content,
                })

            # Remaining content as user message
            if other_blocks:
                if len(other_blocks) == 1 and isinstance(other_blocks[0], TextBlock):
                    result.append({"role": "user", "content": other_blocks[0].text})
                else:
                    content_parts = [_serialize_content_block(b) for b in other_blocks]
                    result.append({"role": "user", "content": content_parts})

    return result


def _serialize_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Kalash tool definitions to OpenAI function format."""
    oai_tools = []
    for tool in tools:
        oai_tool: dict[str, Any] = {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
            },
        }
        if "input_schema" in tool:
            oai_tool["function"]["parameters"] = tool["input_schema"]
        elif "parameters" in tool:
            oai_tool["function"]["parameters"] = tool["parameters"]
        oai_tools.append(oai_tool)
    return oai_tools


# ---------------------------------------------------------------------------
# Response deserialization
# ---------------------------------------------------------------------------


def _deserialize_response(response: Any) -> tuple[list[ContentBlock], StopReason, Usage]:
    """Extract normalized content, stop reason, and usage from an OpenAI response."""
    choice = response.choices[0]
    message = choice.message
    blocks: list[ContentBlock] = []

    # Text content
    if message.content:
        blocks.append(TextBlock(text=message.content))

    # Tool calls
    if message.tool_calls:
        for tc in message.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except (json.JSONDecodeError, TypeError):
                args = {"_raw": tc.function.arguments}

            blocks.append(ToolUseBlock(
                id=tc.id,
                name=tc.function.name,
                input=args,
            ))

    # Legacy function_call support
    if hasattr(message, "function_call") and message.function_call:
        fc = message.function_call
        try:
            args = json.loads(fc.arguments)
        except (json.JSONDecodeError, TypeError):
            args = {"_raw": fc.arguments}
        blocks.append(ToolUseBlock(
            id=f"fc_{fc.name}",
            name=fc.name,
            input=args,
        ))

    # Usage
    raw_usage = response.usage
    prompt_details = getattr(raw_usage, "prompt_tokens_details", None) if raw_usage else None
    cached_tokens = getattr(prompt_details, "cached_tokens", 0) if prompt_details else 0

    usage = Usage(
        input_tokens=raw_usage.prompt_tokens if raw_usage else 0,
        output_tokens=raw_usage.completion_tokens if raw_usage else 0,
        cache_read_tokens=cached_tokens,
        reasoning_tokens=getattr(raw_usage, "completion_tokens_details", {}).get("reasoning_tokens", 0) if raw_usage else 0,
        source="provider_reported",
        provider_raw=raw_usage.model_dump() if raw_usage and hasattr(raw_usage, "model_dump") else {},
    )

    stop_reason = _map_stop_reason(choice.finish_reason)
    return blocks, stop_reason, usage


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------


class OpenAIProvider:
    """Adapter for the OpenAI Chat Completions API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "gpt-4o",
        max_tokens: int = 4096,
        base_url: str | None = None,
        organization: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._default_max_tokens = max_tokens
        self._base_url = base_url
        self._organization = organization
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazy-initialize the OpenAI async client."""
        if self._client is None:
            import openai

            kwargs: dict[str, Any] = {}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            if self._base_url:
                kwargs["base_url"] = self._base_url
            if self._organization:
                kwargs["organization"] = self._organization
            self._client = openai.AsyncOpenAI(**kwargs)
        return self._client

    @property
    def name(self) -> str:
        return f"openai/{self._model}"

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            context_window=128_000,
            max_output_tokens=self._default_max_tokens,
            tool_use=True,
            vision=True,
            reasoning=False,
            streaming=True,
            json_mode=True,
            system_messages=True,
            stop_sequences=True,
            cache_control=False,
            parallel_tool_use=True,
        )

    @property
    def pricing(self) -> Pricing:
        # Default GPT-4o pricing
        return Pricing(
            input_per_mtok=Decimal("2.50"),
            output_per_mtok=Decimal("10.00"),
        )

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
        """Send a completion request to OpenAI."""
        client = self._get_client()

        request: dict[str, Any] = {
            "model": self._model,
            "messages": _serialize_messages(messages, system=system),
            "max_completion_tokens": max_tokens or self._default_max_tokens,
        }

        if tools:
            request["tools"] = _serialize_tools(tools)

        if temperature is not None:
            request["temperature"] = temperature

        if stop_sequences:
            request["stop"] = stop_sequences

        response = await client.chat.completions.create(**request)
        blocks, stop_reason, usage = _deserialize_response(response)

        return ModelResponse(
            id=response.id,
            model=response.model,
            content=blocks,
            stop_reason=stop_reason,
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
        """Stream a response from OpenAI."""
        client = self._get_client()

        request: dict[str, Any] = {
            "model": self._model,
            "messages": _serialize_messages(messages, system=system),
            "max_completion_tokens": max_tokens or self._default_max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        if tools:
            request["tools"] = _serialize_tools(tools)

        if temperature is not None:
            request["temperature"] = temperature

        if stop_sequences:
            request["stop"] = stop_sequences

        stream = await client.chat.completions.create(**request)

        yield MessageStart(id="", model=self._model)

        current_block_index = 0
        text_started = False
        tool_call_started: dict[int, bool] = {}
        tool_call_args: dict[int, str] = {}

        async for chunk in stream:
            if not chunk.choices:
                # Usage-only chunk at the end
                if chunk.usage:
                    yield UsageUpdate(
                        input_tokens=chunk.usage.prompt_tokens,
                        output_tokens=chunk.usage.completion_tokens,
                    )
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            # Text content
            if delta and delta.content:
                if not text_started:
                    yield BlockStart(index=current_block_index, block_type="text")
                    text_started = True
                yield BlockDelta(index=current_block_index, delta=delta.content)

            # Tool calls
            if delta and delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    tc_idx = tc_delta.index

                    if tc_idx not in tool_call_started:
                        # Close text block if open
                        if text_started:
                            yield BlockStop(index=current_block_index)
                            current_block_index += 1
                            text_started = False
                        elif tool_call_started:
                            yield BlockStop(index=current_block_index)
                            current_block_index += 1

                        tool_call_started[tc_idx] = True
                        tool_call_args[tc_idx] = ""
                        yield BlockStart(
                            index=current_block_index,
                            block_type="tool_use",
                            tool_use_id=tc_delta.id,
                            tool_name=tc_delta.function.name if tc_delta.function else None,
                        )

                    if tc_delta.function and tc_delta.function.arguments:
                        tool_call_args[tc_idx] += tc_delta.function.arguments
                        yield BlockDelta(
                            index=current_block_index,
                            delta=tc_delta.function.arguments,
                        )

            # Finish reason
            if choice.finish_reason:
                # Close any open blocks
                if text_started:
                    yield BlockStop(index=current_block_index)
                    text_started = False
                elif tool_call_started:
                    yield BlockStop(index=current_block_index)

                yield MessageStop(stop_reason=_map_stop_reason(choice.finish_reason))

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.close()
            self._client = None
