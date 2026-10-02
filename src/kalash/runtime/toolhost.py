"""Tool schemas, permission checks, execution context and bounded observations."""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from kalash.core.config import KalashConfig
from kalash.core.events import Event, EventBus, EventType
from kalash.models.limits import supports_tools
from kalash.permissions.classify import ToolRisk, classify_tool_call
from kalash.permissions.policy import (
    Decision,
    PermissionPolicy,
    PolicyRequest,
    RiskClass,
)
from kalash.permissions.prompt import ApprovalPrompt, ApprovalResponse, PromptContext
from kalash.runtime.instructions import InstructionLoader
from kalash.runtime.scratchpad import get_scratchpad
from kalash.tools.base import SideEffect, ToolContext, ToolEnvelope
from kalash.tools.registry import ToolRegistry
from kalash.tools.schema import tool_schema

logger = logging.getLogger(__name__)


class ToolCategory(StrEnum):
    """Tool concurrency category. Read-only calls may overlap; the rest serialize."""

    READ = "read"
    WRITE = "write"
    EXEC = "exec"


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    content: str
    is_error: bool = False


class ToolHostProtocol(Protocol):
    """What the agent loop requires of a tool host."""

    async def execute(self, name: str, arguments: dict[str, Any], *, tool_use_id: str) -> str: ...

    def category(self, name: str) -> ToolCategory: ...

    def schemas(self) -> list[dict[str, Any]]: ...


# Tools whose output is bulky and re-derivable, so deferring it is safe. Reads
# and edits are deliberately absent: an agent about to make an exact-match edit
# needs the file body it just read to be present in full, and truncating it
# would produce edits that fail to apply.
DEFERRABLE: frozenset[str] = frozenset({"shell", "search", "fetch", "glob", "list"})

# Below this, inlining is cheaper than a headline plus a later expand round trip.
DEFER_THRESHOLD_BYTES = 6144

# Lines of the deferred body kept inline, so the agent can usually act without
# expanding at all.
DEFER_HEAD_LINES = 24

# Capabilities granted per sandbox mode. Keys match what the tools declare.
_MODE_CAPABILITIES: dict[str, frozenset[str]] = {
    "read_only": frozenset({"fs.read", "memory.read"}),
    "workspace_write": frozenset(
        {
            "fs.read",
            "fs.write",
            "shell.exec",
            "network.fetch",
            "network.search",
            "network.write",
            "memory.read",
            "memory.write",
            "task.spawn",
        }
    ),
    "danger_full_access": frozenset(
        {
            "fs.read",
            "fs.write",
            "shell.exec",
            "network.fetch",
            "network.search",
            "network.write",
            "memory.read",
            "memory.write",
            "task.spawn",
        }
    ),
}

_WRITE_RISKS = (RiskClass.WRITE, RiskClass.WRITE_REMOTE, RiskClass.DESTRUCTIVE)

# Programs whose whole purpose involves the network. A command reaching the
# executor having already passed the gate should not then be silently starved of
# the one resource it needs.
_NETWORK_PROGRAMS: frozenset[str] = frozenset(
    {
        "npm",
        "npx",
        "pnpm",
        "yarn",
        "bun",
        "deno",
        "pip",
        "pip3",
        "uv",
        "uvx",
        "poetry",
        "pipx",
        "cargo",
        "go",
        "gem",
        "bundle",
        "composer",
        "mvn",
        "gradle",
        "git",
        "curl",
        "wget",
        "ssh",
        "scp",
        "rsync",
        "docker",
        "podman",
        "kubectl",
        "helm",
        "terraform",
        "apt",
        "apt-get",
        "dnf",
        "brew",
        "apk",
    }
)


