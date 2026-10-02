"""Versioned local coding tasks with evaluator-owned verification."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CodingTask:
    id: str
    prompt: str
    files: dict[str, str]
    hidden_test: str
    scripted_edits: tuple[dict[str, str], ...] = ()


def load_tasks(path: Path) -> list[CodingTask]:
    tasks = []
    identifiers: set[str] = set()
    for row in json.loads(path.read_text()):
        task = CodingTask(**{**row, "scripted_edits": tuple(row.get("scripted_edits", []))})
        if task.id in identifiers or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", task.id):
            raise ValueError(f"Invalid or duplicate task ID: {task.id!r}")
        identifiers.add(task.id)
        for name in task.files:
            path = Path(name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.parts[0] != "src"
                or path.suffix != ".py"
            ):
                raise ValueError(f"Task file must be under src/: {name}")
        tasks.append(task)
    if not tasks:
        raise ValueError("A benchmark requires at least one task")
    return tasks
