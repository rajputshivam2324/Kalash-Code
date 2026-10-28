"""Recovery nudges, task-list reminders and compatibility with retired caps."""

from __future__ import annotations

from pathlib import Path

from tests.harness.scripted import call, say

# One fixed command whose result depends on a counter file, like re-running a test suite while
# the code underneath changes. `PASS_ON` makes the Nth run succeed.
COUNTER = (
    "n=$(cat .count 2>/dev/null || echo 0); n=$((n+1)); echo $n > .count; "
    'if [ "$n" = "${PASS_ON:-0}" ]; then echo all tests passed; exit 0; fi; '
    "@@FAILURE@@; exit 1"
)
SAME_ERROR = "echo 'SyntaxError: Unexpected reserved word'; echo \"at file.js:$((10+n))\""


def suite_run(description: str, failure: str = SAME_ERROR, pass_on: int = 0):  # type: ignore[no-untyped-def]
    command = COUNTER.replace("@@FAILURE@@", failure)
    if pass_on:
        command = f"PASS_ON={pass_on}; " + command
    return call("bash", {"description": description, "command": command})


def tool_texts(h) -> list[str]:  # type: ignore[no-untyped-def]
    return [m.text for m in h.session.messages() if m.role == "tool"]


def nudged(texts: list[str]) -> list[str]:
    return [t for t in texts if "[Kalash" in t]


async def test_the_same_failure_is_flagged_even_when_other_calls_are_interleaved(
    make_harness, work: Path
):
    (work / "a.js").write_text("x\n")
    h = await make_harness(
        [
            suite_run("Run the tests"),
            call("read", {"file_path": "a.js"}),
            suite_run("Run API tests again"),  # a different description is still the same attempt
            call("read", {"file_path": "a.js"}),
            suite_run("Run API tests after the fix"),
            say("giving up"),
        ]
    )
    await h.turn("fix the tests")
    runs = [t for t in tool_texts(h) if "SyntaxError" in t]
    assert len(runs) == 3
    assert "[Kalash" not in runs[0] and "[Kalash" not in runs[1]
    assert "3rd time" in runs[2] and "not the cause" in runs[2]


async def test_the_fifth_failure_tells_the_model_to_stop_and_report(make_harness):
    h = await make_harness([suite_run(f"try {n}") for n in range(5)] + [say("reported")])
    await h.turn("fix")
    runs = [t for t in tool_texts(h) if "SyntaxError" in t]
    assert "3rd time" in runs[2] and "Stop retrying" not in runs[3]
    assert "Stop retrying" in runs[4] and "Tell the user" in runs[4]


async def test_a_progressing_failure_is_not_nagged(make_harness):
    errors = [
        "echo 'ReferenceError: x is not defined'",
        "echo 'TypeError: y is not a function at module'",
        "echo 'RangeError: maximum call stack size exceeded in recursion'",
    ]
    h = await make_harness(
        [suite_run(f"run {i}", err) for i, err in enumerate(errors)] + [say("ok")]
    )
    await h.turn("iterate")
    assert nudged(tool_texts(h)) == []


async def test_a_success_in_between_resets_the_count(make_harness):
    # fails, fails, passes (3rd run), fails again - never three failures in a row
    h = await make_harness(
        [suite_run("t", pass_on=3) for _ in range(4)] + [say("ok")],
    )
    await h.turn("go")
    assert nudged(tool_texts(h)) == []


async def test_edits_of_the_same_file_that_keep_missing_are_flagged(make_harness, work: Path):
    (work / "a.js").write_text("const a = 1;\n")
    attempts = [
        call("edit", {"file_path": "a.js", "old_string": f"no {w} here", "new_string": "x"})
        for w in ("first", "second", "third")
    ]
    h = await make_harness([call("read", {"file_path": "a.js"}), *attempts, say("stuck")])
    await h.turn("edit it")
    edits = [t for t in tool_texts(h) if "old_string" in t]
    assert len(edits) == 3 and "3rd time" in edits[2]


async def test_retired_token_budget_does_not_request_early_wrap_up(make_harness):
    # each scripted step costs about 120 tokens, so 85% of 300 arrives after step 3
    h = await make_harness(
        [call("bash", {"description": "x", "command": "echo hi"}) for _ in range(6)] + [say("x")],
        limits={"max_session_tokens": 300},
    )
    result = await h.turn("work")
    warned = [t for t in tool_texts(h) if "token budget" in t]
    assert warned == []
    assert result.reason == "completed" and result.steps == 7


async def test_retired_step_cap_does_not_request_early_wrap_up(make_harness):
    h = await make_harness(
        [call("bash", {"description": "x", "command": "echo hi"}) for _ in range(10)]
        + [say("done")],
        limits={"max_steps_per_turn": 10},
    )
    result = await h.turn("work")
    warned = [t for t in tool_texts(h) if "of at most 10" in t]
    assert warned == []
    assert result.reason == "completed" and result.steps == 11


