"""read / write / edit / glob / grep, with dsh-style schemas and server-side observation."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
from pathlib import Path
from typing import Any

from pydantic import Field

from kalash.secure_io import atomic_write
from kalash.tools.base import Effects, Params, Tool, ToolContext, ToolResult
from kalash.tools.diffing import diff_files, edit_preview, whitespace_changes
from kalash.tools.matching import edit_excerpt, explain_no_match, occurrence_lines
from kalash.tools.observe import StaleFile, read_bytes_nofollow_check
from kalash.tools.output import limit_output, truncate_line

MAX_READ_BYTES = 50_000
DEFAULT_LIMIT = 2000
GLOB_LIMIT = 100
GREP_LIMIT = 250
SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".tox",
    "dist",
    "build",
    ".next",
    "target",
}


def _decode(data: bytes) -> tuple[str, str]:
    """Decode UTF-8 (BOM aware). Returns (text, encoding)."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8"), "utf-8-sig"
    return data.decode("utf-8"), "utf-8"


class ReadParams(Params):
    file_path: str = Field(
        description="Path to read; relative paths resolve against the workspace."
    )
    offset: int = Field(default=1, ge=1, description="1-based first line to return. Defaults to 1.")
    limit: int = Field(
        default=DEFAULT_LIMIT,
        ge=1,
        description="Maximum number of lines to return. Defaults to 2000.",
    )


