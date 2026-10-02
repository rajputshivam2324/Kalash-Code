"""Tests for what the user can see and take away from a run.

Three reported gaps, all of which made a working agent look broken or unusable:

* a file edit reported only a byte count, so there was no way to review a change
  without leaving the terminal;
* delegated work produced a silent pause;
* transcript text could not be selected, because Textual puts the terminal into
  mouse-reporting mode and the drag never reaches the terminal's own selection.
"""

import pytest

from kalash.core.diff import (
    MAX_DIFF_LINES,
    MAX_LINE_WIDTH,
    make_diff,
    summarize,
)
from kalash.runtime.scratchpad import reset_cache
from kalash.tools.base import ToolContext
from kalash.tools.fs import EditParams, EditTool, WriteParams, WriteTool
from tests.render import plain as render_plain


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("KALASH_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    reset_cache()
    yield project
    reset_cache()


@pytest.fixture
def ctx(workspace):
    return ToolContext(
        session_id="ses_display",
        run_id="r",
        cwd=workspace,
        writable_roots=(workspace,),
    )


class TestDiffGeneration:
    def test_new_file_is_all_additions(self):
        diff = make_diff("a.py", "", "one\ntwo\n")
        assert diff.stat.added == 2
        assert diff.stat.removed == 0
        assert "+ one" in diff.text

    def test_modification_shows_both_sides(self):
        diff = make_diff("a.py", "alpha\nbeta\n", "alpha\ngamma\n")
        assert "- beta" in diff.text
        assert "+ gamma" in diff.text
        assert diff.stat.render() == "+1/-1"

    def test_unchanged_content_is_empty(self):
        assert make_diff("a.py", "same\n", "same\n").is_empty

    def test_long_unchanged_runs_are_collapsed(self):
        before = "\n".join(f"line {i}" for i in range(200))
        after = before + "\nappended"
        diff = make_diff("a.py", before, after)
        assert "unchanged lines" in diff.text
        assert len(diff.text.splitlines()) < 40

    def test_huge_diff_is_truncated_and_flagged(self):
        before = ""
        after = "\n".join(f"new line {i}" for i in range(500))
        diff = make_diff("a.py", before, after)
        assert diff.stat.truncated
        assert len(diff.text.splitlines()) <= MAX_DIFF_LINES + 1
        assert "more diff lines" in diff.text

    def test_very_long_lines_are_elided(self):
        diff = make_diff("a.py", "", "x" * (MAX_LINE_WIDTH * 3) + "\n")
        line = diff.text.splitlines()[0]
        assert "…" in line
        assert len(line) < MAX_LINE_WIDTH * 2

    def test_summarize_is_one_line(self):
        text = summarize("a.py", "one\n", "one\ntwo\n")
        assert "\n" not in text
        assert "+1/-0" in text


class TestToolsReportDiffs:
    @pytest.mark.asyncio
    async def test_write_returns_a_diff(self, ctx, workspace):
        env = await WriteTool().execute(WriteParams(path="new.py", content="print('hi')\n"), ctx)
        assert env.ok
        assert env.metadata["diff"]
        assert "+ print('hi')" in env.metadata["diff"]
        assert env.metadata["diff_stat"] == "+1/-0"
        assert env.metadata["operation"] == "created"
        # The summary line is what a user reads first.
        assert "+1/-0" in env.content

    @pytest.mark.asyncio
    async def test_overwrite_diffs_against_the_previous_content(self, ctx, workspace):
        target = workspace / "x.py"
        target.write_text("old line\n")
        from kalash.tools.fs import _content_digest

        digest = _content_digest(target.read_bytes())

        env = await WriteTool().execute(
            WriteParams(path="x.py", content="new line\n", digest=digest), ctx
        )
        assert env.ok
        assert "- old line" in env.metadata["diff"]
        assert "+ new line" in env.metadata["diff"]
        assert env.metadata["operation"] == "modified"

    @pytest.mark.asyncio
    async def test_edit_returns_a_diff(self, ctx, workspace):
        target = workspace / "y.py"
        target.write_text("alpha\nbeta\ngamma\n")
        from kalash.tools.fs import _content_digest

        digest = _content_digest(target.read_bytes())

        env = await EditTool().execute(
            EditParams(path="y.py", old_str="beta", new_str="BETA", digest=digest), ctx
        )
        assert env.ok
        assert "- beta" in env.metadata["diff"]
        assert "+ BETA" in env.metadata["diff"]
        assert env.metadata["lines_added"] == 1
        assert env.metadata["lines_removed"] == 1

    @pytest.mark.asyncio
    async def test_diff_failure_never_blocks_the_write(self, ctx, workspace, monkeypatch):
        """The write is the contract; the diff is a display nicety."""
        monkeypatch.setattr(
            "kalash.tools.fs.write.make_diff",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("diff exploded")),
        )
        with pytest.raises(RuntimeError):
            await WriteTool().execute(WriteParams(path="z.py", content="content\n"), ctx)
        # The bytes still landed before the diff was attempted.
        assert (workspace / "z.py").exists()


