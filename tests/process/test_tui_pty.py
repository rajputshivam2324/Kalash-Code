"""The real ``kalash`` command in a real pseudo-terminal.

Pilot tests drive Textual in-process with a fake driver. This tier starts the actual program on a
pty - alternate screen, keyboard decoding, the Linux driver, process exit - and checks what a
person would see. It is what catches "works in the test harness, crashes in my terminal".
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from tests.harness.mock_server import MockLLMServer, text_response

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pty")

ANSI = re.compile(
    r"\x1b\[[0-9;?<>=!]*[ -/]*[@-~]|\x1b[()][A-Z0-9]|\x1b[=>78]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)"
)


class Terminal:
    """Spawns ``python -m kalash`` on a pty and reads its screen output."""

    def __init__(self, args: list[str], cwd: Path, env: dict[str, str]) -> None:
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "kalash", *args],
            cwd=cwd,
            env={**env, "TERM": "xterm-256color", "COLUMNS": "120", "LINES": "40"},
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave)
        self.raw = b""

    def read(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if not ready:
                continue
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return
            if not chunk:
                return
            self.raw += chunk
            self._answer_queries(chunk)

    def _answer_queries(self, chunk: bytes) -> None:
        """Reply to the terminal-capability queries Textual sends, like a real terminal would."""
        if b"\x1b[c" in chunk or b"\x1b[0c" in chunk:  # primary device attributes
            os.write(self.master, b"\x1b[?62;c")
        if b"\x1b[6n" in chunk:  # cursor position report
            os.write(self.master, b"\x1b[1;1R")

    @property
    def text(self) -> str:
        return ANSI.sub("", self.raw.decode("utf-8", "replace"))

    def wait_for(self, needle: str, timeout: float = 15.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.read(0.2)
            if needle in self.text:
                return
            if self.proc.poll() is not None:
                break
        raise AssertionError(f"never saw {needle!r}; screen text was:\n{self.text[-3000:]}")

    def send(self, data: str) -> None:
        os.write(self.master, data.encode())

    def close(self) -> int | None:
        code = self.proc.poll()
        if code is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
            code = self.proc.wait(timeout=5)
        os.close(self.master)
        return code

    def wait_exit(self, timeout: float = 10.0) -> int:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.read(0.1)
            code = self.proc.poll()
            if code is not None:
                return code
        raise AssertionError(f"kalash did not exit; screen text was:\n{self.text[-2000:]}")


def clean_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    for key in list(env):
        if key.endswith("_API_KEY"):
            del env[key]
    env.update(KALASH_HOME=str(home), HOME=str(home.parent), NO_COLOR="")
    return env


def test_first_run_shows_onboarding_and_ctrl_d_exits_cleanly(tmp_path: Path):
    work = tmp_path / "proj"
    work.mkdir()
    term = Terminal(["--cwd", str(work)], work, clean_env(tmp_path / "home"))
    try:
        term.wait_for("Add an API key")
        assert "/provider" in term.text and "Kalash" in term.text
        term.send("\x04")  # Ctrl+D
        assert term.wait_exit() == 0
    finally:
        term.close()


def test_a_message_round_trips_through_the_real_terminal(tmp_path: Path):
    server = MockLLMServer().start()
    try:
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        (home / "settings.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "mock": {
                            "kind": "openai",
                            "base_url": server.base_url,
                            "models": ["m"],
                            "requires_key": False,
                        }
                    }
                }
            )
        )
        server.reply(200, text_response("The answer is forty-two."))
        work = tmp_path / "proj"
        work.mkdir()
        term = Terminal(["--cwd", str(work), "-m", "mock/m"], work, clean_env(home))
        try:
            term.wait_for("mock/m")
            term.send("what is the answer?")
            term.read(0.3)
            term.send("\r")
            term.wait_for("forty-two")
            assert "what is the answer?" in term.text
            term.send("\x04")
            assert term.wait_exit() == 0
        finally:
            term.close()
        assert server.unexpected == 0 and len(server.requests) == 1
    finally:
        server.stop()


@pytest.mark.parametrize("pointer_y", [5, 38], ids=["conversation", "empty-prompt"])
def test_trackpad_bursts_keep_working_in_the_real_terminal(tmp_path: Path, pointer_y: int):
    server = MockLLMServer().start()
    try:
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        (home / "settings.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "mock": {
                            "kind": "openai",
                            "base_url": server.base_url,
                            "models": ["m"],
                            "requires_key": False,
                        }
                    }
                }
            )
        )
        server.reply(200, text_response("\n\n".join(f"SCROLL-ROW-{i:03d}" for i in range(120))))
        work = tmp_path / "proj"
        work.mkdir()
        term = Terminal(["--cwd", str(work), "-m", "mock/m"], work, clean_env(home))
        try:
            term.wait_for("mock/m")
            term.send("long answer please\r")
            term.wait_for("SCROLL-ROW-119")
            term.read(0.3)
            assert b"\x1b[?1006h" in term.raw, "SGR mouse reporting must be enabled"
            for _ in range(2):
                # Real terminal bytes, rather than isolated Pilot wheel events:
                # momentum, a reversal, horizontal noise, then another reversal.
                codes = [64] * 24 + [65] * 24 + [66, 67] + [64] * 24
                term.send("".join(f"\x1b[<{code};10;{pointer_y}M" for code in codes))
                term.read(0.5)
                term.raw = b""
                # A resize gives a fresh complete screen, excluding older frames
                # painted during the gesture. Judge the visible conversation.
                fcntl.ioctl(term.master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 121, 0, 0))
                term.proc.send_signal(signal.SIGWINCH)
                term.read(0.5)
                rows = [int(i) for i in re.findall(r"SCROLL-ROW-(\d{3})", term.text)]
                assert rows and max(rows) < 119, term.text[-3000:]
                term.send(f"\x1b[<65;10;{pointer_y}M" * 120)
                term.wait_for("SCROLL-ROW-119")
                fcntl.ioctl(term.master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
                term.proc.send_signal(signal.SIGWINCH)
                term.read(0.3)
            term.send("\x04")
            assert term.wait_exit() == 0
        finally:
            term.close()
        assert server.unexpected == 0 and len(server.requests) == 1
    finally:
        server.stop()


def test_a_stale_textual_gets_a_message_not_a_traceback(tmp_path: Path):
    """Simulates the machine that pulled new code but kept an old Textual."""
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(
        "from importlib import metadata\n"
        "_v = metadata.version\n"
        "metadata.version = lambda n: '0.89.1' if n == 'textual' else _v(n)\n"
    )
    env = {**clean_env(tmp_path / "home"), "PYTHONPATH": str(shim)}
    work = tmp_path / "proj"
    work.mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "kalash", "-c", "--cwd", str(work)],
        env=env,
        cwd=work,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "textual>=8.2,<9 is required, found 0.89.1" in result.stderr
    assert "pip install --user -U" in result.stderr