class ReadTool(Tool):
    name = "read"
    description = (
        "Read a UTF-8 text file and return line-numbered content (`N: text`). Read a file "
        "before you edit or overwrite it. Use offset and limit to continue reading large files."
    )
    params = ReadParams
    read_only = True

    def effects(self, params: Any, ctx: ToolContext) -> Effects:
        return Effects(reads=[ctx.resolve(params.file_path)])

    def permission_key(self, params: Any, ctx: ToolContext) -> str:
        return f"read {ctx.display(ctx.resolve(params.file_path))}"

    async def run(self, params: ReadParams, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(params.file_path)
        try:
            data = await asyncio.to_thread(read_bytes_nofollow_check, path)
        except (OSError, ValueError) as error:
            if isinstance(error, FileNotFoundError):
                ctx.observations.record_absent(path)
            return ToolResult(f"Cannot read {params.file_path}: {error}", is_error=True)
        if b"\x00" in data[:8192]:
            return ToolResult(
                f"{params.file_path} looks like a binary file ({len(data)} bytes); not shown.",
                is_error=True,
            )
        try:
            text, _ = _decode(data)
        except UnicodeDecodeError:
            return ToolResult(f"{params.file_path} is not valid UTF-8 text.", is_error=True)
        ctx.observations.record(path, data)
        lines = text.splitlines()
        total = len(lines)
        if total == 0:
            return ToolResult("(File is empty)")
        if params.offset > total:
            return ToolResult(
                f"offset {params.offset} is past the end of the file ({total} lines).",
                is_error=True,
            )
        out: list[str] = []
        size = 0
        last = params.offset - 1
        for number in range(params.offset, min(total, params.offset + params.limit - 1) + 1):
            line = f"{number}: {truncate_line(lines[number - 1])}"
            size += len(line.encode("utf-8", "replace")) + 1
            if size > MAX_READ_BYTES and out:
                break
            out.append(line)
            last = number
        footer = (
            f"(End of file - total {total} lines)"
            if last >= total
            else f"(Showing lines {params.offset}-{last} of {total}. "
            f"Use offset={last + 1} to continue.)"
        )
        return ToolResult("\n".join(out) + "\n" + footer)


class WriteParams(Params):
    file_path: str = Field(
        description="Path to write; relative paths resolve against the workspace."
    )
    content: str = Field(description="Full UTF-8 text content to write.")


class WriteTool(Tool):
    name = "write"
    description = (
        "Create a file or fully replace an existing one with UTF-8 text. An existing file must "
        "have been read first in this session; prefer `edit` for targeted changes."
    )
    params = WriteParams

    def effects(self, params: Any, ctx: ToolContext) -> Effects:
        return Effects(writes=[ctx.resolve(params.file_path)])

    def permission_key(self, params: Any, ctx: ToolContext) -> str:
        return f"write {ctx.display(ctx.resolve(params.file_path))}"

    def describe(self, params: Any, ctx: ToolContext) -> str:
        path = ctx.resolve(params.file_path)
        try:
            before = path.read_bytes() if path.is_file() else None
        except OSError:
            before = None
        diff = diff_files(str(path), before, params.content.encode("utf-8"))
        if diff is None:
            return f"{path}\n(no change)"
        label = "new file" if diff.new_file else "overwrites existing file"
        return f"{path}  ({label}, +{diff.added} -{diff.removed})\n{diff.text[:3000]}"

    async def run(self, params: WriteParams, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(params.file_path)
        try:
            existed = await asyncio.to_thread(path.exists)
            if existed:
                if path.is_dir():
                    return ToolResult(f"{params.file_path} is a directory.", is_error=True)
                current = await asyncio.to_thread(path.read_bytes)
                ctx.observations.check_unchanged(path, current)
                encoding = "utf-8-sig" if current.startswith(b"\xef\xbb\xbf") else "utf-8"
            else:
                encoding = "utf-8"
            data = params.content.encode(encoding)
            await asyncio.to_thread(atomic_write, path, data)
        except StaleFile as error:
            return ToolResult(str(error), is_error=True)
        except OSError as error:
            return ToolResult(f"Cannot write {params.file_path}: {error}", is_error=True)
        ctx.observations.record(path, data)
        verb = "Overwrote" if existed else "Created"
        lines = params.content.count("\n") + (0 if params.content.endswith("\n") else 1)
        text = f"{verb} {params.file_path} ({lines} lines, {len(data)} bytes)."
        if existed:
            text += _rewrite_note(current, data)
        return ToolResult(text)


def _rewrite_note(before: bytes, after: bytes) -> str:
    """What a whole-file rewrite actually changed - the model often cannot tell."""
    if before == after:
        return " Nothing changed: the file already had exactly this content."
    old_lines = before.decode("utf-8", "replace").splitlines()
    new_lines = after.decode("utf-8", "replace").splitlines()
    changed, ws = whitespace_changes(old_lines, new_lines)
    if changed and ws == changed:
        return (
            f" All {changed} changed lines differ only in whitespace (spacing or indentation); "
            "the code is the same as before."
        )
    if ws:
        return f" {ws} of the {changed} changed lines differ only in whitespace."
    return f" {changed} lines changed."


class EditParams(Params):
    file_path: str = Field(
        description="Path to edit; relative paths resolve against the workspace."
    )
    old_string: str = Field(description="Literal text to replace.")
    new_string: str = Field(
        description="Literal replacement text. Use an empty string to delete the match."
    )
    replace_all: bool = Field(
        default=False,
        description="Replace all matches. Defaults to false; when false, old_string must appear "
        "exactly once.",
    )


class EditTool(Tool):
    name = "edit"
    description = (
        "Edit an existing UTF-8 text file by replacing literal text. Read the file first. "
        "Include enough surrounding context in old_string to make it unique, or set replace_all."
    )
    params = EditParams

    def effects(self, params: Any, ctx: ToolContext) -> Effects:
        return Effects(writes=[ctx.resolve(params.file_path)])

    def permission_key(self, params: Any, ctx: ToolContext) -> str:
        return f"edit {ctx.display(ctx.resolve(params.file_path))}"

    def describe(self, params: Any, ctx: ToolContext) -> str:
        return edit_preview(
            params.old_string, params.new_string, str(ctx.resolve(params.file_path))
        )

    async def run(self, params: EditParams, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(params.file_path)
        if params.old_string == "":
            return ToolResult(
                "old_string must not be empty. Use `write` to create a file.", is_error=True
            )
        if params.old_string == params.new_string:
            return ToolResult(
                "old_string and new_string are identical; nothing to change.", is_error=True
            )
        try:
            if not await asyncio.to_thread(path.is_file):
                return ToolResult(
                    f"{params.file_path} does not exist (use `write` to create it).", is_error=True
                )
            data = await asyncio.to_thread(path.read_bytes)
            ctx.observations.check_unchanged(path, data)
            text, encoding = _decode(data)
        except StaleFile as error:
            return ToolResult(str(error), is_error=True)
        except UnicodeDecodeError:
            return ToolResult(f"{params.file_path} is not valid UTF-8 text.", is_error=True)
        except OSError as error:
            return ToolResult(f"Cannot read {params.file_path}: {error}", is_error=True)
        crlf = "\r\n" in text and text.count("\r\n") >= text.count("\n") / 2
        body = text.replace("\r\n", "\n") if crlf else text
        old = params.old_string.replace("\r\n", "\n")
        new = params.new_string.replace("\r\n", "\n")
        count = body.count(old)
        if count == 0:
            return ToolResult(explain_no_match(body, old, params.file_path), is_error=True)
        if count > 1 and not params.replace_all:
            where = ", ".join(str(n) for n in occurrence_lines(body, old))
            more = "" if count <= 8 else ", ..."
            return ToolResult(
                f"old_string appears {count} times in {params.file_path} (starting at lines "
                f"{where}{more}). Add surrounding context to make it unique, or set replace_all "
                "to replace every occurrence.",
                is_error=True,
            )
        first_at = body.index(old)
        updated = body.replace(old, new) if params.replace_all else body.replace(old, new, 1)
        excerpt = edit_excerpt(updated, first_at, new)
        if crlf:
            updated = updated.replace("\n", "\r\n")
        try:
            out = updated.encode(encoding)
            await asyncio.to_thread(atomic_write, path, out)
        except OSError as error:
            return ToolResult(f"Cannot write {params.file_path}: {error}", is_error=True)
        ctx.observations.record(path, out)
        replaced = count if params.replace_all else 1
        spacing = (
            " Only whitespace changed (the code is the same as before)."
            if "".join(old.split()) == "".join(new.split())
            else ""
        )
        return ToolResult(
            f"Edited {params.file_path} ({replaced} replacement{'s' if replaced != 1 else ''})."
            f"{spacing} {excerpt}"
        )


# ----------------------------------------------------------------------------- search


class GlobParams(Params):
    pattern: str = Field(
        description='Glob pattern for file paths (e.g. "**/*.py", "src/**/test_*.py"). A '
        "pattern with no / matches the basename at any depth."
    )
    path: str | None = Field(
        default=None, description="Directory to search; defaults to the workspace."
    )


class GlobTool(Tool):
    name = "glob"
    description = (
        "Find files (not directories) whose paths match a glob pattern, including hidden files "
        f"but skipping VCS/dependency/cache directories. Returns up to {GLOB_LIMIT} paths, most "
        "recently modified first, and says when more matched."
    )
    params = GlobParams
    read_only = True

    def effects(self, params: Any, ctx: ToolContext) -> Effects:
        return Effects(reads=[ctx.resolve(params.path or ".")])

    def permission_key(self, params: Any, ctx: ToolContext) -> str:
        return f"glob {params.pattern}"

    async def run(self, params: GlobParams, ctx: ToolContext) -> ToolResult:
        base = ctx.resolve(params.path or ".")
        if not base.is_dir():
            return ToolResult(f"{base} is not a directory.", is_error=True)
        found = await asyncio.to_thread(_glob_files, base, params.pattern)
        if not found:
            return ToolResult("No files matched.")
        found.sort(key=lambda item: item[1], reverse=True)
        shown = [p for p, _ in found[:GLOB_LIMIT]]
        text = "\n".join(shown)
        if len(found) > GLOB_LIMIT:
            text += f"\n(Showing {GLOB_LIMIT} of {len(found)} matches; narrow the pattern or path.)"
        return ToolResult(text)


def _glob_files(base: Path, pattern: str) -> list[tuple[str, float]]:
    anchored = "/" in pattern
    regex = _glob_regex(pattern if anchored else f"**/{pattern}")
    results: list[tuple[str, float]] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            if regex.match(rel):
                try:
                    results.append((full, os.stat(full).st_mtime))
                except OSError:
                    continue
                if len(results) > 50_000:
                    return results
    return results


def _glob_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        elif char == "{":
            end = pattern.find("}", i)
            if end == -1:
                out.append(re.escape(char))
                i += 1
            else:
                options = pattern[i + 1 : end].split(",")
                out.append(
                    "(?:"
                    + "|".join(_glob_regex(o).pattern.rstrip("$").lstrip("^") for o in options)
                    + ")"
                )
                i = end + 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("^" + "".join(out) + "$")


class GrepParams(Params):
    pattern: str = Field(description="Regular expression to search for (ripgrep syntax).")
    path: str | None = Field(
        default=None, description="File or directory to search; defaults to the workspace."
    )
    include: str | None = Field(
        default=None,
        description='One glob filter for files to search (e.g. "*.py", "*.{js,jsx}"). Not a list.',
    )


class GrepTool(Tool):
    name = "grep"
    description = (
        "Search file contents with a regular expression (ripgrep syntax). Returns matching lines "
        f"with line numbers, grouped by file, up to {GREP_LIMIT} matches. Case-sensitive."
    )
    params = GrepParams
    read_only = True

    def effects(self, params: Any, ctx: ToolContext) -> Effects:
        return Effects(reads=[ctx.resolve(params.path or ".")])

    def permission_key(self, params: Any, ctx: ToolContext) -> str:
        return f"grep {params.pattern}"

    async def run(self, params: GrepParams, ctx: ToolContext) -> ToolResult:
        base = ctx.resolve(params.path or ".")
        if not base.exists():
            return ToolResult(f"{base} does not exist.", is_error=True)
        try:
            re.compile(params.pattern)
        except re.error as error:
            if shutil.which("rg") is None:
                return ToolResult(f"Invalid regular expression: {error}", is_error=True)
        lines = (
            await _grep_rg(params, base)
            if shutil.which("rg")
            else await asyncio.to_thread(_grep_python, params, base)
        )
        if isinstance(lines, ToolResult):
            return lines
        if not lines:
            return ToolResult("No matches.")
        total = len(lines)
        shown = lines[:GREP_LIMIT]
        text = "\n".join(shown)
        if total > GREP_LIMIT:
            text += f"\n(Showing {GREP_LIMIT} of {total} matches; narrow the pattern or path.)"
        return ToolResult(limit_output(text, ctx.spill_dir, "grep"))


async def _grep_rg(params: GrepParams, base: Path) -> list[str] | ToolResult:
    command = [
        "rg",
        "--line-number",
        "--no-heading",
        "--color=never",
        "--with-filename",
        "--max-columns=500",
        "--max-columns-preview",
        "-e",
        params.pattern,
    ]
    if params.include:
        command += ["--glob", params.include]
    for name in SKIP_DIRS - {".git"}:
        command += ["--glob", f"!{name}/"]
    command += ["--", str(base)]
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await process.communicate()
    if process.returncode == 1:
        return []
    if process.returncode != 0:
        return ToolResult(f"grep failed: {err.decode('utf-8', 'replace').strip()}", is_error=True)
    return [line for line in out.decode("utf-8", "replace").splitlines() if line]


def _grep_python(params: GrepParams, base: Path) -> list[str] | ToolResult:
    try:
        regex = re.compile(params.pattern)
    except re.error as error:
        return ToolResult(f"Invalid regular expression: {error}", is_error=True)
    files: list[Path] = []
    if base.is_file():
        files = [base]
    else:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            files.extend(Path(dirpath) / n for n in filenames)
    results: list[str] = []
    for file in sorted(files):
        if params.include and not any(
            fnmatch.fnmatch(file.name, g) for g in _expand_braces(params.include)
        ):
            continue
        try:
            data = file.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:8192]:
            continue
        for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            if regex.search(line):
                results.append(f"{file}:{number}:{truncate_line(line, 500)}")
    return results


def _expand_braces(glob: str) -> list[str]:
    match = re.search(r"\{([^}]*)\}", glob)
    if not match:
        return [glob]
    return [
        g
        for option in match.group(1).split(",")
        for g in _expand_braces(glob[: match.start()] + option + glob[match.end() :])
    ]
