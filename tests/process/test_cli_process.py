"""The real ``kalash`` entry point as a subprocess, talking HTTP to a loopback model server."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from kalash.paths import kalash_home
from tests.harness.mock_server import MockLLMServer, text_response, tool_call_response


@pytest.fixture
def server() -> Iterator[MockLLMServer]:
    srv = MockLLMServer().start()
    yield srv
    srv.stop()
    assert srv.unexpected == 0, "the CLI made a model request the test did not script"


@pytest.fixture(autouse=True)
def mock_provider(server: MockLLMServer) -> None:
    kalash_home().mkdir(parents=True, exist_ok=True)
    (kalash_home() / "settings.json").write_text(
        json.dumps(
            {
                "providers": {
                    "mock": {
                        "kind": "openai",
                        "base_url": server.base_url,
                        "models": ["m"],
                        "requires_key": False,
                    }
                },
            }
        )
    )


def run(
    *args: str, work: Path, stdin: str | None = None, timeout: float = 60
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "NO_COLOR": "1", "PYTHONUNBUFFERED": "1"}
    return subprocess.run(
        [sys.executable, "-m", "kalash", *args],
        cwd=work,
        env=env,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def test_headless_text_run_executes_tools_and_prints_only_the_final_answer(
    server: MockLLMServer, work: Path
):
    server.reply(
        200, tool_call_response("c1", "write", {"file_path": "hello.txt", "content": "hi\n"})
    )
    server.reply(200, text_response("Created hello.txt."))
    proc = run("-p", "make hello.txt", "-m", "mock/m", work=work)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "Created hello.txt.\n"
    assert (work / "hello.txt").read_text() == "hi\n"
    assert "> write hello.txt" in proc.stderr
    assert len(server.requests) == 2
    tool_message = server.requests[1]["messages"][-1]
    assert tool_message["role"] == "tool" and "Created hello.txt" in tool_message["content"]
    assert server.requests[0]["tools"] and server.requests[0]["messages"][0]["role"] == "system"


def test_json_output_has_accounting_and_session_id(server: MockLLMServer, work: Path):
    server.reply(200, text_response("done", prompt_tokens=123, completion_tokens=45))
    proc = run("-p", "hi", "-m", "mock/m", "--output-format", "json", work=work)
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True and payload["exit_code"] == 0 and payload["result"] == "done"
    assert payload["usage"]["input_tokens"] == 123 and payload["usage"]["output_tokens"] == 45
    assert payload["model"] == "mock/m" and payload["steps"] == 1 and payload["denials"] == []
    assert list((kalash_home() / "sessions").rglob(f"{payload['session_id']}.jsonl"))


def test_stream_json_emits_ordered_events_ending_with_the_result(server: MockLLMServer, work: Path):
    server.reply(200, tool_call_response("c1", "glob", {"pattern": "*.nothing"}))
    server.reply(200, text_response("none found"))
    proc = run("-p", "find", "-m", "mock/m", "--output-format", "stream-json", work=work)
    events = [json.loads(line) for line in proc.stdout.splitlines()]
    kinds = [e["type"] for e in events]
    assert kinds.index("tool_start") < kinds.index("tool_result") < kinds.index("text")
    assert kinds[-1] == "result" and events[-1]["ok"] is True


def test_prompt_can_come_from_stdin(server: MockLLMServer, work: Path):
    server.reply(200, text_response("from stdin"))
    proc = run("-m", "mock/m", work=work, stdin="what is this?\n")
    assert proc.returncode == 0 and proc.stdout == "from stdin\n"
    assert "what is this?" in json.dumps(server.requests[0]["messages"])


def test_denied_action_is_visible_in_exit_code_and_output(server: MockLLMServer, work: Path):
    outside = work.parent / "outside.txt"
    server.reply(
        200, tool_call_response("c1", "write", {"file_path": str(outside), "content": "x"})
    )
    server.reply(200, text_response("All done, I wrote it!"))
    proc = run("-p", "write it", "-m", "mock/m", "--output-format", "json", work=work)
    payload = json.loads(proc.stdout)
    assert proc.returncode == 3 and payload["ok"] is False and payload["reason"] == "completed"
    assert len(payload["denials"]) == 1 and "outside the workspace" in payload["denials"][0]
    assert not outside.exists()


def test_approval_never_denies_protected_writes_without_prompting(
    server: MockLLMServer, work: Path
):
    (work / ".kalash").mkdir()
    server.reply(
        200,
        tool_call_response("c1", "write", {"file_path": ".kalash/settings.json", "content": "{}"}),
    )
    server.reply(200, text_response("could not"))
    proc = run("-p", "go", "-m", "mock/m", "--approval", "never", work=work)
    assert proc.returncode == 3 and "approval prompts are disabled" in proc.stderr
    assert not (work / ".kalash" / "settings.json").exists()


def test_continue_restores_history_from_the_session_log(server: MockLLMServer, work: Path):
    server.reply(200, text_response("first answer"))
    server.reply(200, text_response("second answer"))
    assert run("-p", "remember 42", "-m", "mock/m", work=work).returncode == 0
    proc = run("-p", "what number?", "-c", "-m", "mock/m", work=work)
    assert proc.returncode == 0 and proc.stdout == "second answer\n"
    roles = [m["role"] for m in server.requests[1]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert len(list((kalash_home() / "sessions").rglob("*.jsonl"))) == 1


def test_resume_by_id_prefix_and_unknown_id(server: MockLLMServer, work: Path):
    server.reply(200, text_response("one"))
    first = run("-p", "a", "-m", "mock/m", "--output-format", "json", work=work)
    sid = json.loads(first.stdout)["session_id"]
    server.reply(200, text_response("two"))
    resumed = run("-p", "b", "-r", sid[:14], "-m", "mock/m", work=work)
    assert resumed.returncode == 0 and resumed.stdout == "two\n"
    missing = run("-p", "b", "-r", "does-not-exist", "-m", "mock/m", work=work)
    assert missing.returncode == 2 and "no session" in missing.stderr


def test_provider_auth_error_exits_1_with_the_provider_message(server: MockLLMServer, work: Path):
    server.reply(401, json.dumps({"error": {"message": "Incorrect API key"}}).encode())
    proc = run("-p", "hi", "-m", "mock/m", work=work)
    assert proc.returncode == 1 and "Incorrect API key" in proc.stderr
    assert "Traceback" not in proc.stderr and len(server.requests) == 1


def test_transient_failure_is_retried_and_succeeds(server: MockLLMServer, work: Path):
    server.reply(429, json.dumps({"error": {"message": "slow"}}).encode())
    server.reply(200, text_response("recovered"))
    # the server answers 429 without Retry-After: backoff starts at 0.5s
    proc = run("-p", "hi", "-m", "mock/m", work=work)
    assert proc.returncode == 0 and proc.stdout == "recovered\n"
    assert "[retry 1" in proc.stderr


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        (["-p", "hi", "--output-format", "xml"], "--output-format must be one of"),
        (["-p", "hi", "--sandbox", "bogus"], "permissions.sandbox"),
        (["-p", "hi", "--approval", "sometimes"], "permissions.approval"),
        (["-p", "hi", "-m", "openai/gpt-5.6-sol"], "OPENAI_API_KEY"),
        (["-p", "hi", "-m", "nonsense"], "cannot resolve model"),
        (["-p", "  "], "empty prompt"),
    ],
)
def test_usage_and_config_errors_exit_2_without_a_traceback(work: Path, args, needle):
    proc = run(*args, work=work)
    assert proc.returncode == 2, proc.stderr
    assert needle in proc.stderr and "Traceback" not in proc.stderr


def test_json_mode_reports_startup_errors_as_json(work: Path):
    proc = run("-p", "hi", "-m", "openai/gpt-5.6-sol", "--output-format", "json", work=work)
    payload = json.loads(proc.stdout)
    assert proc.returncode == 2 and payload["ok"] is False and "OPENAI_API_KEY" in payload["error"]


def test_no_model_selected_explains_how_to_choose_one(work: Path):
    proc = run("-p", "hi", work=work)
    assert proc.returncode == 2 and "kalash model" in proc.stderr


def test_sigint_cancels_the_run_closes_the_session_and_exits_130(server: MockLLMServer, work: Path):
    server.reply(200, text_response("never delivered"), delay=30)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    process = subprocess.Popen(
        [sys.executable, "-m", "kalash", "-p", "slow", "-m", "mock/m", "--output-format", "json"],
        cwd=work,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 15
    while not server.requests and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.requests, "the CLI never reached the model"
    process.send_signal(signal.SIGINT)
    out, err = process.communicate(timeout=20)
    payload = json.loads(out)
    assert process.returncode == 130 and payload["reason"] == "cancelled", err
    log = next((kalash_home() / "sessions").rglob("*.jsonl")).read_text().splitlines()
    assert json.loads(log[-1])["type"] == "turn_end"
    assert json.loads(log[-1])["reason"] == "interrupted"


# ----------------------------------------------------------------------------- other commands


def test_version_and_help(work: Path):
    assert run("--version", work=work).stdout.startswith("kalash 0.2")
    assert "--output-format" in run("--help", work=work).stdout


def test_auth_login_list_and_logout_never_echo_the_key(work: Path):
    login = run("auth", "login", "openai", "--key", "sk-super-secret", work=work)
    assert login.returncode == 0 and "sk-super-secret" not in login.stdout + login.stderr
    listing = run("auth", "list", work=work).stdout
    assert "openai" in listing and "stored" in listing
    assert "sk-super-secret" not in (kalash_home() / "auth.json").read_text()
    run("auth", "logout", "openai", work=work)
    assert "stored" not in run("auth", "list", work=work).stdout.split("openai")[1].split("\n")[0]


def test_provider_command_lists_stores_and_removes_keys(work: Path):
    listing = run("provider", work=work).stdout
    assert "sarvam" in listing and "anthropic" in listing
    stored = run("provider", "sarvam", "--key", "sk-provider-secret", work=work)
    assert stored.returncode == 0 and "sk-provider-secret" not in stored.stdout + stored.stderr
    sarvam_row = next(ln for ln in run("provider", work=work).stdout.splitlines() if "sarvam" in ln)
    assert "stored" in sarvam_row
    assert "sk-provider-secret" not in (kalash_home() / "auth.json").read_text()
    assert run("provider", "sarvam", "--remove", work=work).returncode == 0
    gone = next(ln for ln in run("provider", work=work).stdout.splitlines() if "sarvam" in ln)
    assert "stored" not in gone
    unknown = run("provider", "nowhere", "--key", "x", work=work)
    assert unknown.returncode == 2 and "unknown provider" in unknown.stderr


def test_missing_key_message_points_at_the_provider_command(work: Path):
    result = run("-p", "hi", "-m", "anthropic/claude-sonnet-5-5", work=work)
    assert result.returncode == 2 and "kalash provider anthropic" in result.stderr


def test_model_selection_is_persisted_and_validated(work: Path):
    assert run("model", "anthropic/claude-sonnet-5-5", work=work).returncode == 0
    assert "anthropic/claude-sonnet-5-5" in run("model", work=work).stdout
    bad = run("model", "no-such-model", work=work)
    assert bad.returncode == 2 and "cannot resolve" in bad.stderr


def test_config_set_validates_and_rolls_back(work: Path):
    ok = run("config", "set", "permissions.approval", "never", work=work)
    assert ok.returncode == 0
    shown = json.loads(run("config", "show", work=work).stdout)
    assert shown["permissions"]["approval"] == "never"
    bad = run("config", "set", "permissions.approval", "sometimes", work=work)
    assert bad.returncode == 2 and "not saved" in bad.stderr
    assert json.loads(run("config", "show", work=work).stdout)["permissions"]["approval"] == (
        "never"
    )
    project = run(
        "config", "set", "permissions.sandbox", "read-only", "--scope", "project", work=work
    )
    assert project.returncode == 2 and "ignored in committed project settings" in project.stderr


def test_sessions_listing_and_doctor(server: MockLLMServer, work: Path):
    server.reply(200, text_response("ok"))
    run("-p", "Investigate the flaky test", "-m", "mock/m", work=work)
    listing = run("sessions", work=work).stdout
    assert "Investigate the flaky test" in listing and "mock/m" in listing
    doctor = run("doctor", work=work)
    assert "sandbox" in doctor.stdout and "python" in doctor.stdout
    assert "Traceback" not in doctor.stderr
