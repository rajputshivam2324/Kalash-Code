"""Load a named skill body and bounded references on demand."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from kalash.skills.loader import SkillLoader
from kalash.tools.base import SideEffect, ToolContext, ToolEnvelope

# Bundled reference files can be long; cap what one call returns.
MAX_REFERENCE_CHARS = 20_000


class SkillParams(BaseModel):
    """Parameters for loading a skill."""

    name: str = Field(default="", description="Skill name to load. Omit to list all.")
    references: bool = Field(
        default=False,
        description="Also load the skill's bundled reference files.",
    )
    resource: str = Field(default="", description="One relative resource path to read on demand")
    offset: int = Field(default=0, ge=0, le=1_000_000, description="Resource character offset")
    limit: int = Field(default=20_000, ge=1, le=20_000, description="Max resource characters")


class SkillTool:
    """Loads a skill's instructions on demand."""

    def __init__(self, project_dir: Path | None = None) -> None:
        self._project_dir = project_dir
        self._loader: SkillLoader | None = None

    def _get_loader(self, ctx: ToolContext) -> SkillLoader:
        if self._loader is None:
            self._loader = SkillLoader(self._project_dir or ctx.cwd)
            self._loader.discover_now()
        return self._loader

    @property
    def name(self) -> str:
        return "skill"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return (
            "Load a skill's full instructions by name. Skills appear as names and "
            "descriptions in the <skills> catalog; call this to get the body when "
            "one is relevant to the task. Omit 'name' to list what is installed."
        )

    @property
    def params(self) -> type[BaseModel]:
        return SkillParams

    @property
    def side_effect(self) -> SideEffect:
        return SideEffect.READ

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"fs.read"})

    @property
    def timeout_s(self) -> float:
        return 15.0

    @property
    def max_output_bytes(self) -> int:
        return 131_072

    @property
    def idempotent(self) -> bool:
        return True

    @property
    def cancellable(self) -> bool:
        return False

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:
        return frozenset()

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        assert isinstance(args, SkillParams)
        loader = self._get_loader(ctx)

        if args.resource:
            try:
                content, more = loader.read_resource(
                    args.name, args.resource, offset=args.offset, limit=args.limit
                )
            except (OSError, UnicodeError, ValueError) as exc:
                return ToolEnvelope.fail("KALASH_TOOL_ERROR", str(exc), recoverable=True)
            suffix = (f"\n[more: offset={args.offset + len(content)}]") if more else ""
            return ToolEnvelope.success(
                content=f"# resource: {args.name}/{args.resource}\n{content}{suffix}",
                metadata={"skill": args.name, "resource": args.resource, "more": more},
            )

        if not args.name.strip():
            names = loader.list_names()
            if not names:
                return ToolEnvelope.success(
                    content=(
                        "No skills installed. Skills are SKILL.md files in "
                        "./.kalash/skills/ or ~/.kalash/skills/."
                    ),
                    metadata={"count": 0},
                )
            lines = []
            for name in sorted(names):
                entry = loader.get_entry(name)
                description = entry.metadata.description if entry else ""
                lines.append(f"- {name}: {description}")
            return ToolEnvelope.success(content="\n".join(lines), metadata={"count": len(names)})

        body = await loader.load_body(args.name)
        if body is None:
            known = ", ".join(sorted(loader.list_names())) or "none installed"
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"Unknown skill {args.name!r}. Available: {known}",
                recoverable=True,
            )

        parts = [f"# skill: {args.name}", body.strip()]

        if args.references:
            references = await loader.load_references(args.name)
            for filename, text in sorted(references.items()):
                clipped = text[:MAX_REFERENCE_CHARS]
                suffix = "\n[truncated]" if len(text) > MAX_REFERENCE_CHARS else ""
                parts.append(f"## reference: {filename}\n{clipped}{suffix}")

        entry = loader.get_entry(args.name)
        if entry is not None:
            parts.append(f"Skill directory: {entry.path.parent.resolve()}")
        resources = loader.list_resources(args.name)
        if resources:
            parts.append(
                "Available resources (contents not loaded):\n"
                + "\n".join(f"- {path}" for path in resources)
                + "\nRead one with skill(name=..., resource=..., offset=..., limit=...). "
                "Scripts execute only through shell under the normal permissions."
            )
        return ToolEnvelope.success(
            content="\n\n".join(parts),
            metadata={
                "skill": args.name,
                "source": entry.source if entry else "",
                "allowed_tools": list(entry.metadata.allowed_tools) if entry else [],
            },
        )
