"""Sarvam Chat Completions; V1 flagship models and V2 open-weight routes.

Contract: https://docs.sarvam.ai/api/api-guides-tutorials/chat-completion/overview
V2 access is granted per API key; selecting a model does not grant beta access.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from kalash.core.budget import Pricing
from kalash.models.normalize import Message, ModelCapabilities, Role, TextBlock, ThinkingBlock
from kalash.models.providers.openai_compatible import OpenAICompatibleProvider


@dataclass(frozen=True)
class SarvamModel:
    version: str
    context_window: int
    reasoning: bool = True
    vision: bool = False


SARVAM_MODELS = {
    "sarvam-105b": SarvamModel("v1", 128_000),
    "sarvam-105b-conversations": SarvamModel("v1", 32_000),
    "glm5.3": SarvamModel("v2", 1_048_576),
    "gemma4": SarvamModel("v2", 131_072, reasoning=False, vision=True),
    "deepseekv4-flash": SarvamModel("v2", 1_048_576),
}


class SarvamProvider(OpenAICompatibleProvider):
    def __init__(
        self,
        *,
        model: str = "sarvam-105b",
        api_key: str | None = None,
        base_url: str = "https://api.sarvam.ai",
        max_tokens: int = 32_768,
        reasoning_effort: str | None = "auto",
        pricing: Pricing | None = None,
    ) -> None:
        if model not in SARVAM_MODELS:
            raise ValueError(
                f"Unknown Sarvam model {model!r}; choose one of {', '.join(SARVAM_MODELS)}"
            )
        profile = SARVAM_MODELS[model]
        if reasoning_effort == "auto":
            reasoning_effort = "high" if profile.version == "v1" else "max"
        allowed_efforts = (
            {None, "low", "medium", "high"}
            if profile.version == "v1"
            else {None, "low", "high", "max"}
        )
        if reasoning_effort not in allowed_efforts:
            raise ValueError(
                f"Sarvam {profile.version.upper()} does not support reasoning_effort={reasoning_effort!r}; "
                + (
                    "use low, medium, high or None"
                    if profile.version == "v1"
                    else "use low, high or max"
                )
            )
        if profile.version == "v2" and profile.reasoning and reasoning_effort is None:
            raise ValueError("Sarvam V2 reasoning models require low, high or max effort")
        super().__init__(
            base_url=f"{base_url.rstrip('/')}/{profile.version}",
            model=model,
            api_key=api_key,
            max_tokens=max_tokens,
            context_window=profile.context_window,
            provider_name="sarvam",
            tool_use=True,
            vision=profile.vision,
            json_mode=True,
        )
        self.profile = profile
        self.reasoning_effort = reasoning_effort
        self._configured_pricing = pricing
        self.default_output_tokens = max_tokens

    def _get_client(self) -> Any:
        if self._client is None:
            import openai

            self._client = openai.AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                default_headers={"api-subscription-key": self._api_key or ""},
                max_retries=0,
            )
        return self._client

    @property
    def capabilities(self) -> ModelCapabilities:
        return replace(
            super().capabilities,
            reasoning=self.profile.reasoning and self.reasoning_effort is not None,
            max_output_tokens=self.profile.context_window,
            parallel_tool_use=self.profile.version == "v2",
        )

    @property
    def pricing(self) -> Pricing:
        return self._configured_pricing or super().pricing

    def serialize_messages(
        self, messages: list[Message], system: str | None
    ) -> list[dict[str, Any]]:
        wire = super().serialize_messages(messages, system)
        assistants = iter(
            message
            for message in messages
            if message.role == Role.ASSISTANT
            and any(
                isinstance(block, TextBlock) or getattr(block, "name", None)
                for block in message.content
            )
        )
        for item in wire:
            if item["role"] == "assistant":
                message = next(assistants)
                item["content"] = "".join(
                    block.text for block in message.content if isinstance(block, TextBlock)
                )
                if self.capabilities.reasoning:
                    item["reasoning_content"] = "".join(
                        block.thinking
                        for block in message.content
                        if isinstance(block, ThinkingBlock)
                    )
        return wire

    def _request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        request = super()._request(*args, **kwargs)
        if self.profile.reasoning:
            # None is an explicit V1 disable; omitting it would enable thinking.
            request["reasoning_effort"] = self.reasoning_effort
        if request.get("stop") and len(request["stop"]) > 4:
            raise ValueError("Sarvam accepts at most four stop sequences")
        return request
