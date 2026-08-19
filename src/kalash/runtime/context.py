"""Context assembly and ordering.

Assembles the context window in 9 fixed slots. The order is load-bearing
for prompt caching — changing slot order invalidates cache prefixes.

Pressure ladder (L0–L7) progressively compresses slots as token usage
approaches the budget ceiling.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from kalash.core.budget import BudgetState, estimate_tokens
from kalash.core.events import Event, EventBus, EventType
from kalash.models.normalize import ContentBlock, Message, Role, TextBlock


# ---------------------------------------------------------------------------
# Pressure levels
# ---------------------------------------------------------------------------

PRESSURE_THRESHOLDS: tuple[float, ...] = (0.60, 0.70, 0.74, 0.78, 0.82, 0.92)


class PressureLevel(IntEnum):
    """Token budget pressure — higher means more aggressive compression."""

    L0 = 0  # Comfortable headroom
    L1 = 1  # Start trimming examples
    L2 = 2  # Abbreviate tool schemas
    L3 = 3  # Summarize older turns
    L4 = 4  # Drop non-essential memory
    L5 = 5  # Aggressive compaction trigger
    L6 = 6  # Emergency: drop skill catalog
    L7 = 7  # Critical: only system + recent turns


# ---------------------------------------------------------------------------
# Slot identifiers
# ---------------------------------------------------------------------------


class Slot(IntEnum):
    """Context slots in fixed assembly order."""

    SYSTEM_IDENTITY = 0
    TOOL_SCHEMAS = 1
    SKILLS_CATALOG = 2
    ENVIRONMENT = 3
    KALASH_MD = 4
    MEMORY = 5
    COMPACTED_HISTORY = 6
    RECENT_TURNS = 7
    CURRENT_MESSAGE = 8


# Cache breakpoints emitted after these slots
CACHE_BREAKPOINTS: frozenset[Slot] = frozenset({Slot.SKILLS_CATALOG, Slot.KALASH_MD})


# ---------------------------------------------------------------------------
# Slot budget tracking
# ---------------------------------------------------------------------------


@dataclass
class SlotBudget:
    """Token accounting for a single slot."""

    slot: Slot
    allocated: int = 0
    used: int = 0
    content: list[Message] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return max(0, self.allocated - self.used)


# ---------------------------------------------------------------------------
# Context assembler
# ---------------------------------------------------------------------------


@dataclass
class ContextAssembler:
    """Assembles the context window from 9 ordered slots.

    The assembler is instantiated per-turn and produces a list of normalized
    Message objects ready for the model gateway.
    """

    budget: BudgetState
    context_window: int
    event_bus: EventBus | None = None

    # Internal state
    _slots: dict[Slot, SlotBudget] = field(init=False, default_factory=dict)
    _pressure: PressureLevel = field(init=False, default=PressureLevel.L0)

    def __post_init__(self) -> None:
        for slot in Slot:
            self._slots[slot] = SlotBudget(slot=slot)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def assemble(
        self,
        *,
        system_identity: str,
        tool_schemas: list[dict[str, Any]],
        skills_catalog: list[dict[str, str]],
        environment: dict[str, str] | None = None,
        kalash_md_chain: list[str],
        memory_blocks: list[str],
        compacted_summary: str | None = None,
        recent_turns: list[Message],
        current_message: Message,
        estimated_prompt_tokens: int | None = None,
    ) -> list[Message]:
        """Assemble context in slot order. Returns normalized Message list."""
        self._compute_pressure(estimated_prompt_tokens=estimated_prompt_tokens)
        self._allocate_budgets()

        # Slot 0: System identity + contract (static)
        self._fill_slot(
            Slot.SYSTEM_IDENTITY,
            self._build_system_identity(system_identity),
        )

        # Slot 1: Tool schemas (static per session)
        self._fill_slot(
            Slot.TOOL_SCHEMAS,
            self._build_tool_schemas(tool_schemas),
        )

        # Slot 2: Skills catalog (name + description only)
        self._fill_slot(
            Slot.SKILLS_CATALOG,
            self._build_skills_catalog(skills_catalog),
        )

        # Slot 3: Environment (cwd, git state, platform)
        env = environment or self._detect_environment()
        self._fill_slot(
            Slot.ENVIRONMENT,
            self._build_environment(env),
        )

        # Slot 4: KALASH.md hierarchy (user > project > nearest)
        self._fill_slot(
            Slot.KALASH_MD,
            self._build_kalash_md(kalash_md_chain),
        )

        # Slot 5: Memory block (every turn)
        self._fill_slot(
            Slot.MEMORY,
            self._build_memory(memory_blocks),
        )

        # Slot 6: Compacted history summary
        self._fill_slot(
            Slot.COMPACTED_HISTORY,
            self._build_compacted_history(compacted_summary),
        )

        # Slot 7: Recent verbatim turns
        self._fill_slot(
            Slot.RECENT_TURNS,
            self._build_recent_turns(recent_turns),
        )

        # Slot 8: Current user message
        self._fill_slot(
            Slot.CURRENT_MESSAGE,
            [current_message],
        )

        return self._emit_messages()

    @property
    def pressure_level(self) -> PressureLevel:
        return self._pressure

    @property
    def total_tokens_used(self) -> int:
        return sum(sb.used for sb in self._slots.values())

    # ------------------------------------------------------------------
    # Pressure and budget
    # ------------------------------------------------------------------

    def _compute_pressure(self, *, estimated_prompt_tokens: int | None = None) -> None:
        """Determine pressure level from context fill or session budget usage."""
        if estimated_prompt_tokens is not None and self.context_window > 0:
            ratio = estimated_prompt_tokens / self.context_window
        else:
            ratio = self.budget.tokens_used / max(self.budget.max_tokens, 1)
        level = PressureLevel.L0
        for i, threshold in enumerate(PRESSURE_THRESHOLDS):
            if ratio >= threshold:
                level = PressureLevel(i + 1)
        self._pressure = min(level, PressureLevel.L7)

    def _allocate_budgets(self) -> None:
        """Distribute token budget across slots based on pressure level."""
        available = self.context_window

        # Fixed allocations for static slots
        static_pct = {
            Slot.SYSTEM_IDENTITY: 0.10,
            Slot.TOOL_SCHEMAS: 0.15,
            Slot.SKILLS_CATALOG: 0.05 if self._pressure < PressureLevel.L6 else 0.0,
            Slot.ENVIRONMENT: 0.03,
            Slot.KALASH_MD: 0.08,
            Slot.MEMORY: 0.08 if self._pressure < PressureLevel.L4 else 0.03,
            Slot.COMPACTED_HISTORY: 0.12,
            Slot.RECENT_TURNS: 0.30,
            Slot.CURRENT_MESSAGE: 0.09,
        }

        for slot, pct in static_pct.items():
            self._slots[slot].allocated = int(available * pct)

    # ------------------------------------------------------------------
    # Slot builders
    # ------------------------------------------------------------------

    def _build_system_identity(self, identity: str) -> list[Message]:
        text = identity.strip()
        if not text:
            return []
        return [Message(role=Role.SYSTEM, content=[TextBlock(text=text)])]

    def _build_tool_schemas(self, schemas: list[dict[str, Any]]) -> list[Message]:
        if not schemas:
            return []
        # At high pressure, abbreviate to name+description only
        if self._pressure >= PressureLevel.L2:
            abbreviated = "\n".join(
                f"- {s.get('name', '?')}: {s.get('description', '')[:80]}"
                for s in schemas
            )
            text = f"<tools>\n{abbreviated}\n</tools>"
        else:
            import json
            text = f"<tools>\n{json.dumps(schemas, indent=2)}\n</tools>"
        return [Message(role=Role.SYSTEM, content=[TextBlock(text=text)])]

    def _build_skills_catalog(self, skills: list[dict[str, str]]) -> list[Message]:
        if not skills or self._pressure >= PressureLevel.L6:
            return []
        entries = "\n".join(
            f"- {s['name']}: {s.get('description', '')}" for s in skills
        )
        text = f"<skills>\n{entries}\n</skills>"
        return [Message(role=Role.SYSTEM, content=[TextBlock(text=text)])]

    def _build_environment(self, env: dict[str, str]) -> list[Message]:
        lines = [f"{k}: {v}" for k, v in env.items()]
        text = f"<environment>\n" + "\n".join(lines) + "\n</environment>"
        return [Message(role=Role.SYSTEM, content=[TextBlock(text=text)])]

    def _build_kalash_md(self, chain: list[str]) -> list[Message]:
        if not chain:
            return []
        combined = "\n---\n".join(chain)
        text = f"<kalash_md>\n{combined}\n</kalash_md>"
        return [Message(role=Role.SYSTEM, content=[TextBlock(text=text)])]

    def _build_memory(self, blocks: list[str]) -> list[Message]:
        if not blocks or self._pressure >= PressureLevel.L4:
            return []
        combined = "\n".join(blocks)
        if combined.lstrip().startswith("<memory>"):
            text = combined
        else:
            text = f"<memory>\n{combined}\n</memory>"
        return [Message(role=Role.SYSTEM, content=[TextBlock(text=text)])]

    def _build_compacted_history(self, summary: str | None) -> list[Message]:
        if not summary:
            return []
        text = f"<compacted_history>\n{summary}\n</compacted_history>"
        return [Message(role=Role.USER, content=[TextBlock(text=text)])]

    def _build_recent_turns(self, turns: list[Message]) -> list[Message]:
        if not turns:
            return []
        # At high pressure, trim from the front (keep most recent)
        if self._pressure >= PressureLevel.L3:
            budget = self._slots[Slot.RECENT_TURNS].allocated
            kept: list[Message] = []
            tokens_so_far = 0
            for msg in reversed(turns):
                msg_tokens = self._estimate_message_tokens(msg)
                if tokens_so_far + msg_tokens > budget:
                    break
                kept.insert(0, msg)
                tokens_so_far += msg_tokens
            return kept
        return list(turns)

    def _build_current_message(self, message: Message) -> list[Message]:
        return [message]

    # ------------------------------------------------------------------
    # Emission
    # ------------------------------------------------------------------

    def _fill_slot(self, slot: Slot, messages: list[Message]) -> None:
        sb = self._slots[slot]
        sb.content = messages
        sb.used = sum(self._estimate_message_tokens(m) for m in messages)

    def _emit_messages(self) -> list[Message]:
        """Emit all slot messages in order, inserting cache breakpoints."""
        result: list[Message] = []
        for slot in Slot:
            sb = self._slots[slot]
            result.extend(sb.content)
            # Cache breakpoints after designated slots
            if slot in CACHE_BREAKPOINTS and sb.content:
                # Insert a cache breakpoint marker (provider-specific handling
                # happens in the gateway)
                result.append(
                    Message(
                        role=Role.SYSTEM,
                        content=[TextBlock(text="<cache_breakpoint/>")],
                    )
                )
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _estimate_message_tokens(self, msg: Message) -> int:
        """Estimate token count for a message."""
        total = 0
        for block in msg.content:
            if hasattr(block, "text"):
                total += estimate_tokens(block.text)  # type: ignore[attr-defined]
            elif hasattr(block, "input") and isinstance(getattr(block, "input"), dict):
                import json
                total += estimate_tokens(json.dumps(getattr(block, "input")))
            elif hasattr(block, "content") and isinstance(block.content, str):  # type: ignore[union-attr]
                total += estimate_tokens(block.content)  # type: ignore[union-attr]
            else:
                total += 50  # Rough estimate for non-text blocks
        return total

    @staticmethod
    def _detect_environment() -> dict[str, str]:
        """Auto-detect current environment."""
        import os
        import subprocess

        env: dict[str, str] = {
            "cwd": os.getcwd(),
            "platform": platform.system().lower(),
            "python": platform.python_version(),
        }

        # Try to get git state
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0:
                env["git_branch"] = result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        return env
