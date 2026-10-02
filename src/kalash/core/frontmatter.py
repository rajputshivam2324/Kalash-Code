"""Bounded Markdown frontmatter shared by skills and agent definitions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

MAX_HEADER_CHARS = 16_000
MAX_BODY_CHARS = 100_000


def read_metadata(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        if stream.readline().strip() != "---":
            return {}
        lines: list[str] = []
        size = 0
        for line in stream:
            if line.strip() == "---":
                metadata = yaml.safe_load("".join(lines))
                if not isinstance(metadata, dict):
                    raise ValueError(f"Frontmatter must be a mapping: {path}")
                return metadata
            size += len(line)
            if size > MAX_HEADER_CHARS:
                raise ValueError(f"Frontmatter exceeds {MAX_HEADER_CHARS} characters: {path}")
            lines.append(line)
    raise ValueError(f"Unclosed frontmatter: {path}")


def read_body(path: Path) -> str:
    with path.open(encoding="utf-8") as stream:
        text = stream.read(MAX_BODY_CHARS + MAX_HEADER_CHARS + 10)
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        if end is None:
            raise ValueError(f"Unclosed frontmatter: {path}")
        text = "".join(lines[end + 1 :])
    if len(text) > MAX_BODY_CHARS:
        raise ValueError(f"Body exceeds {MAX_BODY_CHARS} characters: {path}")
    return text.strip()