def _needs_network(risk: ToolRisk) -> bool:
    """Whether a call should be granted network inside the sandbox."""
    if risk.risk_class in (RiskClass.NETWORK, RiskClass.WRITE_REMOTE):
        return True
    if risk.tool_name in ("fetch", "web_search"):
        return True
    if not risk.command:
        return False

    # A build or scaffold step can shell out to a package manager at any point
    # in a chain, so every segment is checked.
    import re

    lowered = risk.command.lower()
    for segment in re.split(r"&&|\|\||;|\|", lowered):
        tokens = segment.strip().split()
        if not tokens:
            continue
        program = Path(tokens[0]).name
        if program in _NETWORK_PROGRAMS:
            return True
    return False


def normalize_sandbox_mode(mode: str) -> str:
    """Normalize ``workspace-write`` to ``workspace_write``.

    Config writes the hyphenated spelling and ``PermissionPolicy`` matches the
    underscored one, so without this every sandbox-default case falls through.
    """
    return (mode or "workspace_write").strip().lower().replace("-", "_")


def capabilities_for_mode(mode: str) -> frozenset[str]:
    """Capability set a session holds under a given sandbox mode."""
    return _MODE_CAPABILITIES.get(normalize_sandbox_mode(mode), _MODE_CAPABILITIES["read_only"])


def resolve_writable_roots(cwd: Path, mode: str, session_id: str) -> tuple[Path, ...]:
    """Writable roots for a session: the workspace plus its own temp directory."""
    if normalize_sandbox_mode(mode) == "read_only":
        return ()
    from kalash.core.paths import temp_dir_for_session

    roots = [cwd.resolve()]
    try:
        roots.append(temp_dir_for_session(session_id).resolve())
    except OSError:
        pass
    return tuple(roots)


