"""OpenAI-compatible provider adapter.

Works with any endpoint implementing the OpenAI chat completions API:
Ollama, vLLM, Together AI, Groq, Fireworks, LM Studio, etc.

Uses conservative defaults since we can't know provider capabilities.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from collections.abc import AsyncIterator

from kalash.core.budget import Pricing, Usage

from ..normalize import (
    Message,
    ModelCapabilities,
    ModelResponse,
    StreamEvent,
)
from .openai import OpenAIProvider

logger = logging.getLogger(__name__)


class OpenAICompatibleProvider(OpenAIProvider):
    """Adapter for any OpenAI-compatible API endpoint.

    Inherits the full OpenAI serialization logic but allows custom
    base_url and uses conservative capability defaults.

    Usage:
        provider = OpenAICompatibleProvider(
            base_url="http://localhost:11434/v1",  # Ollama
            model="llama3.1:70b",
            api_key="ollama",  # some endpoints require a dummy key
        )
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        max_tokens: int = 4096,
        context_window: int = 8192,
        tool_use: bool = False,
        vision: bool = False,
        streaming: bool = True,
        json_mode: bool = False,
        organization: str | None = None,
        provider_name: str | None = None,
        input_price_per_mtok: Decimal | None = None,
        output_price_per_mtok: Decimal | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key or "not-needed",
            model=model,
            max_tokens=max_tokens,
            base_url=base_url,
            organization=organization,
        )
        self._context_window = context_window
        self._tool_use = tool_use
        self._vision = vision
        self._streaming = streaming
        self._json_mode = json_mode
        self._provider_name = provider_name
        self._input_price = input_price_per_mtok
        self._output_price = output_price_per_mtok

    @property
    def name(self) -> str:
        prefix = self._provider_name or "compatible"
        return f"{prefix}/{self._model}"

    @property
    def capabilities(self) -> ModelCapabilities:
        """Conservative defaults — override via constructor."""
        return ModelCapabilities(
            context_window=self._context_window,
            max_output_tokens=self._default_max_tokens,
            tool_use=self._tool_use,
            vision=self._vision,
            reasoning=False,
            streaming=self._streaming,
            json_mode=self._json_mode,
            system_messages=True,
            stop_sequences=True,
            cache_control=False,
            parallel_tool_use=False,
        )

    @property
    def pricing(self) -> Pricing:
        """Pricing may be unknown for self-hosted models."""
        return Pricing(
            input_per_mtok=self._input_price or Decimal("0"),
            output_per_mtok=self._output_price or Decimal("0"),
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
        """Send completion, stripping unsupported features."""
        # Don't send tools if provider doesn't support them
        effective_tools = tools if self._tool_use else None

        return await super().complete(
            messages,
            system=system,
            tools=effective_tools,
            max_tokens=max_tokens,
            temperature=temperature,
            stop_sequences=stop_sequences,
            **kwargs,
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
        """Stream, stripping unsupported features."""
        if not self._streaming:
            # Fall back to non-streaming complete + synthesize events
            response = await self.complete(
                messages,
                system=system,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
                stop_sequences=stop_sequences,
                **kwargs,
            )
            # Synthesize stream events from the complete response
            from ..normalize import (
                BlockDelta,
                BlockStart,
                BlockStop,
                MessageStart,
                MessageStop,
                TextBlock,
                ToolUseBlock,
                UsageUpdate,
            )
            import json

            yield MessageStart(id=response.id, model=response.model)

            for idx, block in enumerate(response.content):
                if isinstance(block, TextBlock):
                    yield BlockStart(index=idx, block_type="text")
                    yield BlockDelta(index=idx, delta=block.text)
                    yield BlockStop(index=idx)
                elif isinstance(block, ToolUseBlock):
                    yield BlockStart(
                        index=idx,
                        block_type="tool_use",
                        tool_use_id=block.id,
                        tool_name=block.name,
                    )
                    yield BlockDelta(index=idx, delta=json.dumps(block.input))
                    yield BlockStop(index=idx)

            if response.usage:
                yield UsageUpdate(
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                )

            yield MessageStop(stop_reason=response.stop_reason)
            return

        # Normal streaming
        effective_tools = tools if self._tool_use else None

        async for event in super().stream(
            messages,
            system=system,
            tools=effective_tools,
            max_tokens=max_tokens,
            temperature=temperature,
            stop_sequences=stop_sequences,
            **kwargs,
        ):
            yield event
