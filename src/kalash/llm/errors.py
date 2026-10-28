"""Typed provider failures, classified from HTTP status first and message text second."""

from __future__ import annotations

import email.utils
import json
import re
import time
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kalash.llm.types import Usage


class ErrorCode(StrEnum):
    AUTH = "AUTH"  # 401/403: bad or missing key
    QUOTA = "QUOTA"  # 402 / insufficient_quota: billing problem
    RATE_LIMIT = "RATE_LIMIT"  # 429
    SERVER = "SERVER"  # 5xx, overloaded
    TIMEOUT = "TIMEOUT"  # request or stream idle timeout
    TRANSPORT = "TRANSPORT"  # connection reset, stream ended early
    EMPTY_RESPONSE = "EMPTY_RESPONSE"  # completed with no content at all
    CONTEXT_OVERFLOW = "CONTEXT_OVERFLOW"  # prompt does not fit the model window
    INVALID_REQUEST = "INVALID_REQUEST"  # 400/404/422: the request itself is wrong
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"  # provider sent something we cannot parse
    INVALID_TOOL_CALL = "INVALID_TOOL_CALL"  # the model wrote a tool call the provider rejected
    CANCELLED = "CANCELLED"


RETRYABLE = frozenset(
    {
        ErrorCode.RATE_LIMIT,
        ErrorCode.SERVER,
        ErrorCode.TIMEOUT,
        ErrorCode.TRANSPORT,
        ErrorCode.EMPTY_RESPONSE,
        ErrorCode.INVALID_TOOL_CALL,  # sampling again usually fixes it (Groq failed_generation)
    }
)

OVERFLOW_PATTERN = re.compile(
    r"context[ _-]?length|context window|maximum context|prompt is too long|too many tokens"
    r"|input is too long|reduce the length|exceeds? the (?:model's )?(?:maximum|context)"
    r"|request too large|tokens? exceed",
    re.IGNORECASE,
)
_QUOTA = re.compile(
    r"insufficient[_ ]quota|insufficient[_ ]balance|insufficient[_ ]credits?"
    r"|billing[_ ](?:hard[_ ]limit|limit|quota)|credit[_ ](?:balance[_ ]exhausted|exhausted)"
    r"|(?:spend|usage)[_ ]limit|enforced_spend_limit|quota[_ ]exceeded|exceeded your current quota",
    re.IGNORECASE,
)
_QUOTA_CODES = frozenset(
    {
        "insufficient_quota",
        "insufficient_balance",
        "billing_hard_limit_reached",
        "quota_exceeded",
        "enforced_spend_limit",
        "credit_balance_exhausted",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
    }
)
_RATE_CODES = frozenset({"rate_limit_exceeded", "rate_limit_error", "rate_limited"})
_BAD_TOOL_CALL = re.compile(
    r"tool_use_failed|failed_generation|failed to call a function", re.IGNORECASE
)


class LLMError(Exception):
    def __init__(
        self,
        message: str,
        code: ErrorCode,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        provider: str | None = None,
        usage: Usage | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.retry_after = retry_after
        self.provider = provider
        self.usage = usage

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE

    def __str__(self) -> str:
        where = f"{self.provider}: " if self.provider else ""
        status = f" (HTTP {self.status})" if self.status else ""
        return f"{where}{self.code.value}{status}: {self.message}"


def classify_status(status: int, body_text: str) -> ErrorCode:
    if status in (401, 403):
        return ErrorCode.AUTH
    if status == 402:
        return ErrorCode.QUOTA
    if status == 429:
        try:
            payload = json.loads(body_text)
        except ValueError:
            payload = None
        error = payload.get("error", payload) if isinstance(payload, dict) else None
        codes = {
            value.casefold()
            for key in ("type", "code")
            if isinstance(error, dict) and isinstance(value := error.get(key), str)
        }
        if codes & _QUOTA_CODES:
            return ErrorCode.QUOTA
        if codes & _RATE_CODES:
            return ErrorCode.RATE_LIMIT
        return ErrorCode.QUOTA if _QUOTA.search(body_text) else ErrorCode.RATE_LIMIT
    if status == 400 and _BAD_TOOL_CALL.search(body_text):
        return ErrorCode.INVALID_TOOL_CALL
    if status == 413:
        return ErrorCode.CONTEXT_OVERFLOW
    if status == 408:
        return ErrorCode.TIMEOUT
    if status >= 500:
        return ErrorCode.SERVER
    if OVERFLOW_PATTERN.search(body_text):
        return ErrorCode.CONTEXT_OVERFLOW
    return ErrorCode.INVALID_REQUEST


def error_message(payload: Any, fallback: str) -> str:
    """Pull a human message out of the usual ``{"error": {"message": ...}}`` shapes."""
    if isinstance(payload, dict):
        err = payload.get("error", payload)
        if isinstance(err, dict):
            for key in ("message", "msg", "detail"):
                if isinstance(err.get(key), str):
                    return str(err[key])
        if isinstance(err, str):
            return err
        if isinstance(payload.get("message"), str):
            return str(payload["message"])
    return fallback[:2000]


def parse_retry_after(headers: Any) -> float | None:
    """Seconds to wait from ``retry-after-ms`` / ``retry-after`` (delta-seconds or HTTP date)."""
    if headers is None:
        return None
    ms = headers.get("retry-after-ms")
    if ms:
        try:
            return max(0.0, float(ms) / 1000)
        except ValueError:
            pass
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, parsed.timestamp() - time.time())


def from_http(status: int, body: bytes, headers: Any, provider: str) -> LLMError:
    text = body.decode("utf-8", "replace")
    try:
        payload: Any = json.loads(text)
    except ValueError:
        payload = None
    message = error_message(payload, text or f"HTTP {status}")
    code = classify_status(status, text)
    return LLMError(
        message, code, status=status, retry_after=parse_retry_after(headers), provider=provider
    )