async def test_a_new_turn_starts_with_a_clean_slate(make_harness):
    h = await make_harness(
        [suite_run("a"), suite_run("b"), say("one"), suite_run("c"), suite_run("d"), say("two")]
    )
    await h.turn("first")
    await h.turn("second")
    assert nudged(tool_texts(h)) == []


def todo(*items: tuple[str, str]):  # type: ignore[no-untyped-def]
    return call("todo_write", {"todos": [{"content": c, "status": s} for c, s in items]})


def echo(n: int):  # type: ignore[no-untyped-def]
    return call("bash", {"description": f"step {n}", "command": f"echo {n}"})


async def test_a_stale_task_list_gets_a_reminder_after_eight_calls(make_harness):
    plan = todo(("Write the API", "in_progress"), ("Write the UI", "pending"))
    h = await make_harness([plan, *[echo(n) for n in range(9)], say("done")])
    await h.turn("build")
    reminders = [t for t in tool_texts(h) if "task list has not been updated" in t]
    assert len(reminders) == 1 and "Write the API" in reminders[0]
    assert "todo_write" in reminders[0]


async def test_updating_the_list_resets_the_reminder_clock(make_harness):
    items = (("Write the API", "in_progress"), ("Write the UI", "pending"))
    h = await make_harness(
        [todo(*items), *[echo(n) for n in range(6)], todo(*items), *[echo(n) for n in range(6)]]
        + [say("done")]
    )
    await h.turn("build")
    assert not [t for t in tool_texts(h) if "task list has not been updated" in t]


async def test_a_finished_list_is_left_alone(make_harness):
    done = todo(("Write the API", "completed"), ("Write the UI", "completed"))
    h = await make_harness([done, *[echo(n) for n in range(10)], say("done")])
    await h.turn("build")
    assert not [t for t in tool_texts(h) if "task list has not been updated" in t]


async def test_no_list_means_no_reminder(make_harness):
    h = await make_harness([*[echo(n) for n in range(10)], say("done")])
    await h.turn("build")
    assert not [t for t in tool_texts(h) if "task list" in t]


async def test_six_identical_failures_end_the_turn_as_stuck_and_the_session_stays_usable(
    make_harness,
):
    h = await make_harness([suite_run(f"attempt {n}") for n in range(8)] + [say("later answer")])
    result = await h.turn("fix it")
    assert result.reason == "stuck" and result.error_code == "STUCK"
    assert "6 times with the same output" in (result.error or "")
    assert result.steps == 6, "stopped at the sixth failure, not after burning the whole script"
    # the log is a valid history: every call has its result, so the next turn can proceed
    again = await h.turn("what now?")
    assert again.reason == "completed" and again.text == "later answer"


async def test_different_failures_behind_the_same_boilerplate_are_not_a_loop(make_harness):
    boilerplate = "; ".join(f"echo 'TAP line {i} of the usual runner header'" for i in range(30))
    names = [
        "parses empty input",
        "rejects negative ids",
        "keeps order stable",
        "escapes html entities",
        "handles unicode titles",
        "returns 404 for unknown id",
    ]
    h = await make_harness(
        [
            suite_run(f"run {i}", f"{boilerplate}; echo 'not ok - {name}'; echo '# fail 1'")
            for i, name in enumerate(names)
        ]
        + [say("progress")]
    )
    result = await h.turn("fix the failing tests one by one")
    assert result.reason == "completed"
    assert not any("Stop retrying" in t for t in tool_texts(h))


def test_exit_code_and_hints_cover_the_new_reasons():
    from kalash.agent.events import TurnResult
    from kalash.cli.headless import EXIT_INCOMPLETE, EXIT_LIMIT, exit_code
    from kalash.tui.describe import error_hint

    assert exit_code(TurnResult("stuck")) == EXIT_INCOMPLETE
    assert exit_code(TurnResult("limit", error_code="LIMIT_TOKENS")) == EXIT_LIMIT
    assert "no spending or step caps" in error_hint("LIMIT_TOKENS", None)
    assert "no spending or step caps" in error_hint("LIMIT_STEPS", None)
    assert "/undo" in error_hint("STUCK", None)


async def test_legacy_limits_do_not_set_stop_codes(make_harness):
    h = await make_harness(
        [call("bash", {"description": "x", "command": "echo hi"}) for _ in range(3)]
        + [say("done")],
        limits={"max_steps_per_turn": 2},
    )
    result = await h.turn("work")
    assert result.reason == "completed" and result.error_code is None
    tokens = await make_harness(
        [call("bash", {"description": "x", "command": "echo hi"}) for _ in range(3)]
        + [say("done")],
        limits={"max_session_tokens": 100},
    )
    result = await tokens.turn("work")
    assert result.reason == "completed" and result.error_code is None
