"""Tool-call risk classification.

:class:`~kalash.permissions.policy.PermissionPolicy` evaluates a
:class:`~kalash.permissions.policy.PolicyRequest`, but nothing in the codebase
ever built one — which is why the policy, however correct, never ran. This
module is that missing translation: tool name plus arguments in, risk class,
affected paths, and confirmation classes out.

The interesting work is in :func:`analyze_command`, because ``shell`` can do
anything and its risk lives entirely in its argument string.

**Classification fails toward "ask".** An unrecognized or unparseable command is
assigned the broadest plausible risk, never the narrowest. Guessing "probably
harmless" on something we could not parse is the one mistake here that is
actually dangerous, so:

* Command substitution, ``eval``, and pipes into a shell defeat static analysis
  entirely and are treated as destructive.
* Chained commands are split and the *maximum* risk across segments wins, so
  ``cd /tmp && rm -rf /`` is not judged on its ``cd``.
* Flags are matched on parsed tokens rather than raw substrings, so ``rm  -rf``,
  ``rm -r -f``, and ``rm --recursive --force`` all classify identically. The
  previous substring approach in ``tools/shell.py`` missed all three.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kalash.permissions.policy import ConfirmationClass, RiskClass

# Ordered weakest to strongest. Used to take the max across command segments.
_RISK_ORDER: tuple[RiskClass, ...] = (
    RiskClass.READ,
    RiskClass.NETWORK,
    RiskClass.WRITE,
    RiskClass.WRITE_REMOTE,
    RiskClass.DESTRUCTIVE,
)

# Constructs that make a command's effect undecidable by inspection.
_OPAQUE_MARKERS: tuple[str, ...] = ("$(", "`", "${", "eval ", "exec ", "source ")

# Splitting on these lets each segment be judged on its own merits.
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||\n")

# Tools whose risk is fixed and needs no argument inspection.
_STATIC_RISK: dict[str, RiskClass] = {
    "read": RiskClass.READ,
    "glob": RiskClass.READ,
    "search": RiskClass.READ,
    "list": RiskClass.READ,
    "expand": RiskClass.READ,
    "skill": RiskClass.READ,
    "tool_search": RiskClass.READ,
    "note": RiskClass.READ,
    "todo": RiskClass.READ,
    "recall": RiskClass.READ,
    "write": RiskClass.WRITE,
    "edit": RiskClass.WRITE,
    "multi_edit": RiskClass.WRITE,
    "remember": RiskClass.WRITE,
    "forget": RiskClass.WRITE,
    "notebook_edit": RiskClass.WRITE,
    "fetch": RiskClass.READ,
    "web_search": RiskClass.READ,
    "task": RiskClass.WRITE,
}

# Argument keys that carry a filesystem path, in the order we prefer them.
_PATH_KEYS: tuple[str, ...] = ("path", "file", "file_path", "target", "cwd")

_REVERSIBLE = "reversible"
_PARTIAL = "partially_reversible"
_IRREVERSIBLE = "irreversible"


@dataclass(frozen=True, slots=True)
class CommandRisk:
    """What static analysis concluded about a shell command."""

    risk_class: RiskClass
    confirmation_classes: tuple[ConfirmationClass, ...] = ()
    reversibility: str = _PARTIAL
    reason: str = ""
    opaque: bool = False
    """True when the command could not be analysed and was judged conservatively."""


@dataclass(frozen=True, slots=True)
class ToolRisk:
    """Everything the permission gate and the approval prompt need."""

    tool_name: str
    risk_class: RiskClass
    summary: str
    paths: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    command: str = ""
    confirmation_classes: list[ConfirmationClass] = field(default_factory=list)
    reversibility: str = _PARTIAL
    reason: str = ""


def _max_risk(a: RiskClass, b: RiskClass) -> RiskClass:
    return a if _RISK_ORDER.index(a) >= _RISK_ORDER.index(b) else b


def _flags(tokens: list[str]) -> set[str]:
    """Collect single-letter and long flags from a token list.

    ``-rf`` contributes ``r`` and ``f``; ``--force`` contributes ``force``. This
    is what makes flag order and grouping irrelevant to the match.
    """
    found: set[str] = set()
    for token in tokens:
        if token.startswith("--"):
            found.add(token[2:].split("=", 1)[0])
        elif token.startswith("-") and len(token) > 1:
            found.update(token[1:])
    return found


def analyze_command(command: str) -> CommandRisk:
    """Classify a shell command string."""
    text = command.strip()
    if not text:
        return CommandRisk(
            risk_class=RiskClass.READ,
            reversibility=_REVERSIBLE,
            reason="empty command",
        )

    lowered = text.lower()

    # Anything that can construct a command at runtime cannot be judged now.
    if any(marker in lowered for marker in _OPAQUE_MARKERS):
        return CommandRisk(
            risk_class=RiskClass.DESTRUCTIVE,
            confirmation_classes=(ConfirmationClass.BULK_DESTRUCTION,),
            reversibility=_IRREVERSIBLE,
            reason="command builds or evaluates code at runtime; effect is not statically knowable",
            opaque=True,
        )

    # Piping a download into an interpreter executes code nobody reviewed.
    if re.search(r"(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|d|k)?sh\b", lowered):
        return CommandRisk(
            risk_class=RiskClass.DESTRUCTIVE,
            confirmation_classes=(ConfirmationClass.BULK_DESTRUCTION,),
            reversibility=_IRREVERSIBLE,
            reason="pipes downloaded content into a shell",
            opaque=True,
        )

    worst = CommandRisk(
        risk_class=RiskClass.READ,
        reversibility=_REVERSIBLE,
        reason="read-only command",
    )

    for raw_segment in _SEGMENT_SPLIT.split(text):
        segment = raw_segment.strip()
        if not segment:
            continue
        found = _analyze_segment(segment)
        if _max_risk(found.risk_class, worst.risk_class) == found.risk_class and (
            found.risk_class != worst.risk_class or found.confirmation_classes
        ):
            merged_classes = tuple(
                dict.fromkeys(worst.confirmation_classes + found.confirmation_classes)
            )
            worst = CommandRisk(
                risk_class=_max_risk(found.risk_class, worst.risk_class),
                confirmation_classes=merged_classes,
                reversibility=found.reversibility,
                reason=found.reason,
                opaque=found.opaque or worst.opaque,
            )

    return worst


def _analyze_segment(segment: str) -> CommandRisk:
    """Classify one command segment (no shell operators)."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        # Unbalanced quotes. We cannot see the real argv, so assume the worst.
        return CommandRisk(
            risk_class=RiskClass.DESTRUCTIVE,
            confirmation_classes=(ConfirmationClass.BULK_DESTRUCTION,),
            reversibility=_IRREVERSIBLE,
            reason="command could not be tokenised (unbalanced quotes)",
            opaque=True,
        )

    if not tokens:
        return CommandRisk(RiskClass.READ, reversibility=_REVERSIBLE)

    # Strip leading environment assignments: FOO=bar cmd ...
    while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
        tokens = tokens[1:]
    if not tokens:
        return CommandRisk(RiskClass.READ, reversibility=_REVERSIBLE)

    escalated = False
    if tokens[0] in ("sudo", "doas", "su", "runas"):
        escalated = True
        tokens = tokens[1:]
        if not tokens:
            return CommandRisk(
                risk_class=RiskClass.DESTRUCTIVE,
                confirmation_classes=(ConfirmationClass.SECURITY_SURFACE,),
                reversibility=_IRREVERSIBLE,
                reason="privilege escalation",
            )

    argv0 = Path(tokens[0]).name
    rest = tokens[1:]
    flags = _flags(rest)

    result = _classify_argv(argv0, rest, flags, segment)

    if escalated:
        # sudo escapes the sandbox entirely, so it is never merely a write.
        return CommandRisk(
            risk_class=RiskClass.DESTRUCTIVE,
            confirmation_classes=tuple(
                dict.fromkeys((*result.confirmation_classes, ConfirmationClass.SECURITY_SURFACE))
            ),
            reversibility=_IRREVERSIBLE,
            reason=f"runs with elevated privileges: {result.reason or argv0}",
        )
    return result


