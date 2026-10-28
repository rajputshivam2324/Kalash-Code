"""read / write / edit / glob / grep behaviour."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from kalash.tools.fs import EditTool, GlobTool, GrepTool, ReadTool, WriteTool
from tests.harness.ctx import make_ctx, run_tool


@pytest.fixture
def ctx(work: Path):
    return make_ctx(work)


async def test_read_numbers_lines_one_based_and_marks_eof(ctx, work: Path):
    (work / "a.txt").write_text("alpha\nbeta\ngamma\n")
    result = await run_tool(ReadTool(), ctx, file_path="a.txt")
    assert result.text == "1: alpha\n2: beta\n3: gamma\n(End of file - total 3 lines)"


async def test_read_offset_limit_and_continuation_hint(ctx, work: Path):
    (work / "a.txt").write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    result = await run_tool(ReadTool(), ctx, file_path="a.txt", offset=4, limit=3)
    assert result.text.splitlines()[:3] == ["4: line4", "5: line5", "6: line6"]
    assert "Use offset=7 to continue" in result.text
    tail = await run_tool(ReadTool(), ctx, file_path="a.txt", offset=9, limit=50)
    assert "End of file" in tail.text and "Use offset" not in tail.text


async def test_read_errors_are_actionable(ctx, work: Path):
    (work / "d").mkdir()
    (work / "bin").write_bytes(b"\x00\x01binary")
    (work / "a.txt").write_text("x\n")
    for path, needle in (("missing.txt", "does not exist"), ("d", "directory"), ("bin", "binary")):
        result = await run_tool(ReadTool(), ctx, file_path=path)
        assert result.is_error and needle in result.text
    past = await run_tool(ReadTool(), ctx, file_path="a.txt", offset=99)
    assert past.is_error and "past the end" in past.text


async def test_read_long_lines_are_cut(ctx, work: Path):
    (work / "w.txt").write_text("x" * 5000 + "\n")
    result = await run_tool(ReadTool(), ctx, file_path="w.txt")
    assert "more chars]" in result.text and len(result.text) < 2500


async def test_empty_file(ctx, work: Path):
    (work / "e.txt").write_text("")
    assert (await run_tool(ReadTool(), ctx, file_path="e.txt")).text == "(File is empty)"


async def test_write_creates_with_normal_permissions_and_parents(ctx, work: Path):
    result = await run_tool(WriteTool(), ctx, file_path="pkg/mod/new.py", content="x = 1\n")
    assert "Created" in result.text
    path = work / "pkg" / "mod" / "new.py"
    assert path.read_text() == "x = 1\n"
    assert stat.S_IMODE(path.stat().st_mode) & 0o044, "created files must not be 0600"


async def test_write_overwrite_requires_read_and_then_works(ctx, work: Path):
    (work / "f.txt").write_text("old")
    blocked = await run_tool(WriteTool(), ctx, file_path="f.txt", content="new")
    assert blocked.is_error and "has not been read" in blocked.text
    assert (work / "f.txt").read_text() == "old"
    await run_tool(ReadTool(), ctx, file_path="f.txt")
    ok = await run_tool(WriteTool(), ctx, file_path="f.txt", content="new")
    assert "Overwrote" in ok.text and (work / "f.txt").read_text() == "new"
    again = await run_tool(WriteTool(), ctx, file_path="f.txt", content="newer")
    assert not again.is_error  # own write updated the observation


async def test_write_preserves_mode_of_existing_file(ctx, work: Path):
    script = work / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    await run_tool(ReadTool(), ctx, file_path="run.sh")
    await run_tool(WriteTool(), ctx, file_path="run.sh", content="#!/bin/sh\necho hi\n")
    assert stat.S_IMODE(script.stat().st_mode) == 0o755


async def test_write_to_directory_is_a_clear_error(ctx, work: Path):
    (work / "d").mkdir()
    await run_tool(ReadTool(), ctx, file_path=".")  # observing a dir is not a read
    result = await run_tool(WriteTool(), ctx, file_path="d", content="x")
    assert result.is_error and "directory" in result.text


async def test_edit_unique_replace_and_ambiguity(ctx, work: Path):
    (work / "a.py").write_text("a = 1\nb = 1\nc = 1\n")
    await run_tool(ReadTool(), ctx, file_path="a.py")
    ambiguous = await run_tool(
        EditTool(), ctx, file_path="a.py", old_string="= 1", new_string="= 2"
    )
    assert ambiguous.is_error and "3 times" in ambiguous.text and "replace_all" in ambiguous.text
    one = await run_tool(EditTool(), ctx, file_path="a.py", old_string="b = 1", new_string="b = 9")
    assert not one.is_error
    everything = await run_tool(
        EditTool(), ctx, file_path="a.py", old_string="= 1", new_string="= 0", replace_all=True
    )
    assert "2 replacements" in everything.text
    assert (work / "a.py").read_text() == "a = 0\nb = 9\nc = 0\n"


@pytest.mark.parametrize(
    ("old", "new", "needle"),
    [
        ("", "x", "must not be empty"),
        ("same", "same", "identical"),
        ("not present", "x", "was not found"),
    ],
)
async def test_edit_rejections(ctx, work: Path, old, new, needle):
    (work / "a.txt").write_text("same\n")
    await run_tool(ReadTool(), ctx, file_path="a.txt")
    result = await run_tool(EditTool(), ctx, file_path="a.txt", old_string=old, new_string=new)
    assert result.is_error and needle in result.text
    assert (work / "a.txt").read_text() == "same\n"


async def test_edit_normalises_crlf_both_ways_and_preserves_line_endings(ctx, work: Path):
    (work / "w.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    await run_tool(ReadTool(), ctx, file_path="w.txt")
    result = await run_tool(
        EditTool(), ctx, file_path="w.txt", old_string="one\ntwo", new_string="ONE\nTWO"
    )
    assert not result.is_error
    assert (work / "w.txt").read_bytes() == b"ONE\r\nTWO\r\nthree\r\n"


async def test_edit_keeps_bom_and_non_ascii(ctx, work: Path):
    (work / "b.txt").write_bytes(b"\xef\xbb\xbfcaf\xc3\xa9 = 1\n")
    await run_tool(ReadTool(), ctx, file_path="b.txt")
    await run_tool(EditTool(), ctx, file_path="b.txt", old_string="= 1", new_string="= 2 ✓")
    assert (work / "b.txt").read_bytes().startswith(b"\xef\xbb\xbfcaf\xc3\xa9 = 2 \xe2\x9c\x93")


async def test_edit_detects_modification_after_read(ctx, work: Path):
    (work / "a.txt").write_text("v1\n")
    await run_tool(ReadTool(), ctx, file_path="a.txt")
    (work / "a.txt").write_text("v2 by someone else\n")
    result = await run_tool(EditTool(), ctx, file_path="a.txt", old_string="v2", new_string="v3")
    assert result.is_error and "changed since you last read" in result.text


async def test_edit_is_atomic_on_failure(ctx, work: Path):
    (work / "a.txt").write_text("keep\n")
    await run_tool(ReadTool(), ctx, file_path="a.txt")
    await run_tool(EditTool(), ctx, file_path="a.txt", old_string="nothing", new_string="x")
    assert (work / "a.txt").read_text() == "keep\n"
    assert not [p for p in work.iterdir() if p.name.endswith(".tmp")]


def test_unknown_parameters_are_rejected_by_the_schema():
    with pytest.raises(ValidationError):
        EditTool().parse({"file_path": "a", "old_string": "b", "new_string": "c", "regex": True})


def test_tool_schemas_are_compact_and_strict():
    schema = EditTool().spec().parameters
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"file_path", "old_string", "new_string"}
    assert "title" not in schema and "default" not in schema["properties"]["replace_all"]


# ----------------------------------------------------------------------------- glob / grep


async def test_glob_matches_basename_anywhere_and_anchored_patterns(ctx, work: Path):
    for rel in ("a.py", "src/b.py", "src/deep/c.py", "src/d.txt", "node_modules/x/e.py"):
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    anywhere = (await run_tool(GlobTool(), ctx, pattern="*.py")).text.splitlines()
    assert sorted(Path(p).name for p in anywhere) == ["a.py", "b.py", "c.py"]
    anchored = (await run_tool(GlobTool(), ctx, pattern="src/*.py")).text.splitlines()
    assert [Path(p).name for p in anchored] == ["b.py"]
    braces = (await run_tool(GlobTool(), ctx, pattern="**/*.{py,txt}")).text.splitlines()
    assert len(braces) == 4  # node_modules is skipped


async def test_glob_orders_by_recent_modification_and_caps(ctx, work: Path):
    for number in range(120):
        path = work / f"f{number:03}.log"
        path.write_text("")
        os.utime(path, (1_000_000 + number, 1_000_000 + number))
    text = (await run_tool(GlobTool(), ctx, pattern="*.log")).text
    lines = text.splitlines()
    assert Path(lines[0]).name == "f119.log" and "Showing 100 of 120" in lines[-1]


async def test_glob_without_matches_and_bad_directory(ctx, work: Path):
    assert (await run_tool(GlobTool(), ctx, pattern="*.zzz")).text == "No files matched."
    assert (await run_tool(GlobTool(), ctx, pattern="*", path="nope")).is_error


async def test_grep_finds_lines_with_numbers_and_filters(ctx, work: Path):
    (work / "a.py").write_text("def alpha():\n    return 1\n")
    (work / "b.txt").write_text("def not_python\n")
    result = await run_tool(GrepTool(), ctx, pattern=r"def \w+", include="*.py")
    assert "a.py:1:def alpha" in result.text and "b.txt" not in result.text
    assert (await run_tool(GrepTool(), ctx, pattern="zzzzzz")).text == "No matches."


async def test_grep_is_case_sensitive_and_reports_bad_regex(ctx, work: Path):
    (work / "a.txt").write_text("Hello\n")
    assert (await run_tool(GrepTool(), ctx, pattern="hello")).text == "No matches."
    bad = await run_tool(GrepTool(), ctx, pattern="(")
    assert bad.is_error


async def test_grep_caps_matches(ctx, work: Path):
    (work / "many.txt").write_text("hit\n" * 400)
    text = (await run_tool(GrepTool(), ctx, pattern="hit")).text
    assert "Showing 250 of 400" in text


async def test_grep_python_fallback_matches_ripgrep_shape(ctx, work: Path, monkeypatch):
    (work / "a.py").write_text("needle here\n")
    monkeypatch.setattr("shutil.which", lambda _name: None)
    result = await run_tool(GrepTool(), ctx, pattern="needle", include="*.{py,md}")
    assert "a.py:1:needle here" in result.text
