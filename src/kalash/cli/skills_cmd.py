"""Skills CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

skills_app = typer.Typer(help="Skills management")
console = Console()


@skills_app.command("ls")
def ls(
    project: Annotated[
        bool,
        typer.Option("--project", help="Show project skills only"),
    ] = False,
) -> None:
    """List discovered skills (name + description only)."""
    from kalash.skills.loader import SkillLoader

    loader = SkillLoader(Path.cwd())
    entries = loader.discover_now()

    table = Table(title="Skills")
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Source", style="yellow")
    table.add_column("Description")

    for entry in sorted(entries.values(), key=lambda e: e.metadata.name):
        if project and entry.source != "project":
            continue
        table.add_row(
            entry.metadata.name,
            entry.source,
            entry.metadata.description[:100],
        )

    console.print(table)


@skills_app.command("validate")
def validate(
    path: Annotated[
        Optional[str],
        typer.Argument(help="Skill directory (defaults to all discovered)"),
    ] = None,
) -> None:
    """Validate SKILL.md frontmatter and naming rules."""
    from kalash.skills.loader import SkillLoader

    loader = SkillLoader(Path.cwd())
    if path:
        skill_path = Path(path)
        errors = loader.validate_skill(skill_path)
        if errors:
            for err in errors:
                console.print(f"[red]✗[/red] {err}")
            raise typer.Exit(code=1)
        console.print(f"[green]✓[/green] {skill_path.name} is valid")
        return

    entries = loader.discover_now()
    problems = 0
    for entry in entries.values():
        errors = loader.validate_skill(entry.path.parent)
        if errors:
            problems += 1
            console.print(f"[red]{entry.metadata.name}[/red]")
            for err in errors:
                console.print(f"  • {err}")
    if problems:
        raise typer.Exit(code=1)
    console.print(f"[green]✓[/green] {len(entries)} skill(s) validated")


@skills_app.command("new")
def new(
    name: Annotated[str, typer.Argument(help="Skill name (lowercase, hyphens)")],
    description: Annotated[
        str,
        typer.Option("--description", "-d", help="Short description with trigger phrases"),
    ] = "",
) -> None:
    """Scaffold a new skill under .kalash/skills/."""
    import re

    if not re.fullmatch(r"[a-z0-9-]{1,64}", name):
        console.print("[red]Name must match [a-z0-9-] (1–64 chars)[/red]")
        raise typer.Exit(code=1)

    root = Path.cwd() / ".kalash" / "skills" / name
    if root.exists():
        console.print(f"[red]Skill directory already exists:[/red] {root}")
        raise typer.Exit(code=1)

    root.mkdir(parents=True)
    desc = description or f"Use when working on {name.replace('-', ' ')} tasks."
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n\nDescribe how to use this skill.\n",
        encoding="utf-8",
    )
    console.print(f"[green]Created skill at[/green] {root / 'SKILL.md'}")
