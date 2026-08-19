import re

with open("src/kalash/models/providers/openai_compatible.py") as f:
    code = f.read()

# For complete
complete_old = """    async def complete(
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
        \"\"\"Send completion, stripping unsupported features.\"\"\"
        # Don't send tools if provider doesn't support them
        effective_tools = tools if self._tool_use else None"""

complete_new = """    async def complete(
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
        \"\"\"Send completion, stripping unsupported features.\"\"\"
        # Don't send tools if provider doesn't support them
        effective_tools = tools if self._tool_use else None
        
        if tools and not self._tool_use:
            note = "\\n\\n[NOTE: Tool calling is not available for this model. You must respond with plain text only and cannot execute functions.]"
            system = (system or "") + note"""

code = code.replace(complete_old, complete_new)

# For stream
stream_old = """        # Normal streaming
        effective_tools = tools if self._tool_use else None

        async for event in super().stream("""

stream_new = """        # Normal streaming
        effective_tools = tools if self._tool_use else None
        if tools and not self._tool_use:
            note = "\\n\\n[NOTE: Tool calling is not available for this model. You must respond with plain text only and cannot execute functions.]"
            system = (system or "") + note

        async for event in super().stream("""

code = code.replace(stream_old, stream_new)

with open("src/kalash/models/providers/openai_compatible.py", "w") as f:
    f.write(code)
