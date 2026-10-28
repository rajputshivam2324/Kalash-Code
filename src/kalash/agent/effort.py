"""Reasoning effort: what a model accepts, how to describe it, and the optional per-step policy.

Effort is a property of the *model* (the catalog says which levels each accepts), so the picker,
the keyboard cycle and ``/effort`` all start from :func:`choices` instead of a hard-coded list; a
level the model does not take is mapped to the nearest one it does (``resolve_effort``) or dropped,
never sent.

``auto`` is opt-in. Judge runs showed a ``high`` default spending 12k reasoning tokens on one
planning step and little on the routine steps around it. Under ``auto`` the model thinks at its
default level on the first step of a turn, after any failed tool result and after a harness nudge,
and at its lowest level on routine follow-ups. Providers that cache by prefix (Anthropic) may lose
cache hits when the level changes between steps, which is why it is not the default.
"""

from __future__ import annotations

from dataclasses import dataclass

from kalash.llm.catalog import OFF_WORDS, ModelInfo

AUTO = "auto"
DEFAULT = "default"
OFF = "off"

LEVEL_NOTES = {
    "minimal": "barely thinks; fastest",
    "low": "light reasoning; quick and cheap",
    "medium": "balanced",
    "high": "thorough; slower, more tokens",
    "xhigh": "very thorough; slow",
    "max": "deepest reasoning; slowest and most expensive",
}


@dataclass(frozen=True)
class Choice:
    value: str  # what to pass to ``Agent.set_effort``
    label: str
    detail: str


def levels(info: ModelInfo | None) -> list[str]:
    """The reasoning levels the model accepts, weakest first (off-spellings excluded)."""
    if info is None or info.thinking == "none":
        return []
    return [e for e in info.efforts if e not in OFF_WORDS]


def off_supported(info: ModelInfo | None) -> bool:
    """Can reasoning really be switched off? (A level list without an off value cannot.)"""
    if info is None or info.thinking == "none":
        return False
    if any(e in OFF_WORDS for e in info.efforts):
        return True
    return info.thinking in ("adaptive", "budget") and info.can_disable_thinking


def has_control(info: ModelInfo | None) -> bool:
    return bool(levels(info)) or off_supported(info)


def choices(info: ModelInfo | None) -> list[Choice]:
    """Every selectable setting for this model, in menu order."""
    if not has_control(info):
        return []
    assert info is not None
    out: list[Choice] = []
    default = info.default_effort
    out.append(
        Choice(
            DEFAULT,
            "Default",
            f"the model's own default ({default})" if default else "no level sent",
        )
    )
    available = levels(info)
    if len(available) >= 2:
        out.append(
            Choice(
                AUTO,
                "Auto",
                f"{available[0]} on routine steps, {_hard(info)} on planning and after failures",
            )
        )
    out.extend(Choice(level, level.title(), LEVEL_NOTES.get(level, "")) for level in available)
    if off_supported(info):
        out.append(Choice(OFF, "Off", "no reasoning; fastest and cheapest"))
    return out


def normalize(info: ModelInfo | None, requested: str | None) -> tuple[str | None, str | None]:
    """(stored setting, problem) for a user's request.

    ``None``/"default" -> the model default; "auto"; "off"; or a level the model takes (a nearby
    level is substituted and mentioned). A model without a reasoning control accepts nothing but the
    default; an unknown model (no catalog entry) takes what it is given.
    """
    if requested is None or requested.lower() == DEFAULT:
        return None, None
    wanted = requested.lower()
    if info is None or info.extra.get("unverified_metadata"):
        return wanted, None
    if not has_control(info):
        return None, "this model has no reasoning control"
    if wanted == AUTO:
        if len(levels(info)) < 2:
            return None, "auto needs a model with at least two reasoning levels"
        return AUTO, None
    if wanted in OFF_WORDS:
        if not off_supported(info):
            return None, f"reasoning cannot be turned off here (levels: {', '.join(levels(info))})"
        return OFF, None
    available = levels(info)
    if wanted in available:
        return wanted, None
    from kalash.llm.catalog import resolve_effort

    nearest = resolve_effort(info, wanted)
    if nearest is not None and nearest in available:
        return nearest, f"{wanted} is not offered here; using {nearest}"
    return None, f"unknown level {requested!r} (this model takes: {', '.join(available)})"


def cycle(info: ModelInfo | None, current: str | None) -> str | None:
    """The next setting for a keyboard shortcut: default -> levels (weak to strong) -> off."""
    order = [c.value for c in choices(info) if c.value != AUTO]
    if not order:
        return None
    key = current or DEFAULT
    return order[(order.index(key) + 1) % len(order)] if key in order else order[0]


def _hard(info: ModelInfo) -> str:
    available = levels(info)
    return info.default_effort if info.default_effort in available else available[-1]


def pick_for_step(
    info: ModelInfo | None, *, first_step: bool, last_failed: bool, nudged: bool
) -> str | None:
    """The level to send for one request under ``auto`` (None: send no level)."""
    available = levels(info)
    if info is None or len(available) < 2:
        return None
    if first_step or last_failed or nudged:
        return _hard(info)
    return available[0]


def label(info: ModelInfo | None, setting: str | None, current: str | None = None) -> str:
    """Short text for the status bar."""
    if setting == AUTO:
        return f"auto:{current}" if current else "auto"
    if setting is None:
        default = info.default_effort if info else None
        return default or ""
    return setting
