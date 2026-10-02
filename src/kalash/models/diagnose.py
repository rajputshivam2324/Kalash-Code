"""Translate provider errors into something actionable.

Provider failures were surfaced by dumping the raw API JSON into the transcript::

    Error code: 413 - {'error': {'message': 'Request too large for model
    `openai/gpt-oss-20b` in organization `org_01k...` service tier `on_demand`
    on tokens per minute (TPM): Limit 8000, Requested 11220, please reduce your
    message size and try again. ...', 'type': 'tokens', 'code':
    'rate_limit_exceeded'}}

That tells a user nothing they can act on, and buries the one fact that matters:
the request was 3,220 tokens over a per-minute ceiling. This module extracts the
cause and states the remedy.

Each entry also reports whether the condition is *retryable after adjustment*,
so the caller can fix the request and try again instead of failing the turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# "Limit 8000, Requested 11220"
_TPM = re.compile(r"limit\s+(\d[\d,]*)[^\d]+requested\s+(\d[\d,]*)", re.IGNORECASE)
# "must be less than or equal to 4096"
_MAX_OUT = re.compile(r"less than or equal to\s+(\d[\d,]*)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """A provider error, explained."""

    summary: str
    """One line, safe to show a user."""

    remedy: str = ""
    """What to do about it."""

    kind: str = "unknown"
    """Machine-readable cause: tpm | max_output | no_tools | invalid_tool | auth | context | rate | unknown."""

    adjust_max_output: int | None = None
    """When set, the request can be retried with this output cap."""

    reduce_input: bool = False
    """When true, the input itself must shrink before a retry can succeed."""

    tpm_limit: int | None = None
    """Observed per-minute ceiling from a provider error, when parseable."""

    requested_total: int | None = None
    """Observed prompt+output total from a provider error, when parseable."""

    @property
    def retryable(self) -> bool:
        return (
            self.adjust_max_output is not None or self.reduce_input or self.kind == "invalid_tool"
        )


def _number(text: str) -> int:
    return int(text.replace(",", ""))


def diagnose(raw: str, model_id: str = "") -> Diagnosis:
    """Explain a provider error string."""
    text = (raw or "").strip()
    if not text:
        return Diagnosis(summary="The provider failed without a message.")

    lowered = text.lower()
    label = model_id or "this model"

    # Output cap exceeded. Deterministic and fixable without user input.
    if "max_completion_tokens" in lowered or "max_tokens" in lowered:
        if match := _MAX_OUT.search(text):
            cap = _number(match.group(1))
            return Diagnosis(
                summary=f"{label} accepts at most {cap:,} output tokens.",
                remedy=f"Reducing the reply reservation to {cap:,} and retrying.",
                kind="max_output",
                adjust_max_output=cap,
            )
        return Diagnosis(
            summary=f"{label} rejected the output token reservation.",
            remedy="Lower max_tokens for this model.",
            kind="max_output",
            adjust_max_output=1024,
        )

    # Throughput ceiling. Providers count prompt + reservation against TPM.
    if (
        "tokens per minute" in lowered
        or "tpm" in lowered
        or "413" in lowered
        or "request too large" in lowered
    ):
        if match := _TPM.search(text):
            limit, requested = _number(match.group(1)), _number(match.group(2))
            over = requested - limit
            return Diagnosis(
                summary=(
                    f"{label} allows {limit:,} tokens per minute; this request "
                    f"needed {requested:,} ({over:,} over)."
                ),
                remedy=(
                    "Fewer tools and a smaller reply reservation, or a model with "
                    "more throughput. /tools shows what is currently loaded."
                ),
                kind="tpm",
                reduce_input=True,
                tpm_limit=limit,
                requested_total=requested,
            )
        return Diagnosis(
            summary=f"{label} hit a per-minute token ceiling.",
            remedy="Wait a moment, or switch to a model with more throughput.",
            kind="tpm",
            reduce_input=True,
        )

    # Model cannot accept a tools array at all.
    if "tool" in lowered and ("not supported" in lowered or "unsupported" in lowered):
        return Diagnosis(
            summary=f"{label} does not support tool calling.",
            remedy=(
                "Kalash needs tools to edit files or run commands. Switch models "
                "with /models — this one can only hold a conversation."
            ),
            kind="no_tools",
        )

    # Model failed to parse tool call arguments (Groq/OpenAI server-side validation)
    if (
        "failed to parse tool call" in lowered
        or "parse tool call arguments" in lowered
        or "failed to call a function" in lowered
        or "failed_generation" in lowered
        or "cutoff by max_tokens" in lowered
    ):
        return Diagnosis(
            summary=f"{label} emitted malformed tool arguments or failed function calling format.",
            remedy="Formatting tool arguments cleanly. Please emit one tool call at a time with properly escaped JSON.",
            kind="invalid_tool",
            adjust_max_output=4096,
        )

    # Model hallucinated a tool name not present in the request schema.
    if "tool call validation failed" in lowered or "not in request.tools" in lowered:
        bad = "unknown"
        if (match := re.search(r"tool '([^']+)'", text, re.IGNORECASE)) or (
            match := re.search(r'tool "([^"]+)"', text, re.IGNORECASE)
        ):
            bad = match.group(1)
        return Diagnosis(
            summary=f"The model tried to call `{bad}`, which is not loaded.",
            remedy=(
                f"Use only the tools listed in the system prompt. "
                f"Do not invent tool names like `{bad}`."
            ),
            kind="invalid_tool",
        )

    if "context" in lowered and ("length" in lowered or "window" in lowered):
        return Diagnosis(
            summary=f"The conversation exceeded {label}'s context window.",
            remedy="Run /clear to start a fresh context, or switch to a larger model.",
            kind="context",
            reduce_input=True,
        )

    if any(
        marker in lowered
        for marker in ("invalid api key", "unauthorized", "authentication", "401", "403")
    ):
        return Diagnosis(
            summary="The provider rejected the API key.",
            remedy="Re-connect with /connect.",
            kind="auth",
        )

    if "rate limit" in lowered or "429" in lowered:
        return Diagnosis(
            summary=f"{label} is rate limited right now.",
            remedy="Wait a few seconds and retry.",
            kind="rate",
        )

    if "insufficient" in lowered and "quota" in lowered:
        return Diagnosis(
            summary="The account has no remaining quota for this model.",
            remedy="Check billing, or switch providers with /connect.",
            kind="quota",
        )

    # Unrecognized. Return the provider's own message, trimmed of the JSON
    # wrapper so at least the sentence is readable.
    return Diagnosis(summary=_readable(text), kind="unknown")


def _readable(text: str) -> str:
    """Pull the human sentence out of a JSON-ish provider error."""
    if match := re.search(r"'message':\s*'([^']+)'", text):
        return match.group(1)
    if match := re.search(r'"message":\s*"([^"]+)"', text):
        return match.group(1)
    return text[:400]


def explain(raw: str, model_id: str = "") -> str:
    """One- or two-line explanation suitable for a transcript."""
    result = diagnose(raw, model_id)
    return f"{result.summary} {result.remedy}".strip()
