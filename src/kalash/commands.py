"""Custom slash commands: ``<project>/.kalash/commands/<name>.md`` and ``~/.kalash/commands``.

A file's text is the prompt. ``$ARGUMENTS`` is replaced by whatever follows the command, and
``$1``..``$9`` by the individual words. Optional frontmatter ``description:`` is shown in the
popup. Project commands override user ones. The command only *expands to a prompt* - it has no
extra authority.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from kalash.paths import kalash_home

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


@dataclass(frozen=True)
class CustomCommand:
    name: str
    description: str
    template: str
    path: Path

    def expand(self, arguments: str) -> str:
        try:
            words = shlex.split(arguments)
        except ValueError:
            words = arguments.split()
        text = self.template.replace("$ARGUMENTS", arguments.strip())
        for number in range(9, 0, -1):
            value = words[number - 1] if number <= len(words) else ""
            text = text.replace(f"${number}", value)
        return text.strip()


def discover(root: Path) -> dict[str, CustomCommand]:
    commands: dict[str, CustomCommand] = {}
    for base in (kalash_home() / "commands", root / ".kalash" / "commands"):
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.md")):
            name = path.stem.lower()
            if not _NAME.match(name):
                continue
            try:
                text = path.read_text("utf-8")
            except OSError:
                continue
            description = ""
            match = _FRONT.match(text)
            if match:
                for line in match.group(1).splitlines():
                    key, _, value = line.partition(":")
                    if key.strip() == "description":
                        description = value.strip().strip("\"'")
                text = match.group(2)
            if not description:
                first = next((ln.strip("# ").strip() for ln in text.splitlines() if ln.strip()), "")
                description = first[:80] or f"custom command /{name}"
            commands[name] = CustomCommand(name, description, text.strip(), path)
    return commands