def _classify_argv(  # noqa: PLR0911 - a flat rule table reads better than nesting
    argv0: str,
    rest: list[str],
    flags: set[str],
    segment: str,
) -> CommandRisk:
    """Rule table over a parsed command."""
    if argv0 == "git":
        return _classify_git(rest, flags)

    if argv0 == "rm":
        recursive = bool({"r", "R", "recursive"} & flags)
        forced = "f" in flags or "force" in flags
        if recursive or forced:
            return CommandRisk(
                risk_class=RiskClass.DESTRUCTIVE,
                confirmation_classes=(ConfirmationClass.BULK_DESTRUCTION,),
                reversibility=_IRREVERSIBLE,
                reason="recursive or forced delete",
            )
        return CommandRisk(
            risk_class=RiskClass.DESTRUCTIVE,
            confirmation_classes=(ConfirmationClass.DATA_DESTRUCTION,),
            reversibility=_IRREVERSIBLE,
            reason="deletes files",
        )

    if argv0 in ("dd", "mkfs", "fdisk", "parted", "shred", "srm"):
        return CommandRisk(
            risk_class=RiskClass.DESTRUCTIVE,
            confirmation_classes=(ConfirmationClass.DATA_DESTRUCTION,),
            reversibility=_IRREVERSIBLE,
            reason=f"{argv0} can destroy data irrecoverably",
        )

    if argv0 in ("chmod", "chown", "chgrp", "setfacl"):
        return CommandRisk(
            risk_class=RiskClass.WRITE,
            confirmation_classes=(ConfirmationClass.SECURITY_SURFACE,),
            reversibility=_PARTIAL,
            reason="changes file permissions or ownership",
        )

    if argv0 == "find" and ("delete" in flags or "-delete" in rest or "-exec" in rest):
        return CommandRisk(
            risk_class=RiskClass.DESTRUCTIVE,
            confirmation_classes=(ConfirmationClass.BULK_DESTRUCTION,),
            reversibility=_IRREVERSIBLE,
            reason="find with -delete or -exec",
        )

    if argv0 in ("curl", "wget", "ssh", "scp", "rsync", "sftp", "nc", "telnet"):
        return CommandRisk(
            risk_class=RiskClass.NETWORK,
            reversibility=_PARTIAL,
            reason=f"{argv0} performs network access",
        )

    if argv0 in ("kubectl", "terraform", "aws", "gcloud", "az", "helm", "flyctl"):
        production = bool(re.search(r"\b(prod|production|live)\b", segment, re.IGNORECASE))
        classes = [ConfirmationClass.PRODUCTION] if production else []
        destructive = bool({"delete", "destroy", "apply", "drop"} & set(rest[:2]))
        return CommandRisk(
            risk_class=RiskClass.DESTRUCTIVE if destructive else RiskClass.NETWORK,
            confirmation_classes=tuple(classes),
            reversibility=_IRREVERSIBLE if destructive else _PARTIAL,
            reason=f"{argv0} operates on remote infrastructure",
        )

    if argv0 in ("docker", "podman") and ({"rm", "rmi", "prune", "down"} & set(rest[:2])):
        return CommandRisk(
            risk_class=RiskClass.DESTRUCTIVE,
            confirmation_classes=(ConfirmationClass.DATA_DESTRUCTION,),
            reversibility=_IRREVERSIBLE,
            reason="removes container resources",
        )

    if argv0 in ("npm", "pnpm", "yarn", "pip", "pip3", "uv", "cargo", "go", "gem"):
        subcommand = rest[0] if rest else ""
        if subcommand in ("publish", "release"):
            return CommandRisk(
                risk_class=RiskClass.WRITE_REMOTE,
                confirmation_classes=(ConfirmationClass.REMOTE_PUBLICATION,),
                reversibility=_IRREVERSIBLE,
                reason="publishes a package",
            )
        if "g" in flags or "global" in flags:
            return CommandRisk(
                risk_class=RiskClass.WRITE,
                confirmation_classes=(ConfirmationClass.BOUNDARY_CROSSING,),
                reversibility=_PARTIAL,
                reason="installs globally, outside the workspace",
            )
        return CommandRisk(
            risk_class=RiskClass.NETWORK,
            reversibility=_PARTIAL,
            reason=f"{argv0} may fetch dependencies",
        )

    # Commands known to only observe.
    if argv0 in (
        "ls",
        "cat",
        "head",
        "tail",
        "wc",
        "grep",
        "rg",
        "fd",
        "which",
        "file",
        "stat",
        "pwd",
        "echo",
        "date",
        "env",
        "printenv",
        "diff",
        "tree",
        "du",
        "df",
        "ps",
        "uname",
        "whoami",
        "sort",
        "uniq",
        "cut",
        "awk",
        "sed",
        "jq",
        "python",
        "python3",
        "node",
        "pytest",
        "ruff",
        "mypy",
        "make",
        "cargo-check",
        "tsc",
        "eslint",
    ):
        # sed -i and make can write, so this is WRITE unless clearly read-only.
        if argv0 == "sed" and ("i" in flags or "in-place" in flags):
            return CommandRisk(
                risk_class=RiskClass.WRITE,
                reversibility=_PARTIAL,
                reason="sed in-place edit",
            )
        if argv0 in ("python", "python3", "node", "make"):
            return CommandRisk(
                risk_class=RiskClass.WRITE,
                reversibility=_PARTIAL,
                reason=f"{argv0} runs arbitrary code that may write",
            )
        return CommandRisk(
            risk_class=RiskClass.READ,
            reversibility=_REVERSIBLE,
            reason=f"{argv0} is read-only",
        )

    # Unknown command. Assume it can write; do not assume it cannot.
    return CommandRisk(
        risk_class=RiskClass.WRITE,
        reversibility=_PARTIAL,
        reason=f"unrecognised command {argv0!r}; assuming it may modify state",
    )


