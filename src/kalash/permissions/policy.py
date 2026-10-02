"""Permission policy — 6-stage rule evaluation.

Evaluates whether an action should be ALLOWED, DENIED, or requires ASK
(user confirmation). The algorithm is ordered so that earlier stages take
precedence.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from kalash.core.events import Event, EventBus, EventType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decision and risk classification
# ---------------------------------------------------------------------------


class Decision(StrEnum):
    """Permission decision outcome."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class RiskClass(StrEnum):
    """Risk classification for tool operations."""

    READ = "read"
    WRITE = "write"
    WRITE_REMOTE = "write_remote"
    NETWORK = "network"
    DESTRUCTIVE = "destructive"


# ---------------------------------------------------------------------------
# Confirmation classes — can only ADD a prompt, never bypass
# ---------------------------------------------------------------------------


class ConfirmationClass(StrEnum):
    """Actions that always require explicit user confirmation.

    These can only ADD a confirmation prompt; they cannot be bypassed
    by grants or sandbox modes.
    """

    BULK_DESTRUCTION = "bulk_destruction"
    DESTRUCTIVE_GIT = "destructive_git"
    REMOTE_PUBLICATION = "remote_publication"
    COMMITS = "commits"
    PRODUCTION = "production"
    DATA_DESTRUCTION = "data_destruction"
    SECURITY_SURFACE = "security_surface"
    BOUNDARY_CROSSING = "boundary_crossing"
    TRUST_CONFIG = "trust_config"
    DEFERRED_AUTHORITY = "deferred_authority"


# ---------------------------------------------------------------------------
# Policy rule types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DenyRule:
    """An explicit deny rule."""

    pattern: str  # Path glob or tool name pattern
    reason: str = ""
    source: str = ""  # Where this rule came from (config, etc.)


@dataclass(frozen=True, slots=True)
class ProtectedPath:
    """A protected path or branch pattern."""

    pattern: str  # Glob pattern
    reason: str = ""


@dataclass(frozen=True, slots=True)
class PolicyRequest:
    """A request to evaluate permission for an action."""

    tool_name: str
    risk_class: RiskClass
    paths: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    command: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    confirmation_classes: list[ConfirmationClass] = field(default_factory=list)
    grant_signature: str = ""


@dataclass
class PolicyResult:
    """Result of a permission evaluation."""

    decision: Decision
    stage: int  # Which stage made the decision (1-6)
    reason: str = ""
    confirmation_classes: list[ConfirmationClass] = field(default_factory=list)
    grant_id: str | None = None  # If resolved by an existing grant


# ---------------------------------------------------------------------------
# Permission policy
# ---------------------------------------------------------------------------


