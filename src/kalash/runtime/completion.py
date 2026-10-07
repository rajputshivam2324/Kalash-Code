"""Explicit caller-owned task contracts and fresh, permission-gated verification."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class CompletionStatus(StrEnum):
    STOPPED = "stopped_unverified"
    VERIFIED = "verified_complete"
    PARTIAL = "partial"
    FAILED = "failed"
    REFUSED = "refused"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class CompletionContract:
    """Supplied by a caller/user, never inferred from provider-authored claims.

    Verification establishes this explicit contract only, not universal correctness.
    Commands still pass through the ordinary permission and sandbox gate.
    """

    required_files: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()


def workspace_digest(root: Path) -> str:
    """Bounded content snapshot; refuse verification if coverage would be incomplete."""
    digest = hashlib.sha256()
    count = size = 0
    ignored = {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
    import os

    def denied(error: OSError) -> None:
        raise error

    for directory, subdirs, files in os.walk(root, followlinks=False, onerror=denied):
        for name in sorted(subdirs):
            path = Path(directory) / name
            if path.is_symlink():
                digest.update(
                    str(path.relative_to(root)).encode()
                    + b"symlink-dir"
                    + str(path.readlink()).encode()
                )
        subdirs[:] = sorted(
            d for d in subdirs if d not in ignored and not (Path(directory) / d).is_symlink()
        )
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink():
                digest.update(
                    str(path.relative_to(root)).encode()
                    + b"symlink"
                    + str(path.readlink()).encode()
                )
                continue
            count += 1
            size += path.stat().st_size
            if count > 10000 or size > 128 * 1024 * 1024:
                raise ValueError(
                    "Workspace exceeds verification snapshot bounds; define a smaller workspace"
                )
            digest.update(str(path.relative_to(root)).encode() + b"\0")
            digest.update(str(path.stat().st_mode & 0o777).encode() + b"\0")
            with path.open("rb") as stream:
                while chunk := stream.read(65536):
                    digest.update(chunk)
    return digest.hexdigest()


async def verify_contract(
    contract: CompletionContract, host: Any, executor: Any = None
) -> tuple[bool, list[dict[str, Any]], str]:
    from kalash.core.ids import generate_id

    if not contract.required_files and not contract.checks:
        return False, [], "Empty contract cannot establish verified completion"
    root = Path(host.cwd).resolve()
    for relative in contract.required_files:
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return False, [], f"Missing or out-of-workspace deliverable: {relative}"
    try:
        before = workspace_digest(root)
        receipts = []
        for command in contract.checks:
            if executor is not None:
                from kalash.models.normalize import ToolUseBlock

                result = (
                    await executor.execute(
                        [ToolUseBlock(generate_id("verify_"), "shell", {"command": command})]
                    )
                )[0]
            else:
                result = await host.execute_result(
                    "shell", {"command": command}, tool_use_id=generate_id("verify_")
                )
            receipts.append(dict(result.evidence))
            if result.is_error or result.evidence.get("exit_code") != 0:
                return False, receipts, f"Verification did not pass: {command}"
        if workspace_digest(root) != before:
            return (
                False,
                receipts,
                "Workspace changed during verification; rerun checks against the final artifact",
            )
        return True, receipts, ""
    except (OSError, ValueError) as error:
        return False, [], f"Verification unavailable: {error}"
