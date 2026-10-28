"""The command registry and its ranking (no UI): the menu must be complete and unambiguous."""

from __future__ import annotations

from kalash.modes import MODES
from kalash.tui.app import KalashApp
from kalash.tui.slash import SECTIONS, SlashCommand, builtin_commands, grouped, match_commands

COMMANDS = builtin_commands()


def names(found: list[SlashCommand]) -> list[str]:
    return [c.name for c in found]


def test_the_empty_query_lists_every_command_with_no_cap():
    assert names(match_commands("", COMMANDS)) == names(COMMANDS)
    assert len(COMMANDS) > 12, "the old menu stopped at the first dozen"


def test_every_permission_mode_has_its_own_command():
    by_mode = {c.mode: c for c in COMMANDS if c.mode}
    assert set(by_mode) == set(MODES)
    assert [c.name for c in COMMANDS[:5]] == ["ask", "auto", "plan", "read-only", "full"]
    for mode_id, command in by_mode.items():
        assert command.description == MODES[mode_id].description or mode_id in ("plan", "full")


def test_modes_come_first_and_sections_follow_the_declared_order():
    sections = [title for title, _ in grouped(COMMANDS)]
    assert sections[0] == "Modes"
    assert sections == [s for s in SECTIONS if s in sections]
    assert sum(len(group) for _, group in grouped(COMMANDS)) == len(COMMANDS)


def test_names_and_aliases_never_collide():
    seen: dict[str, str] = {}
    for command in COMMANDS:
        for word in (command.name, *command.aliases):
            assert word not in seen, f"{word!r} is both /{seen.get(word)} and /{command.name}"
            seen[word] = command.name


def test_every_command_has_a_handler_or_is_a_mode_shortcut():
    for command in COMMANDS:
        if command.mode is None:
            handler = f"_cmd_{command.name.replace('-', '_')}"
            assert hasattr(KalashApp, handler), f"/{command.name} has no {handler}"


def test_ranking_prefers_name_prefix_then_alias_then_substring_then_subsequence():
    assert names(match_commands("mo", COMMANDS))[0] == "model"
    found = names(match_commands("mod", COMMANDS))
    assert found[0] == "model" and "permissions" in found, "reachable through its /mode alias"
    assert names(match_commands("read", COMMANDS))[0] == "read-only"
    assert names(match_commands("yolo", COMMANDS)) == ["full"]
    assert names(match_commands("pms", COMMANDS))[0] == "permissions", "subsequence match"
    assert "diff" in names(match_commands("changes", COMMANDS)), "description match from 3 chars"


def test_one_character_queries_do_not_match_descriptions():
    for command in match_commands("z", COMMANDS):
        assert "z" in command.name or any("z" in a for a in command.aliases)


def test_an_unknown_query_finds_nothing_instead_of_everything():
    assert match_commands("qqqqq", COMMANDS) == []