@dataclass
class PermissionPolicy:
    """Evaluates permission requests through a 6-stage algorithm.

    Stages (in order of precedence):
    1. Explicit deny rules
    2. Protected paths/branches
    3. Existing grants (once/session/always)
    4. Sandbox-mode capability defaults
    5. Approval policy (risk-class → decision mapping)
    6. Confirmation classes (can only ADD a prompt, never override)
    """

    event_bus: EventBus

    # Configuration
    deny_rules: list[DenyRule] = field(default_factory=list)
    protected_paths: list[ProtectedPath] = field(default_factory=list)
    sandbox_mode: str = "workspace_write"  # From SandboxMode
    approval_mode: str = "on-request"
    protected_branches: list[str] = field(default_factory=lambda: ["main", "master", "production"])

    # Grant store reference (injected)
    _grant_store: Any = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def evaluate(self, request: PolicyRequest) -> PolicyResult:
        """Evaluate a permission request through the 6-stage algorithm.

        Args:
            request: The action requesting permission.

        Returns:
            PolicyResult with decision and metadata.
        """
        # Stage 1: Explicit deny rules
        result = self._stage_1_deny_rules(request)
        if result:
            await self._emit_decision(request, result)
            return result

        # Stage 2: Protected paths/branches
        result = self._stage_2_protected_paths(request)
        if result:
            await self._emit_decision(request, result)
            return result

        # Stage 3: Existing grants. A grant is the user having already answered
        # this exact question, so it is not re-litigated by stage 6.
        result = await self._stage_3_grants(request)
        if result:
            await self._emit_decision(request, result)
            return result

        # Stage 4: Sandbox-mode defaults
        result = self._stage_4_sandbox_defaults(request)

        # Stage 5: Approval policy (risk-class mapping), when 4 did not decide
        if result is None:
            result = self._stage_5_approval_policy(request)

        # Stage 6: Confirmation classes. This must run even when stage 4 already
        # said ALLOW — a blanket "workspace-write allows writes" is exactly the
        # case that needs a boundary-crossing or destructive-git prompt layered
        # on top. It can only escalate to ASK, never relax a decision.
        result = self._stage_6_confirmation_classes(request, result)
        if self.approval_mode == "never" and result.decision is Decision.ASK:
            result = PolicyResult(
                decision=Decision.DENY,
                stage=6,
                reason="This operation requires approval; prompting is disabled",
            )

        await self._emit_decision(request, result)
        return result

    # ------------------------------------------------------------------
    # Stage implementations
    # ------------------------------------------------------------------

    def _stage_1_deny_rules(self, request: PolicyRequest) -> PolicyResult | None:
        """Stage 1: Check explicit deny rules."""
        for rule in self.deny_rules:
            # Check tool name
            if fnmatch.fnmatch(request.tool_name, rule.pattern):
                return PolicyResult(
                    decision=Decision.DENY,
                    stage=1,
                    reason=rule.reason or f"Denied by rule: {rule.pattern}",
                )
            # Check paths
            for path in request.paths:
                if fnmatch.fnmatch(path, rule.pattern):
                    return PolicyResult(
                        decision=Decision.DENY,
                        stage=1,
                        reason=rule.reason or f"Path denied by rule: {rule.pattern}",
                    )
        return None

    def _stage_2_protected_paths(self, request: PolicyRequest) -> PolicyResult | None:
        """Stage 2: Check protected paths and branches."""
        for path in request.paths:
            for protected in self.protected_paths:
                if fnmatch.fnmatch(path, protected.pattern):
                    return PolicyResult(
                        decision=Decision.DENY,
                        stage=2,
                        reason=protected.reason or f"Protected path: {protected.pattern}",
                    )

        # Check protected branches in git commands
        if request.command and request.risk_class in (
            RiskClass.WRITE_REMOTE,
            RiskClass.DESTRUCTIVE,
        ):
            for branch in self.protected_branches:
                if branch in request.command:
                    return PolicyResult(
                        decision=Decision.ASK,
                        stage=2,
                        reason=f"Operation targets protected branch: {branch}",
                    )

        return None

    async def _stage_3_grants(self, request: PolicyRequest) -> PolicyResult | None:
        """Stage 3: Check existing grants (once/session/always)."""
        if self._grant_store is None:
            return None

        grant = await self._grant_store.find_matching(
            tool_name=request.tool_name,
            paths=request.paths,
            hosts=request.hosts,
            grant_signature=request.grant_signature or None,
        )

        if grant is None:
            return None

        # Consume 'once' grants
        if grant.get("lifetime") == "once":
            await self._grant_store.consume(grant["id"])

        # Honour the grant's own decision. Grants can be stored with
        # decision="deny", and returning ALLOW for one of those would turn a
        # standing prohibition into a standing permission.
        recorded = str(grant.get("decision", "allow")).lower()
        decision = Decision.DENY if recorded == "deny" else Decision.ALLOW
        verb = "Denied" if decision is Decision.DENY else "Allowed"

        return PolicyResult(
            decision=decision,
            stage=3,
            reason=f"{verb} by grant: {grant['id']}",
            grant_id=grant["id"],
        )

    def _stage_4_sandbox_defaults(self, request: PolicyRequest) -> PolicyResult | None:
        """Stage 4: Sandbox-mode capability defaults."""
        if (
            self.approval_mode == "untrusted"
            and self.sandbox_mode != "read_only"
            and request.risk_class is not RiskClass.READ
        ):
            return None
        match self.sandbox_mode:
            case "read_only":
                # Only allow reads
                if request.risk_class == RiskClass.READ:
                    return PolicyResult(
                        decision=Decision.ALLOW,
                        stage=4,
                        reason="READ allowed in read-only sandbox",
                    )
                return PolicyResult(
                    decision=Decision.DENY,
                    stage=4,
                    reason=f"{request.risk_class} denied in read-only sandbox",
                )

            case "workspace_write":
                # Allow read and write within workspace
                if request.risk_class in (RiskClass.READ, RiskClass.WRITE):
                    return PolicyResult(
                        decision=Decision.ALLOW,
                        stage=4,
                        reason=f"{request.risk_class} allowed in workspace-write sandbox",
                    )
                # Network and remote writes need approval
                if request.risk_class in (RiskClass.NETWORK, RiskClass.WRITE_REMOTE):
                    return None  # Fall through to stage 5

            case "danger_full_access":
                # Allow everything except destructive without ask
                if request.risk_class != RiskClass.DESTRUCTIVE:
                    return PolicyResult(
                        decision=Decision.ALLOW,
                        stage=4,
                        reason="Allowed in full-access sandbox",
                    )

        return None

    def _stage_5_approval_policy(self, request: PolicyRequest) -> PolicyResult:
        """Stage 5: Risk-class → decision mapping."""
        match request.risk_class:
            case RiskClass.READ:
                return PolicyResult(
                    decision=Decision.ALLOW,
                    stage=5,
                    reason="READ operations are allowed by default",
                )
            case RiskClass.WRITE:
                return PolicyResult(
                    decision=Decision.ASK,
                    stage=5,
                    reason="WRITE requires approval",
                )
            case RiskClass.WRITE_REMOTE:
                return PolicyResult(
                    decision=Decision.ASK,
                    stage=5,
                    reason="Remote writes require approval",
                )
            case RiskClass.NETWORK:
                return PolicyResult(
                    decision=Decision.ASK,
                    stage=5,
                    reason="Network access requires approval",
                )
            case RiskClass.DESTRUCTIVE:
                return PolicyResult(
                    decision=Decision.ASK,
                    stage=5,
                    reason="Destructive operations require approval",
                )
            case _:
                return PolicyResult(
                    decision=Decision.ASK,
                    stage=5,
                    reason="Unknown risk class — asking to be safe",
                )

    def _stage_6_confirmation_classes(
        self, request: PolicyRequest, result: PolicyResult
    ) -> PolicyResult:
        """Stage 6: Confirmation classes — can only ADD a prompt.

        If the action has confirmation classes, the result can only become
        ASK (never downgraded from ASK to ALLOW).
        """
        applicable = request.confirmation_classes

        if not applicable:
            return result

        # Confirmation classes can only upgrade to ASK, never downgrade
        if result.decision == Decision.ALLOW:
            return PolicyResult(
                decision=Decision.ASK,
                stage=6,
                reason=f"Confirmation required: {', '.join(applicable)}",
                confirmation_classes=applicable,
            )

        # Already ASK or DENY — just annotate
        result.confirmation_classes = applicable
        return result

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def _emit_decision(self, request: PolicyRequest, result: PolicyResult) -> None:
        """Emit a permission decision event."""
        await self.event_bus.emit(
            Event(
                type=EventType.PERMISSION_DECIDED,
                data={
                    "tool_name": request.tool_name,
                    "risk_class": request.risk_class,
                    "decision": result.decision,
                    "stage": result.stage,
                    "reason": result.reason,
                },
            )
        )