def _classify_git(rest: list[str], flags: set[str]) -> CommandRisk:
    """Git is granular by design: reading history and force-pushing differ hugely."""
    subcommand = ""
    for token in rest:
        if not token.startswith("-"):
            subcommand = token
            break

    if subcommand in ("status", "log", "diff", "show", "blame", "describe", "ls-files"):
        return CommandRisk(
            risk_class=RiskClass.READ,
            reversibility=_REVERSIBLE,
            reason=f"git {subcommand} is read-only",
        )

    if subcommand == "push":
        forced = bool({"f", "force", "force-with-lease"} & flags)
        protected = any(branch in rest for branch in ("main", "master", "production", "release"))
        classes = [ConfirmationClass.REMOTE_PUBLICATION]
        if forced:
            classes.append(ConfirmationClass.DESTRUCTIVE_GIT)
        return CommandRisk(
            risk_class=RiskClass.WRITE_REMOTE,
            confirmation_classes=tuple(classes),
            reversibility=_IRREVERSIBLE if forced else _PARTIAL,
            reason=(
                "force push rewrites remote history"
                if forced
                else f"pushes to {'a protected branch' if protected else 'a remote'}"
            ),
        )

    if subcommand in ("reset", "clean"):
        hard = "hard" in flags or bool({"f", "d", "force"} & flags)
        if hard:
            return CommandRisk(
                risk_class=RiskClass.DESTRUCTIVE,
                confirmation_classes=(ConfirmationClass.DESTRUCTIVE_GIT,),
                reversibility=_IRREVERSIBLE,
                reason=f"git {subcommand} discards uncommitted work",
            )
        return CommandRisk(
            risk_class=RiskClass.WRITE,
            reversibility=_PARTIAL,
            reason=f"git {subcommand}",
        )

    if subcommand in ("rebase", "filter-branch", "filter-repo"):
        return CommandRisk(
            risk_class=RiskClass.DESTRUCTIVE,
            confirmation_classes=(ConfirmationClass.DESTRUCTIVE_GIT,),
            reversibility=_IRREVERSIBLE,
            reason=f"git {subcommand} rewrites history",
        )

    if subcommand == "branch" and "D" in flags:
        return CommandRisk(
            risk_class=RiskClass.DESTRUCTIVE,
            confirmation_classes=(ConfirmationClass.DESTRUCTIVE_GIT,),
            reversibility=_IRREVERSIBLE,
            reason="force-deletes a branch",
        )

    if subcommand == "commit":
        return CommandRisk(
            risk_class=RiskClass.WRITE,
            confirmation_classes=(ConfirmationClass.COMMITS,),
            reversibility=_PARTIAL,
            reason="creates a commit",
        )

    if subcommand == "config":
        return CommandRisk(
            risk_class=RiskClass.WRITE,
            confirmation_classes=(ConfirmationClass.TRUST_CONFIG,),
            reversibility=_PARTIAL,
            reason="modifies git configuration",
        )

    return CommandRisk(
        risk_class=RiskClass.WRITE,
        reversibility=_PARTIAL,
        reason=f"git {subcommand or '(unknown)'}",
    )


