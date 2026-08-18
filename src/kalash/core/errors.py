"""Exception taxonomy for Kalash.

Every error derives from KalashError with a stable code.
Never swallow an exception without logging it with context.
"""

from __future__ import annotations


class KalashError(Exception):
    """Base exception for all Kalash errors."""

    code: str = "KALASH_INTERNAL"

    def __init__(self, message: str, *, code: str | None = None, recoverable: bool = False) -> None:
        super().__init__(message)
        if code:
            self.code = code
        self.recoverable = recoverable
        self.message = message


# --- Config errors ---


class ConfigError(KalashError):
    """Configuration-related errors."""

    code = "KALASH_CONFIG_ERROR"


class ConfigNotFoundError(ConfigError):
    """A required config file was not found."""

    code = "KALASH_CONFIG_NOT_FOUND"


class ConfigScopeIgnoredError(ConfigError):
    """A security-relevant key found in project scope was ignored."""

    code = "KALASH_CONFIG_SCOPE_IGNORED"


# --- Model errors ---


class ModelError(KalashError):
    """Model gateway errors."""

    code = "KALASH_MODEL_ERROR"


class ModelContextExceededError(ModelError):
    """Assembled context exceeds the model's window."""

    code = "KALASH_MODEL_CONTEXT_EXCEEDED"


class ModelAllFailedError(ModelError):
    """All models (primary + fallback) failed."""

    code = "KALASH_MODEL_ALL_FAILED"


class ModelContextWindowTooSmallError(ModelError):
    """Model's context window is too small for Kalash."""

    code = "KALASH_CONTEXT_WINDOW_TOO_SMALL"


# --- Tool errors ---


class ToolError(KalashError):
    """Tool-related errors."""

    code = "KALASH_TOOL_ERROR"


class ToolUnknownError(ToolError):
    """Unknown tool name."""

    code = "KALASH_TOOL_UNKNOWN"


class ToolInvalidArgsError(ToolError):
    """Schema violation in tool arguments."""

    code = "KALASH_TOOL_INVALID_ARGS"


class ToolMalformedArgsError(ToolError):
    """Unparseable JSON in tool arguments."""

    code = "KALASH_TOOL_MALFORMED_ARGS"


class ToolTimeoutError(ToolError):
    """Tool execution timed out."""

    code = "KALASH_TOOL_TIMEOUT"


class ToolStaleReadError(ToolError):
    """File changed since last read."""

    code = "KALASH_TOOL_STALE_READ"


class ToolCancelledError(ToolError):
    """Tool execution was cancelled."""

    code = "KALASH_CANCELLED"


# --- Permission errors ---


class PermissionError_(KalashError):
    """Permission-related errors."""

    code = "KALASH_PERMISSION_DENIED"


class ApprovalRequiredError(PermissionError_):
    """Unattended run needs approval."""

    code = "KALASH_APPROVAL_REQUIRED"


class ApprovalExpiredError(PermissionError_):
    """Approval prompt timed out."""

    code = "KALASH_APPROVAL_EXPIRED"


class ApprovalAbandonedError(PermissionError_):
    """Approval prompt was abandoned (terminal disconnect)."""

    code = "KALASH_APPROVAL_ABANDONED"


# --- Sandbox errors ---


class SandboxError(KalashError):
    """Sandbox-related errors."""

    code = "KALASH_SANDBOX_ERROR"


class SandboxPathDeniedError(SandboxError):
    """Path is outside writable roots."""

    code = "KALASH_SANDBOX_PATH_DENIED"


class SandboxProtectedPathError(SandboxError):
    """Path is protected (I-010)."""

    code = "KALASH_SANDBOX_PROTECTED_PATH"


class SandboxUnavailableError(SandboxError):
    """Sandbox backend cannot be established (I-011)."""

    code = "KALASH_SANDBOX_UNAVAILABLE"


# --- Memory errors ---


class MemoryError_(KalashError):
    """Memory-related errors."""

    code = "KALASH_MEMORY_ERROR"


class UnsupportedCapabilityError(MemoryError_):
    """Provider does not support requested capability."""

    code = "KALASH_MEMORY_UNSUPPORTED_CAPABILITY"


# --- Storage errors ---


class StorageError(KalashError):
    """Storage-related errors."""

    code = "KALASH_STORAGE_ERROR"


class MigrationError(StorageError):
    """Database migration failed."""

    code = "KALASH_STORAGE_MIGRATION_ERROR"


# --- Hook errors ---


class HookError(KalashError):
    """Hook-related errors."""

    code = "KALASH_HOOK_ERROR"


class HookDeniedError(HookError):
    """PreToolUse hook denied the call."""

    code = "KALASH_HOOK_DENIED"


class TrustInvalidatedError(HookError):
    """Trust was invalidated due to content hash change."""

    code = "KALASH_TRUST_INVALIDATED"


# --- Session errors ---


class SessionError(KalashError):
    """Session-related errors."""

    code = "KALASH_SESSION_ERROR"


class SessionClosedError(SessionError):
    """Operation on a closed session."""

    code = "KALASH_SESSION_CLOSED"


# --- State errors ---


class StateError(KalashError):
    """State machine violations."""

    code = "KALASH_STATE_ILLEGAL_TRANSITION"


# --- Budget errors ---


class BudgetError(KalashError):
    """Budget ceiling exceeded."""

    code = "KALASH_BUDGET_EXCEEDED"


class BudgetImmutableError(BudgetError):
    """Unattended run cannot raise budget."""

    code = "KALASH_BUDGET_IMMUTABLE"


class BudgetUnboundedError(BudgetError):
    """Unattended run has no finite ceiling."""

    code = "KALASH_BUDGET_UNBOUNDED"


class ContextUnsatisfiableError(BudgetError):
    """Cannot fit guaranteed slots in the context window."""

    code = "KALASH_CONTEXT_UNSATISFIABLE"