@dataclass
class ToolHost:
    """Executes tool calls under the permission gate."""

    registry: ToolRegistry
    event_bus: EventBus
    session_id: str
    cwd: Path
    run_id: str = ""
    sandbox_mode: str = "workspace_write"
    mode: str = "build"
    workspace_only: bool = False
    network_enabled: bool = True
    """``build`` executes; ``plan`` refuses anything that changes state."""

    policy: PermissionPolicy | None = None
    approval: ApprovalPrompt | None = None
    defer_threshold_bytes: int = DEFER_THRESHOLD_BYTES

    # Delegation bounds, carried into every ToolContext this host builds.
    spawn_depth: int = 0
    max_spawn_depth: int = 3
    model_id: str = ""
    provider_id: str = ""
    """Provider identity for diagnostics and delegation."""

    hooks: Any = None
    """Optional :class:`~kalash.hooks.runner.HookRunner`. A PreToolUse block is
    final and is reported back to the model as correctable feedback."""

    cancel_event: asyncio.Event | None = None
    """Set by the agent loop; tools poll this to abort in-flight work."""

    delegate: Callable[..., Awaitable[dict[str, Any]]] | None = None
    capability_limit: frozenset[str] | None = None
    instruction_loader: InstructionLoader | None = None
    loaded_skills: dict[str, str] = field(default_factory=dict, init=False)
    config: KalashConfig | None = None

    # Session grants earned from "allow session"/"allow always" answers. Also
    # persisted in kalash_grants when storage is available.
    _grants: set[str] = field(default_factory=set, init=False)
    _denied_once: dict[str, str] = field(default_factory=dict, init=False)

    # -- context -------------------------------------------------------------

    @property
    def writable_roots(self) -> tuple[Path, ...]:
        return resolve_writable_roots(self.cwd, self.sandbox_mode, self.session_id)

    @property
    def capabilities(self) -> frozenset[str]:
        available = capabilities_for_mode(self.sandbox_mode)
        return available if self.capability_limit is None else available & self.capability_limit

    def build_context(self, *, allow_network: bool = False, tool_use_id: str = "") -> ToolContext:
        """Construct the populated context tools execute against."""
        return ToolContext(
            session_id=self.session_id,
            run_id=self.run_id or self.session_id,
            cwd=self.cwd,
            writable_roots=self.writable_roots,
            capabilities=self.capabilities,
            spawn_depth=self.spawn_depth,
            max_spawn_depth=self.max_spawn_depth,
            model_id=self.model_id,
            allow_network=allow_network,
            require_sandbox=normalize_sandbox_mode(self.sandbox_mode) != "danger_full_access",
            tool_use_id=tool_use_id,
            event_bus=self.event_bus,
            cancel_event=self.cancel_event,
            hooks=self.hooks,
            delegate=self.delegate,
            config=self.config,
        )

    # -- loop-facing interface ----------------------------------------------

    def schemas(self) -> list[dict[str, Any]]:
        """Expose the complete permitted tool set, stable across turns."""
        if not supports_tools(self.model_id):
            return []
        return [
            tool_schema(tool)
            for tool in self.registry.list_tools()
            if not (self.mode == "plan" and tool.side_effect in (SideEffect.WRITE, SideEffect.EXEC))
            and tool.capabilities <= self.capabilities
        ]

    def category(self, name: str) -> ToolCategory:
        """Concurrency class, derived from the tool's declared side effect."""
        try:
            tool = self.registry.get(name)
        except KeyError:
            # Unknown tools serialize: an unknown effect is not safely parallel.
            return ToolCategory.WRITE
        match tool.side_effect:
            case SideEffect.NONE | SideEffect.READ:
                return ToolCategory.READ
            case SideEffect.WRITE:
                return ToolCategory.WRITE
            case _:
                return ToolCategory.EXEC

    async def execute(self, name: str, arguments: dict[str, Any], *, tool_use_id: str) -> str:
        result = await self.execute_result(name, arguments, tool_use_id=tool_use_id)
        return result.content

    async def execute_result(
        self, name: str, arguments: dict[str, Any], *, tool_use_id: str
    ) -> ToolExecutionResult:
        """Gate, run, and render one tool call. Never raises."""
        try:
            tool = self.registry.get(name)
        except KeyError:
            tool = None
        if tool is not None:
            if self.mode == "plan" and tool.side_effect in (SideEffect.WRITE, SideEffect.EXEC):
                return ToolExecutionResult(
                    "REFUSED: state-changing tools are disabled in plan mode", True
                )
            if not tool.capabilities <= self.capabilities:
                return ToolExecutionResult(
                    "REFUSED: tool requires capabilities unavailable to this run", True
                )
            if not self.network_enabled and any(
                cap.startswith("network.") for cap in tool.capabilities
            ):
                return ToolExecutionResult("REFUSED: network access is disabled for this run", True)
        risk = classify_tool_call(
            name,
            arguments,
            cwd=self.cwd,
            writable_roots=self.writable_roots,
        )
        if self.instruction_loader is not None and risk.paths:
            added = self.instruction_loader.include(risk.paths)
            if added and self.category(name) != ToolCategory.READ:
                return ToolExecutionResult(
                    "New scoped project guidance loaded. Review it before retrying this change:\n"
                    + "\n\n".join(added),
                    True,
                )

        # Hooks run before the policy. They are the user's deterministic control
        # and a block from one is final — the model cannot reason its way past it.
        blocked = await self._run_pre_hooks(name, arguments)
        if blocked is not None:
            return ToolExecutionResult(blocked, is_error=True)

        # Delegation is announced before it starts: a child can run for a long
        # time returning nothing, and an unannounced pause looks like a stall.
        if name == "task":
            await self.event_bus.emit(
                Event(
                    type=EventType.AGENT_SPAWN,
                    session_id=self.session_id,
                    data={
                        "child_run_id": tool_use_id,
                        "brief": str(arguments.get("prompt", ""))[:120],
                        "agent": "task",
                    },
                )
            )

        verdict = await self._gate(risk, arguments)
        if verdict is not None:
            await self.event_bus.emit(
                Event(
                    type=EventType.TOOL_DENIED,
                    session_id=self.session_id,
                    data={"tool": name, "reason": verdict, "tool_use_id": tool_use_id},
                )
            )
            return ToolExecutionResult(verdict, is_error=True)

        # Commands that legitimately need the network get it, having already
        # passed the gate. Package managers, git remotes, and curl all classify
        # as NETWORK, so this is the same decision the user already approved.
        context = self.build_context(
            allow_network=self.network_enabled and _needs_network(risk),
            tool_use_id=tool_use_id,
        )

        start_time = time.monotonic()
        envelope = await self.registry.dispatch(name, arguments, context)
        if name == "skill" and envelope.ok and envelope.metadata.get("skill"):
            self.loaded_skills[str(envelope.metadata["skill"])] = envelope.content
        duration_ms = int((time.monotonic() - start_time) * 1000)
        rendered = self._render(name, envelope, risk)
        # Apply the tool contract even to third-party tools and error bodies.
        limit = min(max(tool.max_output_bytes, 1024), 65536) if tool else 65536
        rendered = self._bound_observation(name, rendered, limit)
        await self._publish_result(name, tool_use_id, envelope, duration_ms=duration_ms)
        await self._run_post_hooks(name, arguments, envelope)
        return ToolExecutionResult(rendered, is_error=not envelope.ok)

    async def _publish_result(
        self, name: str, tool_use_id: str, envelope: ToolEnvelope, *, duration_ms: int = 0
    ) -> None:
        """Emit the detail a UI needs to render this call's outcome."""
        payload: dict[str, Any] = {
            "tool_name": name,
            "tool_use_id": tool_use_id,
            "ok": envelope.ok,
            "duration_ms": duration_ms,
        }
        for key in (
            "path",
            "diff",
            "diff_stat",
            "operation",
            "turns",
            "child_tokens",
            "match_count",
            "query",
            "search_path",
            "pattern",
            "count",
            "entry_count",
            "exit_code",
            "command",
            "sandboxed",
            "sandbox_warning",
        ):
            value = envelope.metadata.get(key)
            if value not in (None, "", 0):
                payload[key] = value

        if name == "task":
            await self.event_bus.emit(
                Event(
                    type=EventType.AGENT_COMPLETE,
                    session_id=self.session_id,
                    data={
                        "child_run_id": tool_use_id,
                        "status": "completed" if envelope.ok else "failed",
                        "turns": envelope.metadata.get("turns", 0),
                        "tokens": envelope.metadata.get("child_tokens", 0),
                    },
                )
            )
            return

        await self.event_bus.emit(
            Event(
                type=EventType.TOOL_COMPLETE,
                session_id=self.session_id,
                data=payload,
            )
        )

    # -- hooks ---------------------------------------------------------------

    async def _run_pre_hooks(self, name: str, arguments: dict[str, Any]) -> str | None:
        """Fire PreToolUse. Returns a refusal string when a hook blocks."""
        if self.hooks is None:
            return None

        from kalash.hooks.events import HookEvent, ToolUsePayload

        try:
            results = await self.hooks.dispatch(
                ToolUsePayload(
                    event=HookEvent.PRE_TOOL_USE,
                    session_id=self.session_id,
                    tool_name=name,
                    arguments=arguments,
                )
            )
        except Exception as exc:
            # A blocking hook signals by raising; treat that as authoritative.
            detail = str(exc) or "a PreToolUse hook blocked this call"
            return (
                f"REFUSED by a PreToolUse hook: {detail}. "
                f"Adapt your approach; do not retry the same call."
            )

        for result in results:
            if result.error:
                return f"REFUSED: PreToolUse hook could not complete: {result.error}"
            if result.blocked:
                detail = result.stderr.strip() or "blocked by policy"
                return (
                    f"REFUSED by a PreToolUse hook: {detail}. "
                    f"Adapt your approach; do not retry the same call."
                )
        return None

    async def _run_post_hooks(
        self, name: str, arguments: dict[str, Any], envelope: ToolEnvelope
    ) -> None:
        """Fire PostToolUse. Observational only; failures never affect the turn."""
        if self.hooks is None:
            return

        from kalash.hooks.events import HookEvent, ToolUsePayload

        try:
            await self.hooks.dispatch(
                ToolUsePayload(
                    event=HookEvent.POST_TOOL_USE,
                    session_id=self.session_id,
                    tool_name=name,
                    arguments=arguments,
                    result=envelope.ok,
                )
            )
        except Exception as e:
            logger.error("PostToolUse hook failed for %s", name, exc_info=e)

    # -- the gate ------------------------------------------------------------

    async def _gate(self, risk: ToolRisk, arguments: dict[str, Any]) -> str | None:
        """Return a refusal string, or None to proceed."""
        if self.workspace_only:
            for name in risk.paths:
                path = Path(name)
                path = (self.cwd / path if not path.is_absolute() else path).resolve()
                if not path.is_relative_to(self.cwd.resolve()):
                    return "REFUSED: this run may only access its workspace"
        if not self.network_enabled and _needs_network(risk):
            return "REFUSED: network access is disabled for this run"
        # Plan mode is enforced, not merely requested in the prompt.
        if self.mode == "plan" and risk.risk_class in _WRITE_RISKS:
            return (
                f"REFUSED: plan mode is active, so {risk.tool_name} cannot change state "
                f"({risk.reason}). Describe the intended change instead; the user will "
                f"switch to build mode to apply it."
            )

        if self.policy is None:
            return None

        signature = self._signature(risk, arguments)
        if signature in self._grants:
            return None

        request = PolicyRequest(
            tool_name=risk.tool_name,
            risk_class=risk.risk_class,
            paths=risk.paths,
            hosts=risk.hosts,
            command=risk.command,
            arguments=arguments,
            confirmation_classes=risk.confirmation_classes,
            grant_signature=signature,
        )
        result = await self.policy.evaluate(request)

        if result.decision is Decision.ALLOW:
            return None

        if result.decision is Decision.DENY:
            return f"REFUSED: {result.reason} (policy stage {result.stage})"

        return await self._ask(risk, result.reason, signature)

    async def _ask(self, risk: ToolRisk, reason: str, signature: str) -> str | None:
        """Prompt the user. Returns None on approval, a refusal string otherwise."""
        if self.approval is None:
            return (
                f"REFUSED: {risk.tool_name} needs approval ({reason}) and no approval "
                f"channel is available."
            )

        context = PromptContext(
            action=risk.summary,
            tool_name=risk.tool_name,
            command=risk.command,
            paths=risk.paths,
            capabilities=sorted(self.capabilities),
            risk_class=risk.risk_class.value,
            reversibility=risk.reversibility,
            confirmation_classes=[c.value for c in risk.confirmation_classes],
            metadata={"reason": risk.reason or reason},
        )
        outcome = await self.approval.request_approval(context)

        match outcome.response:
            case ApprovalResponse.ALLOW_ONCE:
                return None
            case ApprovalResponse.ALLOW_SESSION:
                self._grants.add(signature)
                await self._persist_grant(risk, signature, lifetime="session")
                return None
            case ApprovalResponse.ALLOW_ALWAYS:
                self._grants.add(signature)
                await self._persist_grant(risk, signature, lifetime="always")
                return None
            case ApprovalResponse.MODIFY:
                return (
                    f"REFUSED: the user wants {risk.tool_name} changed before it runs. "
                    f"Propose a different approach."
                )
            case _:
                detail = f": {outcome.reason}" if outcome.reason else ""
                return f"REFUSED by the user{detail}. Do not retry; choose another approach."

    @staticmethod
    def _signature(risk: ToolRisk, arguments: dict[str, Any]) -> str:
        """Grant key.

        Shell grants are scoped to the program being run, so approving
        ``npm install`` once does not also approve ``rm -rf`` for the session.
        """
        if risk.tool_name == "shell":
            command = str(arguments.get("command", ""))
            try:
                tokens = shlex.split(command)
            except ValueError:
                tokens = command.split()
            program = Path(tokens[0]).name if tokens else ""
            return f"shell:{program}:{risk.risk_class.value}"
        return f"{risk.tool_name}:{risk.risk_class.value}"

    async def _persist_grant(self, risk: ToolRisk, signature: str, *, lifetime: str) -> None:
        """Record a session or always grant in durable storage."""
        if self.policy is None:
            return
        store = self.policy._grant_store  # noqa: SLF001
        if store is None:
            return
        from kalash.permissions.grants import GrantLifetime

        try:
            await store.create(
                tool_pattern=risk.tool_name,
                lifetime=GrantLifetime(lifetime),
                reason=f"user approved {risk.summary}",
                metadata={
                    "signature": signature,
                    "risk_class": risk.risk_class.value,
                },
            )
        except Exception as e:
            logger.error("could not persist permission grant", exc_info=e)

    # -- rendering -----------------------------------------------------------

    def _bound_observation(self, name: str, content: str, limit: int) -> str:
        body = content.encode("utf-8")
        if len(body) <= limit:
            return content
        try:
            stored = get_scratchpad(self.session_id).put("other", name, content)
            hint = f"Full result: expand({stored.ref}, grep=...) or use offset/limit."
        except OSError:
            logger.warning("could not retain oversized %s result", name, exc_info=True)
            hint = "Full result could not be stored; rerun with narrower output."
        suffix = f"\n[output bounded from {len(body)} bytes. {hint}]"
        room = max(0, limit - len(suffix.encode("utf-8")))
        return body[:room].decode("utf-8", errors="ignore") + suffix

    def _render(self, name: str, envelope: ToolEnvelope, risk: ToolRisk) -> str:
        """Turn an envelope into the string the model sees."""
        if not envelope.ok:
            error = envelope.error
            if error is None:
                return f"ERROR: {name} failed without detail."
            parts = [f"ERROR {error.code}: {error.message}"]
            if envelope.content:
                parts.append(envelope.content)
            if error.remediation:
                parts.append(error.remediation)
            return "\n".join(parts)

        content = envelope.content or "(no output)"

        if envelope.truncated and envelope.truncation is not None:
            hint = envelope.truncation.retrieval_hint
            content += (
                f"\n[truncated: showing {envelope.truncation.shown_lines} of "
                f"{envelope.truncation.total_lines} lines" + (f". {hint}]" if hint else "]")
            )

        deferred = self._maybe_defer(name, content, risk)
        return deferred if deferred is not None else content

    def _maybe_defer(self, name: str, content: str, risk: ToolRisk) -> str | None:
        """Move a bulky result to the scratchpad, returning the stand-in."""
        if name not in DEFERRABLE:
            return None
        if len(content.encode("utf-8")) <= self.defer_threshold_bytes:
            return None

        kind = {"shell": "shell", "search": "search", "fetch": "fetch"}.get(name, "file")
        headline = risk.summary or name

        try:
            stored = get_scratchpad(self.session_id).put(
                kind,
                headline,
                content,
                metadata={"tool": name, "path": risk.paths[0] if risk.paths else ""},
            )
        except OSError:
            # If the store is unavailable, inlining is still correct.
            logger.warning("scratchpad deferral failed for %s", name, exc_info=True)
            return None

        lines = content.splitlines()
        head = (
            "\n".join(lines[:DEFER_HEAD_LINES])
            .encode("utf-8")[:6144]
            .decode("utf-8", errors="ignore")
        )
        remaining = max(0, len(lines) - DEFER_HEAD_LINES)

        return (
            f"{head}\n"
            f"[{remaining} more lines stored as {stored.ref} — "
            f"expand({stored.ref}) for the rest, or expand({stored.ref}, grep=...) to search it]"
        )
