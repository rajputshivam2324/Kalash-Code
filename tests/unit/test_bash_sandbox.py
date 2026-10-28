"""bash, background jobs, output limiting, environment scrubbing and the OS sandbox."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from kalash.paths import kalash_home
from kalash.sandbox import SandboxPolicy, bwrap_argv, clean_env, detect, seatbelt_profile
from kalash.tools.bash import BashTool, JobKillTool, JobOutputTool
from kalash.tools.output import INLINE_BYTES, limit_output
from tests.harness.ctx import make_ctx, run_tool

needs_sandbox = pytest.mark.skipif(not detect().ok, reason="no working OS sandbox on this host")


async def bash(ctx, command: str, **kw):
    return await run_tool(BashTool(), ctx, description="test", command=command, **kw)


@pytest.fixture
async def ctx(work: Path):
    context = make_ctx(work, mode="danger-full-access")  # plain behaviour, no sandbox needed
    yield context
    await context.jobs.shutdown()


async def test_exit_code_and_combined_output(ctx):
    ok = await bash(ctx, "echo out; echo err >&2")
    assert ok.text.splitlines()[:2] == ["out", "err"] and ok.text.endswith("[exit code: 0]")
    assert not ok.is_error
    bad = await bash(ctx, "echo boom; exit 3")
    assert bad.is_error and bad.text.endswith("[exit code: 3]")


async def test_workdir_and_fresh_shell_each_call(ctx, work: Path):
    (work / "sub").mkdir()
    assert "sub" in (await bash(ctx, "pwd", workdir="sub")).text
    await bash(ctx, "export LEAK=1")
    assert "LEAK=" not in (await bash(ctx, "env")).text
    missing = await bash(ctx, "pwd", workdir="nope")
    assert missing.is_error and "not a directory" in missing.text


async def test_secret_environment_variables_are_not_passed(ctx, monkeypatch):
    monkeypatch.setenv("MY_SERVICE_API_KEY", "super-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("HARMLESS", "visible")
    out = (await bash(ctx, "env")).text
    assert "super-secret" not in out and "ghp_secret" not in out and "HARMLESS=visible" in out


def test_clean_env_pass_through_is_explicit():
    env = clean_env({"FOO_TOKEN": "t", "PLAIN": "p"}, pass_env=["FOO_TOKEN"])
    assert env["FOO_TOKEN"] == "t" and env["PLAIN"] == "p"
    assert "FOO_TOKEN" not in clean_env({"FOO_TOKEN": "t"})


async def test_timeout_moves_command_to_background_instead_of_killing_it(ctx, work: Path):
    started = time.monotonic()
    result = await bash(ctx, "echo started; sleep 30", timeout_ms=1000)
    assert time.monotonic() - started < 5
    assert "started" in result.text and "moved to background as job1" in result.text
    job = ctx.jobs.get("job1")
    assert job is not None and job.running
    killed = await run_tool(JobKillTool(), ctx, job_id="job1")
    assert "Stopped job1" in killed.text and not job.running


async def test_background_job_output_is_incremental(ctx):
    started = await bash(ctx, "echo first; sleep 0.4; echo second", run_in_background=True)
    assert "job1" in started.text
    first = await run_tool(JobOutputTool(), ctx, job_id="job1", wait_ms=5000)
    assert "first" in first.text and "second" in first.text
    assert "finished (exit code 0)" in first.text
    again = await run_tool(JobOutputTool(), ctx, job_id="job1")
    assert "first" not in again.text
    unknown = await run_tool(JobOutputTool(), ctx, job_id="job99")
    assert unknown.is_error and "Known jobs: job1" in unknown.text


async def test_backgrounded_grandchild_does_not_hang_the_call(ctx):
    started = time.monotonic()
    result = await bash(ctx, "(sleep 20 &) ; echo done", timeout_ms=10_000)
    assert time.monotonic() - started < 5
    assert "done" in result.text and result.text.endswith("[exit code: 0]")


async def test_cancellation_kills_the_process_group(ctx, work: Path):
    import asyncio

    marker = work / "alive"
    task = asyncio.create_task(bash(ctx, f"sleep 1; touch {marker}; sleep 30"))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(1.5)
    assert not marker.exists() and not ctx.jobs.get("job1").running


def test_large_output_keeps_head_and_tail_and_spills_the_rest(work: Path):
    head, tail = "HEAD-LINE\n", "FINAL-SUMMARY: 3 failed\n"
    text = head + ("x" * 100 + "\n") * 2000 + tail
    out = limit_output(text, work / "spill")
    assert out.startswith("HEAD-LINE") and out.rstrip().endswith("3 failed")
    assert len(out.encode()) < INLINE_BYTES
    spilled = next((work / "spill").iterdir())
    assert spilled.read_text() == text and "saved to" in out and str(spilled) in out
    assert limit_output("short", work / "spill") == "short"


async def test_huge_command_output_is_bounded_and_failure_tail_survives(ctx):
    result = await bash(ctx, "seq 1 200000; echo THE-END; exit 1")
    assert len(result.text) < 40_000 and "THE-END" in result.text
    assert "[exit code: 1]" in result.text and "saved to" in result.text


# ----------------------------------------------------------------------------- sandbox


def test_bwrap_argv_has_constant_shape_and_does_not_walk_the_workspace(work: Path):
    (work / "ca.pem").write_text("-----BEGIN CERTIFICATE-----")
    (work / "tests").mkdir()
    (work / "tests" / "fixture.key").write_text("k")
    policy = SandboxPolicy("workspace-write", work, kalash_home() / "tmp" / "s")
    argv = bwrap_argv(policy, work)
    text = " ".join(argv)
    assert "--unshare-pid" in argv and "--die-with-parent" in argv
    assert f"--bind {work} {work}" in text
    assert ".pem" not in text and ".key" not in text.replace("auth.key", "")
    assert "--unshare-net" not in argv
    assert "--unshare-net" in bwrap_argv(
        SandboxPolicy("workspace-write", work, kalash_home() / "tmp" / "s", network=False), work
    )


def test_read_only_policy_does_not_bind_the_workspace_writable(work: Path):
    argv = bwrap_argv(SandboxPolicy("read-only", work, kalash_home() / "tmp" / "s"), work)
    pairs = [(argv[i], argv[i + 1]) for i, a in enumerate(argv) if a in ("--bind", "--ro-bind")]
    assert ("--bind", str(work)) not in pairs
    assert ("--ro-bind", str(work)) in pairs


def test_git_hooks_and_kalash_dir_are_read_only_inside_the_sandbox(work: Path):
    (work / ".git" / "hooks").mkdir(parents=True)
    (work / ".kalash").mkdir()
    argv = bwrap_argv(SandboxPolicy("workspace-write", work, kalash_home() / "tmp" / "s"), work)
    for protected in (work / ".git" / "hooks", work / ".kalash"):
        i = argv.index(str(protected))
        assert argv[i - 1] == "--ro-bind" and argv[i + 1] == str(protected)


def test_seatbelt_profile_passes_paths_as_parameters_not_text(work: Path):
    weird = work / 'dir with "quotes" (and parens)'
    weird.mkdir()
    profile, params = seatbelt_profile(
        SandboxPolicy("workspace-write", weird, kalash_home() / "tmp" / "s")
    )
    assert "quotes" not in profile and any("ROOT0=" in p and "quotes" in p for p in params)
    assert "(deny file-write*)" in profile


@pytest.fixture
async def sbx(work: Path):
    context = make_ctx(work, mode="workspace-write")
    yield context
    await context.jobs.shutdown()


@pytest.fixture
def outside_dir():
    """A real directory outside the workspace and outside /tmp (which the sandbox replaces)."""
    import shutil
    import tempfile

    path = Path(tempfile.mkdtemp(dir="/var/tmp", prefix="kalash-test-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@needs_sandbox
async def test_sandbox_allows_workspace_writes_and_blocks_others(sbx, work: Path, outside_dir):
    outside = outside_dir / "outside.txt"
    ok = await bash(sbx, "echo inside > inside.txt && cat inside.txt")
    assert "inside" in ok.text and (work / "inside.txt").exists()
    blocked = await bash(sbx, f"echo nope > {outside}")
    assert blocked.is_error and not outside.exists()
    assert "[sandbox:" in blocked.text and "policy" in blocked.text


@needs_sandbox
async def test_read_only_mode_blocks_workspace_writes(work: Path):
    ctx = make_ctx(work, mode="read-only")
    try:
        result = await bash(ctx, "echo x > f.txt")
        assert result.is_error and not (work / "f.txt").exists()
        assert (await bash(ctx, "ls")).text.endswith("[exit code: 0]")
    finally:
        await ctx.jobs.shutdown()


@needs_sandbox
async def test_sandbox_protects_git_hooks_kalash_dir_and_credentials(sbx, work: Path):
    (work / ".git" / "hooks").mkdir(parents=True)
    (work / ".kalash").mkdir()
    kalash_home().mkdir(parents=True, exist_ok=True)
    (kalash_home() / "auth.json").write_text('{"k": "secret"}')
    hook = await bash(sbx, "echo evil > .git/hooks/pre-commit")
    cfg = await bash(sbx, "echo '{}' > .kalash/settings.local.json")
    creds = await bash(sbx, f"cat {kalash_home() / 'auth.json'}")
    assert hook.is_error and cfg.is_error
    assert not (work / ".git" / "hooks" / "pre-commit").exists()
    assert not (work / ".kalash" / "settings.local.json").exists()
    assert "secret" not in creds.text


@needs_sandbox
async def test_sandbox_isolates_pids_and_gives_a_private_tmp(sbx):
    procs = await bash(sbx, "ls /proc | grep -c '^[0-9]'")
    assert int(procs.text.split()[0]) < 15, "host processes must not be visible"
    await bash(sbx, "echo kept > /tmp/scratch.txt")
    assert "kept" in (await bash(sbx, "cat /tmp/scratch.txt")).text  # persists across calls
    assert not Path("/tmp/scratch.txt").exists()  # but is not the host /tmp


@needs_sandbox
async def test_sandbox_wrap_cost_is_small_even_with_a_big_workspace(sbx, work: Path):
    for i in range(3000):
        (work / f"f{i}.pem").write_text("x")
    started = time.monotonic()
    result = await bash(sbx, "true")
    assert result.text.endswith("[exit code: 0]") and time.monotonic() - started < 3


@needs_sandbox
@pytest.mark.parametrize(("network", "reachable"), [(True, True), (False, False)])
async def test_network_setting_controls_reachability(work: Path, network, reachable):
    import socket

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    ctx = make_ctx(work, mode="workspace-write", network=network)
    try:
        probe = f"python3 -c \"import socket; socket.create_connection(('127.0.0.1', {port}), 2)\""
        result = await bash(ctx, probe)
        assert (not result.is_error) is reachable, result.text
    finally:
        server.close()
        await ctx.jobs.shutdown()


async def test_unavailable_sandbox_refuses_instead_of_running_unconfined(work: Path, monkeypatch):
    from kalash import sandbox

    monkeypatch.setattr(sandbox, "_status_cache", {})
    monkeypatch.setattr(sandbox.shutil, "which", lambda _n: None)
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    ctx = make_ctx(work, mode="workspace-write")
    try:
        result = await bash(ctx, "touch ran.txt")
    finally:
        monkeypatch.undo()
        sandbox._status_cache.clear()
        await ctx.jobs.shutdown()
    assert result.is_error and "unavailable" in result.text and "bubblewrap" in result.text
    assert not (work / "ran.txt").exists()


async def test_require_escalated_runs_unsandboxed(sbx, outside_dir):
    outside = outside_dir / "escalated.txt"
    result = await bash(sbx, f"echo yes > {outside}", require_escalated=True)
    assert not result.is_error and outside.read_text() == "yes\n"
