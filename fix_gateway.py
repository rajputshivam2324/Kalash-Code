import re

with open('/home/shivam/Kalash Code/src/kalash/models/gateway.py', 'r') as f:
    content = f.read()

# Add _stream_with_retry
stream_retry_code = """
    async def _stream_with_retry(
        self,
        provider: ProviderProtocol,
        messages: list[Message],
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        start = time.monotonic()
        last_exc: Exception | None = None
        
        for attempt in range(self._retry.max_attempts):
            elapsed = time.monotonic() - start
            if elapsed >= self._retry.budget_s:
                logger.warning(
                    "Retry budget exhausted for %s after %.1fs",
                    provider.name,
                    elapsed,
                )
                break
                
            try:
                stream = provider.stream(messages, **kwargs)
                async for event in stream:
                    yield event
                return
            except Exception as exc:
                last_exc = exc
                last_kind = classify_error(exc)
                
                if last_kind in (ErrorKind.AUTH, ErrorKind.PERMANENT, ErrorKind.CONTEXT_EXCEEDED):
                    raise exc
                    
                if attempt < self._retry.max_attempts - 1:
                    delay = self._retry.delay_for_attempt(attempt)
                    remaining = self._retry.budget_s - (time.monotonic() - start)
                    delay = min(delay, max(0, remaining - 0.1))
                    if delay > 0:
                        logger.info(
                            "Retrying stream %s in %.2fs (attempt %d, %s)",
                            provider.name, delay, attempt + 1, last_kind
                        )
                        import asyncio
                        await asyncio.sleep(delay)
                        
        if last_exc:
            raise last_exc
"""

content = content.replace("    # --- Retry loop for a single provider ---", "    # --- Retry loop for a single provider ---\n" + stream_retry_code)

# Replace stream mode in _attempt_with_retry
old_stream_mode = """                else:
                    stream = provider.stream(messages, **kwargs)
                    return ProviderAttemptResult(success=True, stream=stream)"""
new_stream_mode = """                else:
                    stream = self._stream_with_retry(provider, messages, **kwargs)
                    return ProviderAttemptResult(success=True, stream=stream)"""
content = content.replace(old_stream_mode, new_stream_mode)

# Add limits import and estimate tokens logic
limits_import = """
        from kalash.models import limits
        from kalash.core.budget import estimate_tokens
        
        prompt_tokens = 0
        for m in messages:
            for b in m.content:
                if hasattr(b, "text"):
                    prompt_tokens += estimate_tokens(b.text)
                elif hasattr(b, "input") and isinstance(getattr(b, "input"), dict):
                    import json
                    prompt_tokens += estimate_tokens(json.dumps(getattr(b, "input")))
                elif hasattr(b, "content") and isinstance(getattr(b, "content"), str):
                    prompt_tokens += estimate_tokens(getattr(b, "content"))
                else:
                    prompt_tokens += 50
"""

# Replace in complete
old_complete = """        call_kwargs: dict[str, Any] = {
            "system": system,
            "tools": tools,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stop_sequences": stop_sequences,
            **kwargs,
        }

        providers = [self._primary, *self._fallbacks]
        errors: list[tuple[str, Exception, str]] = []

        for provider in providers:
            result = await self._attempt_with_retry(
                provider, messages, mode="complete", **call_kwargs
            )"""

new_complete = limits_import + """
        call_kwargs: dict[str, Any] = {
            "system": system,
            "tools": tools,
            "temperature": temperature,
            "stop_sequences": stop_sequences,
            **kwargs,
        }

        providers = [self._primary, *self._fallbacks]
        errors: list[tuple[str, Exception, str]] = []

        for provider in providers:
            provider_kwargs = dict(call_kwargs)
            provider_kwargs["max_tokens"], _ = limits.fit_request_size(
                provider.name, prompt_tokens, max_tokens or 4096
            )
            result = await self._attempt_with_retry(
                provider, messages, mode="complete", **provider_kwargs
            )"""
content = content.replace(old_complete, new_complete)

# Replace in stream
old_stream = """        call_kwargs: dict[str, Any] = {
            "system": system,
            "tools": tools,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stop_sequences": stop_sequences,
            **kwargs,
        }

        providers = [self._primary, *self._fallbacks]
        errors: list[tuple[str, Exception, str]] = []

        for provider in providers:
            result = await self._attempt_with_retry(
                provider, messages, mode="stream", **call_kwargs
            )"""
new_stream = limits_import + """
        call_kwargs: dict[str, Any] = {
            "system": system,
            "tools": tools,
            "temperature": temperature,
            "stop_sequences": stop_sequences,
            **kwargs,
        }

        providers = [self._primary, *self._fallbacks]
        errors: list[tuple[str, Exception, str]] = []

        for provider in providers:
            provider_kwargs = dict(call_kwargs)
            provider_kwargs["max_tokens"], _ = limits.fit_request_size(
                provider.name, prompt_tokens, max_tokens or 4096
            )
            result = await self._attempt_with_retry(
                provider, messages, mode="stream", **provider_kwargs
            )"""
content = content.replace(old_stream, new_stream)

with open('/home/shivam/Kalash Code/src/kalash/models/gateway.py', 'w') as f:
    f.write(content)

print("Done")
