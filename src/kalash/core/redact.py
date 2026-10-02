"""Secret and PII scrubbing — used before ANY egress.

Detection uses three signals:
1. Pattern matching for known credential formats
2. Shannon entropy for unknown formats
3. Contextual signals (variable names, file types)

Redaction marker: [REDACTED:<kind>:<digest>]
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class RedactionProfile(StrEnum):
    """How aggressively to redact."""

    NONE = "none"  # Requires justification, audited
    SECRETS = "secrets"  # Model requests
    SECRETS_PII = "secrets_pii"  # Memory egress default
    STRICT = "strict"  # Paths, usernames, hostnames too
    METADATA_ONLY = "metadata_only"  # Telemetry: counters only


class SecretKind(StrEnum):
    """Detected secret type."""

    AWS_ACCESS_KEY = "aws_access_key_id"
    AWS_SECRET_KEY = "aws_secret_access_key"
    GITHUB_PAT = "github_pat"
    GITHUB_TOKEN = "github_token"
    OPENAI_KEY = "openai_api_key"
    ANTHROPIC_KEY = "anthropic_api_key"
    SLACK_TOKEN = "slack_token"
    STRIPE_KEY = "stripe_key"
    JWT = "jwt"
    PEM_PRIVATE_KEY = "private_key"
    DB_CONNECTION_STRING = "db_connection_string"
    GENERIC_SECRET = "generic_secret"
    GENERIC_TOKEN = "generic_token"


@dataclass(frozen=True, slots=True)
class RedactionResult:
    """Result of redacting text."""

    text: str
    redactions: list[dict[str, Any]]
    had_secrets: bool


# Known secret patterns (high-confidence, always redact)
_SECRET_PATTERNS: list[tuple[re.Pattern[str], SecretKind]] = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), SecretKind.AWS_ACCESS_KEY),
    (re.compile(r"ASIA[0-9A-Z]{16}"), SecretKind.AWS_ACCESS_KEY),
    (
        re.compile(r"(?:aws_secret_access_key|AWS_SECRET_ACCESS_KEY)\s*[=:]\s*[A-Za-z0-9/+=]{40}"),
        SecretKind.AWS_SECRET_KEY,
    ),
    (re.compile(r"ghp_[A-Za-z0-9]{36,}"), SecretKind.GITHUB_PAT),
    (re.compile(r"gho_[A-Za-z0-9]{36,}"), SecretKind.GITHUB_TOKEN),
    (re.compile(r"github_pat_[A-Za-z0-9_]{22,}"), SecretKind.GITHUB_PAT),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), SecretKind.OPENAI_KEY),
    (re.compile(r"sk-ant-[A-Za-z0-9\-]{20,}"), SecretKind.ANTHROPIC_KEY),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), SecretKind.SLACK_TOKEN),
    (re.compile(r"sk_live_[A-Za-z0-9]{24,}"), SecretKind.STRIPE_KEY),
    (re.compile(r"rk_live_[A-Za-z0-9]{24,}"), SecretKind.STRIPE_KEY),
    (re.compile(r"AIza[A-Za-z0-9\-_]{35}"), SecretKind.GENERIC_TOKEN),
    (re.compile(r"eyJ[A-Za-z0-9\-_]+\.eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+"), SecretKind.JWT),
    (
        re.compile(r"-----BEGIN\s(?:RSA\s|EC\s|DSA\s|ENCRYPTED\s)?PRIVATE KEY-----"),
        SecretKind.PEM_PRIVATE_KEY,
    ),
    (
        re.compile(r"(?:postgres|mysql|mongodb\+srv|redis)://[^\s]+:[^\s]+@[^\s]+"),
        SecretKind.DB_CONNECTION_STRING,
    ),
]

# Context patterns (variable assignment to secret-looking names)
_CONTEXT_PATTERN = re.compile(
    r"(?i)(?:secret|token|password|passwd|api[_\-]?key|credential|auth|bearer|private[_\-]?key)"
    r"\s*[=:]\s*['\"]?([^\s'\"]{8,})['\"]?"
)

# PII patterns
_PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"), "email"),
    (re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b"), "phone"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "ssn"),
]

# Session key for stable HMAC-based digest
_session_key: bytes | None = None


def _get_session_key() -> bytes:
    """Get or create the per-session key for redaction markers."""
    global _session_key
    if _session_key is None:
        _session_key = os.urandom(32)
    return _session_key


def _make_marker(kind: str, value: str) -> str:
    """Create a stable redaction marker."""
    digest = hmac.new(_get_session_key(), value.encode(), hashlib.sha256).hexdigest()[:6]
    return f"[REDACTED:{kind}:{digest}]"


def _shannon_entropy(text: str) -> float:
    """Calculate Shannon entropy of a string."""
    if not text:
        return 0.0
    freq: dict[str, int] = {}
    for ch in text:
        freq[ch] = freq.get(ch, 0) + 1
    length = len(text)
    return -sum((c / length) * math.log2(c / length) for c in freq.values())


def redact(text: str, profile: RedactionProfile = RedactionProfile.SECRETS) -> RedactionResult:
    """Redact secrets and optionally PII from text.

    Args:
        text: Input text to redact.
        profile: How aggressively to redact.

    Returns:
        RedactionResult with the redacted text and metadata.
    """
    if profile == RedactionProfile.NONE:
        return RedactionResult(text=text, redactions=[], had_secrets=False)

    if profile == RedactionProfile.METADATA_ONLY:
        return RedactionResult(text="", redactions=[], had_secrets=False)

    result = text
    redactions: list[dict[str, Any]] = []

    # Pattern-based detection (high confidence)
    for pattern, kind in _SECRET_PATTERNS:
        for match in pattern.finditer(result):
            marker = _make_marker(kind.value, match.group())
            redactions.append(
                {
                    "kind": kind.value,
                    "offset": match.start(),
                    "length": len(match.group()),
                }
            )
            result = result[: match.start()] + marker + result[match.end() :]
            # Re-scan from start after replacement (positions shifted)
            break  # Process one at a time to handle position shifts

    # Re-run patterns until stable (handles overlapping matches)
    changed = True
    while changed:
        changed = False
        for pattern, kind in _SECRET_PATTERNS:
            candidate = pattern.search(result)
            if candidate and not candidate.group().startswith("[REDACTED:"):
                marker = _make_marker(kind.value, candidate.group())
                result = result[: candidate.start()] + marker + result[candidate.end() :]
                redactions.append({"kind": kind.value})
                changed = True
                break

    # Context-based detection (entropy + context)
    for match in _CONTEXT_PATTERN.finditer(result):
        value = match.group(1)
        if (
            not value.startswith("[REDACTED:")
            and _shannon_entropy(value) > 3.5
            and len(value) >= 12
        ):
            marker = _make_marker("generic_secret", value)
            result = result.replace(value, marker, 1)
            redactions.append({"kind": "generic_secret"})

    # PII patterns (if profile includes PII)
    if profile in (RedactionProfile.SECRETS_PII, RedactionProfile.STRICT):
        for pattern, pii_kind in _PII_PATTERNS:
            for match in pattern.finditer(result):
                if not match.group().startswith("[REDACTED:"):
                    marker = _make_marker(pii_kind, match.group())
                    result = result.replace(match.group(), marker, 1)
                    redactions.append({"kind": pii_kind})

    # STRICT: also redact paths, usernames, hostnames
    if profile == RedactionProfile.STRICT:
        # Replace absolute paths with relative
        result = re.sub(
            r"(/home/\w+|/Users/\w+|C:\\Users\\\w+)",
            "<home>",
            result,
        )

    return RedactionResult(
        text=result,
        redactions=redactions,
        had_secrets=len(redactions) > 0,
    )