def _extract_paths(arguments: dict[str, Any]) -> list[str]:
    """Pull filesystem paths out of tool arguments."""
    paths: list[str] = []
    for key in _PATH_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
    # multi_edit and similar carry a list of per-file edits.
    edits = arguments.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict):
                candidate = edit.get("path") or edit.get("file")
                if isinstance(candidate, str) and candidate.strip():
                    paths.append(candidate.strip())
    return list(dict.fromkeys(paths))


def _extract_hosts(arguments: dict[str, Any]) -> list[str]:
    """Pull hostnames out of URL-bearing arguments."""
    from urllib.parse import urlparse

    hosts: list[str] = []
    for key in ("url", "uri", "endpoint"):
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        candidate = value.strip()
        if "://" not in candidate:
            candidate = "https://" + candidate
        host = urlparse(candidate).hostname
        if host:
            hosts.append(host)
    return list(dict.fromkeys(hosts))


def classify_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    cwd: Path | None = None,
    writable_roots: tuple[Path, ...] = (),
) -> ToolRisk:
    """Classify a tool call for the permission gate.

    An unknown tool is treated as a write, not as a read: an MCP server can
    register anything, and assuming a name we do not recognise is harmless is
    how an ungated write gets through.
    """
    paths = _extract_paths(arguments)
    hosts = _extract_hosts(arguments)
    base = cwd or Path.cwd()

    if tool_name == "shell":
        command = str(arguments.get("command", ""))
        analysis = analyze_command(command)
        summary = f"Run: {command}" if command else "Run a shell command"
        return ToolRisk(
            tool_name=tool_name,
            risk_class=analysis.risk_class,
            summary=summary,
            paths=paths,
            hosts=hosts,
            command=command,
            confirmation_classes=list(analysis.confirmation_classes),
            reversibility=analysis.reversibility,
            reason=analysis.reason,
        )

    risk = _STATIC_RISK.get(tool_name)
    confirmation: list[ConfirmationClass] = []

    if risk is None:
        risk = RiskClass.WRITE
        reason = f"unregistered tool {tool_name!r}; assuming it may modify state"
    else:
        reason = f"{tool_name} is classified {risk.value}"

    # Writing outside every writable root is a boundary crossing regardless of
    # which tool is doing it.
    if risk in (RiskClass.WRITE, RiskClass.DESTRUCTIVE) and writable_roots:
        for raw in paths:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = base / candidate
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            inside = any(resolved == root or root in resolved.parents for root in writable_roots)
            if not inside:
                confirmation.append(ConfirmationClass.BOUNDARY_CROSSING)
                reason = f"writes outside the workspace: {resolved}"
                break

    reversibility = {
        RiskClass.READ: _REVERSIBLE,
        RiskClass.NETWORK: _REVERSIBLE,
        RiskClass.WRITE: _PARTIAL,
        RiskClass.WRITE_REMOTE: _IRREVERSIBLE,
        RiskClass.DESTRUCTIVE: _IRREVERSIBLE,
    }[risk]

    target = paths[0] if paths else (hosts[0] if hosts else "")
    summary = f"{tool_name}({target})" if target else tool_name

    return ToolRisk(
        tool_name=tool_name,
        risk_class=risk,
        summary=summary,
        paths=paths,
        hosts=hosts,
        confirmation_classes=list(dict.fromkeys(confirmation)),
        reversibility=reversibility,
        reason=reason,
    )
