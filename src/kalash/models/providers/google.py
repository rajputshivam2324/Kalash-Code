"""Google provider adapter.

Maps Kalash normalized messages to/from the Google Gemini API using google-genai.
Uses lazy import of the google.genai package to avoid import-time cost.
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
    "max_tokens": StopReason.MAX_TOKENS,
    "safety": StopReason.CONTENT_FILTER,
    "language": StopReason.CONTENT_FILTER,
}


def _map_stop_reason(reason: Any) -> StopReason:
    if not reason:
        return StopReason.UNKNOWN
    
    reason_str = str(reason).lower()
    if reason_str in _STOP_REASON_MAP:
        return _STOP_REASON_MAP[reason_str]
    # Check if reason is an enum like FinishReason.STOP
    if hasattr(reason, "name") and reason.name.lower() in _STOP_REASON_MAP:
        return _STOP_REASON_MAP[reason.name.lower()]
        
    return StopReason.UNKNOWN


# ---------------------------------------------------------------------------
# Message serialization
# ---------------------------------------------------------------------------


def _serialize_messages(
    messages: list[Message], *, system: str | None = None
) -> tuple[list[Any], Any]:
    """Convert normalized messages to Google Gemini format.
    
    Returns (contents, system_instruction).
    """
    from google.genai import types
    
    contents = []
    
    system_instruction = None
    if system:
        system_instruction = types.Content(parts=[types.Part.from_text(text=system)])

    for msg in messages:
        if msg.role == Role.SYSTEM:
            text = " ".join(
                b.text for b in msg.content if isinstance(b, TextBlock)
            )
            if not system_instruction:
                system_instruction = types.Content(parts=[types.Part.from_text(text=text)])
            else:
                system_instruction.parts.append(types.Part.from_text(text=text))
            continue

        parts = []
        for block in msg.content:
            if isinstance(block, TextBlock):
                parts.append(types.Part.from_text(text=block.text))
            elif isinstance(block, ToolUseBlock):
                sig = getattr(block, "thought_signature", None)
                if sig is not None:
                    parts.append(types.Part(
                        function_call=types.FunctionCall(name=block.name, args=block.input),
                        thought_signature=sig,
                    ))
                else:
                    parts.append(types.Part.from_function_call(name=block.name, args=block.input))
            elif isinstance(block, ToolResultBlock):
                if isinstance(block.content, str):
                    try:
                        content_dict = json.loads(block.content)
                    except json.JSONDecodeError:
                        content_dict = {"result": block.content}
                else:
                    content_dict = {"result": str(block.content)}
                parts.append(types.Part.from_function_response(name=block.tool_use_id, response=content_dict))
            elif isinstance(block, ImageBlock):
                if block.source_type == "url":
                    parts.append(types.Part.from_text(text=f"Image URL: {block.data}"))
                else:
                    parts.append(types.Part.from_text(text=f"[Image of type {block.media_type}]"))

        if not parts:
            continue
            
        role = "user" if msg.role == Role.USER else "model"
        contents.append(types.Content(role=role, parts=parts))

    return contents, system_instruction


_GEMINI_ALLOWED_SCHEMA_KEYS = frozenset({
    "type", "format", "description", "nullable", "enum",
    "properties", "required", "items"
})


def _clean_gemini_schema_dict(d: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Clean a single JSON schema object for Gemini SDK."""
    if "$ref" in d:
        ref_path = d["$ref"]
        def_name = ref_path.split("/")[-1]
        if def_name in defs:
            return _clean_gemini_schema_dict(dict(defs[def_name]), defs)

    out: dict[str, Any] = {}
    for k, v in d.items():
        if k not in _GEMINI_ALLOWED_SCHEMA_KEYS:
            continue
        if k == "properties" and isinstance(v, dict):
            out["properties"] = {
                prop_name: _clean_gemini_schema_dict(prop_val, defs) if isinstance(prop_val, dict) else prop_val
                for prop_name, prop_val in v.items()
            }
        elif k == "items" and isinstance(v, dict):
            out["items"] = _clean_gemini_schema_dict(v, defs)
        elif isinstance(v, dict):
            out[k] = _clean_gemini_schema_dict(v, defs)
        elif isinstance(v, list):
            out[k] = [
                _clean_gemini_schema_dict(item, defs) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            out[k] = v
    return out


def _sanitize_gemini_schema(raw_schema: dict[str, Any]) -> dict[str, Any]:
    """Sanitize schema for Google GenAI SDK (inlines $defs, strips forbidden keys)."""
    defs = raw_schema.get("$defs", {})
    return _clean_gemini_schema_dict(dict(raw_schema), defs)


def _serialize_tools(tools: list[dict[str, Any]]) -> list[Any]:
    """Convert Kalash tool definitions to Gemini tool format."""
    from google.genai import types

    gemini_tools = []
    function_declarations = []

    for tool in tools:
        schema = tool.get("input_schema") or tool.get("parameters") or {}
        sanitized_schema = _sanitize_gemini_schema(schema)
        func_decl = types.FunctionDeclaration(
            name=tool["name"],
            description=tool.get("description", ""),
            parameters=sanitized_schema,
        )
        function_declarations.append(func_decl)

    if function_declarations:
        gemini_tools.append(types.Tool(function_declarations=function_declarations))

    return gemini_tools


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------


class GeminiProvider:
    """Adapter for the Google Gemini API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "gemini-2.5-pro",
        max_tokens: int = 8192,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._default_max_tokens = max_tokens
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazy-initialize the Google async client."""
        if self._client is None:
            from google import genai

            kwargs: dict[str, Any] = {}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            self._client = genai.Client(**kwargs)
        return self._client

    @property
    def name(self) -> str:
        return f"google/{self._model}"

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            context_window=1_048_576,
            max_output_tokens=self._default_max_tokens,
            tool_use=True,
            vision=False,
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
        return Pricing(
            input_per_mtok=Decimal("1.25"),
            output_per_mtok=Decimal("5.00"),
        )

    def _build_config(
        self,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        stop_sequences: list[str] | None = None,
    ) -> tuple[Any, Any]:
        from google.genai import types
        
        system_instruction = None
        if system:
            system_instruction = types.Content(parts=[types.Part.from_text(text=system)])
            
        config = types.GenerateContentConfig(
            max_output_tokens=max_tokens or self._default_max_tokens,
            temperature=temperature,
            stop_sequences=stop_sequences,
            system_instruction=system_instruction,
            tools=_serialize_tools(tools) if tools else None,
            thinking_config=types.ThinkingConfig(thinking_budget=0) if hasattr(types, "ThinkingConfig") else None,
        )
        return config

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
        """Send a completion request to Gemini."""
        client = self._get_client()
        contents, msg_system = _serialize_messages(messages, system=system)
        config = self._build_config(
            system=None,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            stop_sequences=stop_sequences,
        )
        if msg_system:
            config.system_instruction = msg_system

        response = await client.aio.models.generate_content(
            model=self._model,
            contents=contents,
            config=config,
        )

        blocks: list[ContentBlock] = []
        if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.text:
                    blocks.append(TextBlock(text=part.text))
                elif part.function_call:
                    try:
                        args = part.function_call.args
                        if isinstance(args, dict):
                            pass
                        elif hasattr(args, "model_dump"):
                            args = args.model_dump()
                    except Exception:
                        args = {"_raw": str(part.function_call.args)}
                        
                    blocks.append(ToolUseBlock(
                        id=part.function_call.name,
                        name=part.function_call.name,
                        input=args or {},
                        thought_signature=getattr(part, "thought_signature", None),
                    ))

        stop_reason = StopReason.UNKNOWN
        if response.candidates and response.candidates[0].finish_reason:
            stop_reason = _map_stop_reason(response.candidates[0].finish_reason)
            if any(isinstance(b, ToolUseBlock) for b in blocks):
                stop_reason = StopReason.TOOL_USE

        usage = Usage(
            input_tokens=response.usage_metadata.prompt_token_count if response.usage_metadata else 0,
            output_tokens=response.usage_metadata.candidates_token_count if response.usage_metadata else 0,
            source="provider_reported",
            provider_raw={},
        )

        return ModelResponse(
            id="",
            model=self._model,
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
        """Stream a response from Gemini."""
        client = self._get_client()
        contents, msg_system = _serialize_messages(messages, system=system)
        config = self._build_config(
            system=None,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            stop_sequences=stop_sequences,
        )
        if msg_system:
            config.system_instruction = msg_system

        stream = await client.aio.models.generate_content_stream(
            model=self._model,
            contents=contents,
            config=config,
        )

        yield MessageStart(id="", model=self._model)

        current_block_index = 0
        text_started = False

        async for chunk in stream:
            if chunk.usage_metadata:
                yield UsageUpdate(
                    input_tokens=chunk.usage_metadata.prompt_token_count or 0,
                    output_tokens=chunk.usage_metadata.candidates_token_count or 0,
                )

            if not chunk.candidates:
                continue
                
            candidate = chunk.candidates[0]
            
            if candidate.content and candidate.content.parts:
                for part in candidate.content.parts:
                    if part.text:
                        if not text_started:
                            yield BlockStart(index=current_block_index, block_type="text")
                            text_started = True
                        yield BlockDelta(index=current_block_index, delta=part.text)
                    elif part.function_call:
                        if text_started:
                            yield BlockStop(index=current_block_index)
                            current_block_index += 1
                            text_started = False
                            
                        yield BlockStart(
                            index=current_block_index,
                            block_type="tool_use",
                            tool_use_id=part.function_call.name,
                            tool_name=part.function_call.name,
                            thought_signature=getattr(part, "thought_signature", None),
                        )
                        
                        args = part.function_call.args
                        args_json = "{}"
                        if isinstance(args, dict):
                            args_json = json.dumps(args)
                        elif hasattr(args, "model_dump"):
                            args_json = json.dumps(args.model_dump())
                        elif args:
                            args_json = json.dumps(args)
                            
                        yield BlockDelta(
                            index=current_block_index,
                            delta=args_json,
                        )
                        yield BlockStop(index=current_block_index)
                        current_block_index += 1

            if candidate.finish_reason:
                if text_started:
                    yield BlockStop(index=current_block_index)
                    text_started = False
                    
                stop_reason = _map_stop_reason(candidate.finish_reason)
                if candidate.content and candidate.content.parts and any(p.function_call for p in candidate.content.parts):
                    stop_reason = StopReason.TOOL_USE
                    
                yield MessageStop(stop_reason=stop_reason)

    async def close(self) -> None:
        """Close the HTTP client."""
        pass
