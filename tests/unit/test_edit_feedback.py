"""What ``edit`` tells a model when it misses, and what it shows when it lands.

Regression for a live run (Sarvam 105B, React/Node app): six near-miss edits and six full re-reads
in a row because "old_string was not found" named no cause and a successful edit showed nothing.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from kalash.tools.fs import EditTool, ReadTool
from tests.harness.ctx import make_ctx, run_tool

SOURCE = (
    "import http from 'node:http';\n"
    "\n"
    "function request(url, body) {\n"
    "  return new Promise((resolve, reject) => {\n"
    "    const server = http.createServer();\n"
    "    server.listen(0, () => {\n"
    "      const options = { method: 'POST' };\n"
    "      if (body) options.body = JSON.stringify(body);\n"
    "      const res = fetch(url, options);\n"
    "      resolve(res);\n"
    "    });\n"
    "  });\n"
    "}\n"
)


@pytest.fixture
def ctx(work: Path):
    return make_ctx(work)


async def edit(ctx, work: Path, old: str, new: str, **kw):  # type: ignore[no-untyped-def]
    (work / "t.js").write_text(SOURCE)
    await run_tool(ReadTool(), ctx, file_path="t.js")
    return await run_tool(EditTool(), ctx, file_path="t.js", old_string=old, new_string=new, **kw)


async def test_copied_line_number_prefixes_are_named_as_the_cause(ctx, work: Path):
    old = "9: const res = fetch(url, options);\n10: resolve(res);"
    result = await edit(ctx, work, old, "x")
    assert result.is_error
    assert "line number" in result.text and "display only" in result.text
    assert "was not found" in result.text


async def test_wrong_indentation_shows_the_real_lines_with_numbers(ctx, work: Path):
    old = "const res = fetch(url, options);\nresolve(res);"  # indentation lost
    result = await edit(ctx, work, old, "x")
    assert result.is_error
    assert "indentation" in result.text and "lines 9-10" in result.text
    assert "9:       const res = fetch(url, options);" in result.text
    assert "10:       resolve(res);" in result.text


async def test_a_near_miss_points_at_the_closest_text(ctx, work: Path):
    old = "      const response = await fetch(url, opts);\n      resolve(response);"
    result = await edit(ctx, work, old, "x")
    assert result.is_error
    assert "closest text is at lines 9-10" in result.text and "% similar" in result.text
    assert "9:       const res = fetch(url, options);" in result.text


async def test_nothing_resembling_it_says_so_and_suggests_grep(ctx, work: Path):
    result = await edit(ctx, work, "completely::unrelated(\n  tokens)", "x")
    assert result.is_error
    assert "Nothing in the file resembles it" in result.text and "grep" in result.text


async def test_ambiguous_match_lists_where_the_candidates_are(ctx, work: Path):
    result = await edit(ctx, work, "options", "opts")
    assert result.is_error and "3 times" in result.text
    assert "starting at lines 7, 8, 9" in result.text


async def test_a_failed_edit_leaves_the_file_untouched(ctx, work: Path):
    await edit(ctx, work, "no such text", "x")
    assert (work / "t.js").read_text() == SOURCE


async def test_a_successful_edit_shows_the_changed_region_with_context(ctx, work: Path):
    result = await edit(
        ctx, work, "const res = fetch(url, options);", "const res = await fetch(url, options);"
    )
    assert not result.is_error
    assert "Edited t.js (1 replacement)." in result.text
    assert "Now lines 7-11:" in result.text
    assert "9:       const res = await fetch(url, options);" in result.text
    assert "7:       const options = { method: 'POST' };" in result.text  # context above
    assert "11:     });" in result.text  # context below


async def test_excerpt_follows_a_multi_line_replacement(ctx, work: Path):
    new = (
        "      const res = await fetch(url, options);\n      log(res.status);\n      resolve(res);"
    )
    result = await edit(
        ctx, work, "      const res = fetch(url, options);\n      resolve(res);", new
    )
    assert "9:       const res = await fetch(url, options);" in result.text
    assert "10:       log(res.status);" in result.text
    assert "11:       resolve(res);" in result.text


async def test_excerpt_of_a_very_long_replacement_is_bounded(ctx, work: Path):
    new = "\n".join(f"line {i}" for i in range(60))
    result = await edit(ctx, work, "resolve(res);", new)
    assert not result.is_error
    assert "the edit continues to line" in result.text
    assert result.text.count("\n") < 20


async def test_excerpt_of_replace_all_shows_the_first_occurrence(ctx, work: Path):
    result = await edit(ctx, work, "options", "opts", replace_all=True)
    assert "3 replacements" in result.text
    assert "7:       const opts = { method: 'POST' };" in result.text


async def test_excerpt_of_a_crlf_file_is_plain_lines(ctx, work: Path):
    (work / "w.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    await run_tool(ReadTool(), ctx, file_path="w.txt")
    result = await run_tool(EditTool(), ctx, file_path="w.txt", old_string="two", new_string="TWO")
    assert "2: TWO" in result.text and "\r" not in result.text


async def test_diagnosis_of_a_huge_file_stays_fast(ctx, work: Path):
    (work / "big.txt").write_text(
        "".join(f"row number {i} of a large generated file\n" for i in range(25_000))
    )
    await run_tool(ReadTool(), ctx, file_path="big.txt", limit=5)
    started = time.monotonic()
    result = await run_tool(
        EditTool(),
        ctx,
        file_path="big.txt",
        old_string="row number 7 of a lorge generated file",
        new_string="x",
    )
    assert result.is_error and time.monotonic() - started < 3.0


# ------------------------------------------------------------ whole-file rewrites (write)

QUIZ = (
    "const questions = [\n"
    "  {\n"
    "    id: 7,\n"
    "    options: [\n"
    "      { id: 0, label: 'a' },\n"
    "      { id: 1, label: 'b' },\n"
    "    ],\n"
    "  },\n"
    "];\n"
)


async def rewrite(ctx, work: Path, content: str):  # type: ignore[no-untyped-def]
    from kalash.tools.fs import WriteTool

    (work / "server.js").write_text(QUIZ)
    await run_tool(ReadTool(), ctx, file_path="server.js")
    return await run_tool(WriteTool(), ctx, file_path="server.js", content=content)


async def test_a_rewrite_that_only_realigns_whitespace_says_so(ctx, work: Path):
    realigned = QUIZ.replace("{ id: 0,", "{ id:   0,").replace("{ id: 1,", "{ id:   1,")
    result = await rewrite(ctx, work, realigned)
    assert not result.is_error and result.text.startswith("Overwrote server.js")
    assert "All 2 changed lines differ only in whitespace" in result.text
    assert "the code is the same as before" in result.text


async def test_a_rewrite_with_the_same_content_reports_that_nothing_changed(ctx, work: Path):
    result = await rewrite(ctx, work, QUIZ)
    assert "Nothing changed: the file already had exactly this content." in result.text


async def test_a_mixed_rewrite_counts_the_whitespace_part(ctx, work: Path):
    mixed = QUIZ.replace("{ id: 0,", "{ id:   0,").replace("label: 'b'", "label: 'B!'")
    result = await rewrite(ctx, work, mixed)
    assert "1 of the 2 changed lines differ only in whitespace" in result.text


async def test_a_real_rewrite_just_counts_the_changed_lines(ctx, work: Path):
    result = await rewrite(ctx, work, QUIZ.replace("id: 7", "id: 8").replace("'a'", "'z'"))
    assert "2 lines changed." in result.text and "whitespace" not in result.text


async def test_creating_a_file_has_no_rewrite_note(ctx, work: Path):
    from kalash.tools.fs import WriteTool

    result = await run_tool(WriteTool(), ctx, file_path="fresh.js", content="x = 1\n")
    assert result.text.startswith("Created fresh.js") and "whitespace" not in result.text


async def test_an_edit_that_only_changes_spacing_is_called_out(ctx, work: Path):
    result = await edit(
        ctx, work, "const options = { method: 'POST' };", "const options  =  { method:  'POST' };"
    )
    assert not result.is_error and "Only whitespace changed" in result.text


def test_file_diffs_flag_whitespace_only_changes_for_the_ui():
    from kalash.tools.diffing import diff_files

    before = QUIZ.encode()
    realigned = QUIZ.replace("{ id: 0,", "{ id:   0,").encode()
    assert diff_files("f", before, realigned).whitespace_only is True  # type: ignore[union-attr]
    changed = QUIZ.replace("id: 7", "id: 8").encode()
    assert diff_files("f", before, changed).whitespace_only is False  # type: ignore[union-attr]
    assert diff_files("f", None, before).whitespace_only is False  # type: ignore[union-attr]
