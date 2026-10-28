"""Prompt history, custom slash commands, git info, diffs."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from kalash import commands, gitinfo, history
from kalash.paths import kalash_home
from kalash.tools.diffing import diff_files, edit_preview

# ----------------------------------------------------------------------------- history


def test_history_roundtrip_dedupes_consecutive_and_caps():
    entries: list[str] = []
    for text in ["one", "two", "two", "  ", "three"]:
        entries = history.add(entries, text)
    assert entries == ["one", "two", "three"] and history.load() == entries
    big = []
    for i in range(history.MAX_ENTRIES + 25):
        big = history.add(big, f"prompt {i}")
    assert len(history.load()) == history.MAX_ENTRIES and history.load()[-1].endswith("524")


def test_history_ignores_a_damaged_file_and_oversized_prompts():
    kalash_home().mkdir(parents=True)
    history.history_path().write_text('"ok"\n{broken\n123\n"also ok"\n')
    assert history.load() == ["ok", "also ok"]
    assert history.add([], "x" * (history.MAX_LENGTH + 1)) == []
    history.history_path().unlink()
    assert history.load() == []


def test_history_file_is_private():
    history.add([], "secret prompt")
    assert history.history_path().stat().st_mode & 0o077 == 0


# ----------------------------------------------------------------------------- commands


def test_custom_commands_expand_arguments_and_project_overrides_user(work: Path):
    user = kalash_home() / "commands"
    user.mkdir(parents=True)
    (user / "review.md").write_text("USER review $ARGUMENTS")
    (user / "Bad Name.md").write_text("ignored")
    project = work / ".kalash" / "commands"
    project.mkdir(parents=True)
    (project / "review.md").write_text(
        '---\ndescription: "Review a file"\n---\nReview $1 for $2 issues. All: $ARGUMENTS'
    )
    (project / "fix.md").write_text("# Fix the thing\nFix it now.")
    found = commands.discover(work)
    assert set(found) == {"review", "fix"}
    review = found["review"]
    assert review.description == "Review a file"
    assert review.expand('src/a.py "security bug"') == (
        'Review src/a.py for security bug issues. All: src/a.py "security bug"'
    )
    assert review.expand("") == "Review  for  issues. All:"
    assert found["fix"].description == "Fix the thing"


# ----------------------------------------------------------------------------- git


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(cwd),
        },
    )


async def test_git_status_reports_branch_dirty_files_and_none_outside_a_repo(work: Path):
    assert await gitinfo.status(work, force=True) is None
    git(work, "init", "-q", "-b", "feature")
    (work / "a.txt").write_text("1")
    git(work, "add", "a.txt")
    git(work, "commit", "-q", "-m", "init")
    clean = await gitinfo.status(work, force=True)
    assert clean is not None and clean.branch == "feature" and clean.dirty == 0
    (work / "a.txt").write_text("2")
    (work / "b.txt").write_text("new")
    dirty = await gitinfo.status(work, force=True)
    assert dirty is not None and dirty.dirty == 2
    assert "a.txt" in (await gitinfo.diff_summary(work) or "")
    files = await gitinfo.tracked_files(work)
    assert sorted(files) == ["a.txt", "b.txt"]


def test_shorten_path():
    home = Path("/home/u")
    assert gitinfo.shorten_path(Path("/home/u/proj/x"), home) == "~/proj/x"
    assert gitinfo.shorten_path(Path("/srv/app"), home) == "/srv/app"
    assert gitinfo.shorten_path(home, home) == "~"


# ----------------------------------------------------------------------------- diffs


def test_diff_files_counts_lines_and_flags_new_files():
    diff = diff_files("a.py", b"one\ntwo\nthree\n", b"one\nTWO\nthree\nfour\n")
    assert diff is not None and (diff.added, diff.removed) == (2, 1) and not diff.new_file
    assert "-two" in diff.text and "+TWO" in diff.text and "+four" in diff.text
    created = diff_files("n.py", None, b"x\ny\n")
    assert created is not None and created.new_file and created.added == 2


def test_diff_files_skips_unchanged_binary_and_huge():
    assert diff_files("a", b"same", b"same") is None
    assert diff_files("a", b"\x00\x01", b"\x00\x02") is None
    assert diff_files("a", b"x" * 2_000_000, b"y") is None


def test_diff_is_truncated_for_big_changes():
    after = "\n".join(f"line {i}" for i in range(2000)).encode()
    diff = diff_files("a", b"", after)
    assert diff is not None and diff.truncated and len(diff.text.splitlines()) == 400


def test_edit_preview_is_bounded():
    text = edit_preview("a\nb\nc\n" * 100, "x\n" * 100, "f.py")
    assert text.startswith("f.py") and text.endswith("...")
    assert json.dumps(text)


def test_system_prompt_carries_the_lessons_from_live_runs(work: Path):
    from kalash.agent.prompt import build_system_prompt

    prompt = build_system_prompt(
        tool_names={"read", "edit", "write", "bash", "job_output", "job_kill", "todo_write"}
    )
    assert "Never replace an existing file with a shell redirect" in prompt
    assert "`job_output` and `job_kill` are tools, not shell commands" in prompt
    assert "pkill -f" in prompt
    assert "the file:line it names" in prompt
    without_jobs = build_system_prompt(tool_names={"bash"})
    assert "job_kill" not in without_jobs