class TestSubagentWidget:
    def test_reports_running_then_done(self):
        from kalash.tui.messages import SubagentLine

        line = SubagentLine("find the bug")
        assert "running" in render_plain(line.renderable)
        line.finish(turns=3, tokens=12_000)
        plain = render_plain(line.renderable)
        assert "done" in plain
        assert "3 turns" in plain
        assert "12,000" in plain

    def test_reports_failure(self):
        from kalash.tui.messages import SubagentLine

        line = SubagentLine("x")
        line.finish(failed=True)
        assert "failed" in render_plain(line.renderable)


class TestToolCallVisibility:
    def test_summary_names_the_shell_command(self):
        from kalash.tui.app import summarize_tool_call

        label = summarize_tool_call("shell", {"command": "npm install --silent"})
        assert "npm install" in label

    def test_summary_names_the_file(self):
        from kalash.tui.app import summarize_tool_call

        assert "src/app.py" in summarize_tool_call("read", {"path": "src/app.py"})

    def test_summary_survives_missing_arguments(self):
        from kalash.tui.app import summarize_tool_call

        assert summarize_tool_call("glob", {}) == "Globbed"

    def test_line_shows_elapsed_only_once_slow(self):
        from kalash.tui.messages import ToolCallLine

        line = ToolCallLine("shell  npm install")
        line.tick(0.5)
        assert "s\n" not in render_plain(line.renderable)
        line.tick(9.0)
        assert "9s" in render_plain(line.renderable)
        assert "ctrl+c" in render_plain(line.renderable)

    def test_finished_line_stops_ticking(self):
        from kalash.tui.messages import ToolCallLine

        line = ToolCallLine("shell  build")
        line.finish(duration_ms=4200)
        before = render_plain(line.renderable)
        line.tick(30.0)
        assert render_plain(line.renderable) == before
        assert "4.2s" in before

    def test_shell_summary_includes_sandbox_warning(self):
        from kalash.tui.formatting import summarize_tool_result

        summary = summarize_tool_result(
            "shell",
            {"sandbox_warning": "OS sandbox unavailable — command runs unwrapped"},
        )
        assert "unwrapped" in summary.lower()

    def test_tool_line_highlights_sandbox_warning(self):
        from kalash.tui.messages import ToolCallLine

        line = ToolCallLine("$ npm test", tool_name="shell")
        line.set_summary("runs unwrapped (OS sandbox unavailable)")
        plain = render_plain(line.renderable)
        assert "unwrapped" in plain

    def test_assistant_shows_working_state(self):
        from kalash.tui.messages import AssistantMessage

        msg = AssistantMessage()
        msg.set_working()
        assert "working" in msg.renderable.plain

    def test_list_is_a_compact_headline(self):
        from kalash.tui.app import summarize_tool_call

        assert summarize_tool_call("list", {"path": "."}).startswith("Listed")
        assert "$" in summarize_tool_call("shell", {"command": "ls"})

    def test_tool_line_folds_long_output(self):
        from kalash.tui.messages import ToolCallLine

        line = ToolCallLine("$ pytest")
        line.append_output("\n".join(f"line {i}" for i in range(20)) + "\n")
        line.finish(duration_ms=1500)
        plain = render_plain(line.renderable)
        assert "output lines hidden" in plain
        assert "ctrl+o" in plain
        line.toggle_expand()
        assert "line 19" in render_plain(line.renderable)
