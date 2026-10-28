"""Permission decisions: allow, ask or deny a tool call before it runs.

Order of evaluation (first match wins):

1. user ``deny`` rules                        -> deny
2. reads/writes of credential paths          -> deny
3. writes in ``read-only`` mode, or outside the workspace / writable roots unless
   ``danger-full-access``                     -> deny
4. writes to protected paths (``.git``, ``.kalash``, ``.mcp.json``) -> ask
5. running a command outside the sandbox     -> ask
6. user ``ask`` rules, tools that need approval (untrusted MCP) -> ask
7. network tools when the network is disabled -> deny
8. everything else                            -> allow

Commands are *not* classified by parsing shell text: the OS sandbox confines what a command
can write, and ``ask``/``deny`` rules are an explicit, user-owned way to add prompts.
An ``ask`` becomes a denial when approvals are disabled or nobody can answer.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from kalash.paths import kalash_home
from kalash.tools.base import Effects, Tool, ToolContext

Verdict = Literal["allow", "ask", "deny"]
Answer = Literal["once", "session", "deny"]

SENSITIVE_HOME = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".netrc",
    ".docker/config.json",
    ".git-credentials",
    ".config/gcloud",
    ".kube/config",
)
PROTECTED_NAMES = ((".git", "hooks"), (".git", "config"), (".kalash",), (".mcp.json",))


@dataclass
class Decision:
    verdict: Verdict
    reason: str = ""


@dataclass
class ApprovalRequest:
    call_id: str
    tool: str
    summary: str
    reason: str
    detail: str = ""  # a diff, the full command... shown under the summary


class Approver(Protocol):
    async def __call__(self, request: ApprovalRequest) -> Answer: ...


def is_sensitive(path: Path) -> bool:
    home = Path.home()
    kalash = kalash_home()
    if path in (kalash / "auth.json", kalash / "auth.key", kalash / "auth.lock"):
        return True
    for name in SENSITIVE_HOME:
        target = home / name
        if path == target or target in path.parents:
            return True
    return False


def is_protected(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    parts = relative.parts
    return any(parts[: len(p)] == p for p in PROTECTED_NAMES)


def _inside(path: Path, base: Path) -> bool:
    return path == base or base in path.parents


@dataclass
class PermissionEngine:
    mode: str
    approval: str
    root: Path
    writable_roots: list[Path]
    session_tmp: Path
    network: bool = True
    deny_rules: list[str] = field(default_factory=list)
    ask_rules: list[str] = field(default_factory=list)
    approver: Approver | None = None
    _session_grants: set[str] = field(default_factory=set)

    def decide(self, tool: Tool, key: str, effects: Effects) -> Decision:
        for rule in self.deny_rules:
            if fnmatch.fnmatchcase(key, rule):
                return Decision("deny", f"blocked by deny rule {rule!r}")
        for path in effects.reads:
            if is_sensitive(_real(path)):
                return Decision("deny", f"{path} holds credentials and cannot be accessed")
        for path in effects.writes:
            real = _real(path)
            if is_sensitive(real):
                return Decision("deny", f"{path} holds credentials and cannot be modified")
            if self.mode == "read-only":
                return Decision(
                    "deny", "the session sandbox is read-only; file changes are disabled"
                )
            if self.mode != "danger-full-access" and not self._writable(real):
                return Decision(
                    "deny",
                    f"{path} is outside the workspace ({self.root}); writes are limited to the "
                    f"workspace and temp. Choose a path inside the workspace.",
                )
            if is_protected(real, self.root):
                return Decision("ask", f"{path} is a protected configuration path")
        if effects.escalate:
            return Decision("ask", "runs outside the sandbox")
        for rule in self.ask_rules:
            if fnmatch.fnmatchcase(key, rule):
                return Decision("ask", f"matches ask rule {rule!r}")
        if tool.needs_approval:
            server = tool.name.split("__")[1] if tool.name.startswith("mcp__") else ""
            fix = f" The user can allow it with `/mcp trust {server}`." if server else ""
            return Decision(
                "ask", f"{tool.name} is an external tool that has not been trusted.{fix}"
            )
        if effects.network and not self.network:
            return Decision("deny", "network access is disabled for this session")
        return Decision("allow")

    def _writable(self, path: Path) -> bool:
        return any(
            _inside(path, base) for base in [self.root, self.session_tmp, *self.writable_roots]
        )

    async def resolve(
        self, call_id: str, tool: Tool, key: str, decision: Decision, detail: str = ""
    ) -> Decision:
        """Turn an ``ask`` into allow/deny by consulting the approver."""
        if decision.verdict != "ask":
            return decision
        grant = f"{tool.name}\0{key}"
        if grant in self._session_grants and not any(
            fnmatch.fnmatchcase(key, r) for r in self.deny_rules
        ):
            return Decision("allow", "previously approved for this session")
        if self.approval == "never":
            return Decision(
                "deny",
                f"{decision.reason}; approval is required but approval prompts are disabled "
                f"(approval: never). Do not retry; tell the user what you needed.",
            )
        if self.approver is None:
            return Decision(
                "deny",
                f"{decision.reason}; approval is required but no one is available to answer. "
                f"Re-run interactively or with approval 'never' to skip such actions.",
            )
        answer = await self.approver(
            ApprovalRequest(call_id, tool.name, key, decision.reason, detail)
        )
        if answer == "deny":
            return Decision("deny", "the user declined this action")
        if answer == "session":
            self._session_grants.add(grant)
        return Decision("allow", "approved by the user")


def _real(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def tool_context_roots(ctx: ToolContext) -> list[Path]:
    return [ctx.root, *ctx.writable_roots]
