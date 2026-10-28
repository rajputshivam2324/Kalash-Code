"""Skills: ``<dir>/<name>/SKILL.md`` with ``name``/``description`` frontmatter.

Discovered one level deep in ``~/.kalash/skills`` and ``<project>/.kalash/skills``
(project wins on a name clash). Only name and description enter the system prompt; the
body is loaded on demand by the ``skill`` tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from kalash.paths import kalash_home

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path


def parse_skill(path: Path) -> Skill | None:
    try:
        text = path.read_text("utf-8")
    except OSError:
        return None
    match = _FRONTMATTER.match(text)
    if not match:
        return None
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None
    name = meta.get("name")
    description = meta.get("description")
    if not isinstance(name, str) or not isinstance(description, str):
        return None
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name):
        return None
    return Skill(name, " ".join(description.split()), match.group(2).strip(), path)


def discover_skills(root: Path) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    for base in (kalash_home() / "skills", root / ".kalash" / "skills"):
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*/SKILL.md")):
            skill = parse_skill(path)
            if skill is not None:
                skills[skill.name] = skill
    return skills
