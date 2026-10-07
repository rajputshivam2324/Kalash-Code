"""Offline POSIX PTY test: Unicode, resize, scrollback and actual approval keys."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import gzip
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def child(control: Path) -> None:
    from kalash.permissions.prompt import ApprovalResponse, PromptContext
    from kalash.tui.app import KalashApp
    from kalash.tui.approval import ApprovalScreen
    from kalash.tui.messages import AssistantMessage, SystemMessage

    class TerminalApp(KalashApp):
        def on_mount(self) -> None:
            super().on_mount()
            self.run_worker(self.exercise(), exit_on_error=True)

        async def exercise(self) -> None:
            for n in range(240):
                await self._post(SystemMessage(f"PTY entry-{n:03d}: हिंदी 🙂"))
            message = AssistantMessage()
            await self._post(message)
            for n in range(90):
                message.append(f"stream-{n:03d}: **Unicode** हिंदी 🙂\n")
                await asyncio.sleep(0.002)
            decisions = []
            for phase, expected in (
                ("allow", ApprovalResponse.ALLOW_ONCE),
                ("deny", ApprovalResponse.DENY),
            ):
                response = asyncio.get_running_loop().create_future()
                context = PromptContext(
                    action="Synthetic PTY approval",
                    tool_name="write",
                    command="fixture",
                    paths=["fixture.txt"],
                    capabilities=["fs.write"],
                    risk_class="write",
                    reversibility="reversible",
                )
                await self.push_screen(ApprovalScreen(context), response.set_result)
                control.write_text(json.dumps({"phase": phase}))
                message.append("background stream behind modal\n")
                result = await asyncio.wait_for(response, 10)
                assert result.response == expected
                decisions.append(result.response.value)
            message.finish()
            await asyncio.sleep(0.2)
            transcript = self._transcript()
            exported = self._transcript_text()
            assert len(transcript.children) <= transcript.MAX_WIDGETS
            assert exported.count("PTY entry-") == 240
            assert "stream-089" in exported
            control.write_text(
                json.dumps(
                    {
                        "phase": "done",
                        "passed": True,
                        "stdin_is_tty": sys.stdin.isatty(),
                        "entries": 240,
                        "stream_lines": 90,
                        "resident_widgets": len(transcript.children),
                        "decisions": decisions,
                        "archive_complete": not transcript.archive_error,
                    }
                )
            )
            self.exit()

    TerminalApp().run()


def run(output: Path) -> None:
    import fcntl
    import pty
    import select
    import struct

    output.mkdir(parents=True, exist_ok=False)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, __import__("termios").TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    control = output / "control.json"
    captured = bytearray()
    phases = set()
    with tempfile.TemporaryDirectory(prefix="kalash-pty-home-") as home:
        from kalash.sandbox.environment import safe_environment

        env = {**safe_environment(), "KALASH_HOME": home, "TERM": "xterm-256color"}
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--child", str(control)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
        )
        os.close(slave)
        deadline = time.monotonic() + 30
        try:
            while process.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    with contextlib.suppress(OSError):
                        chunk = os.read(master, 65536)
                        captured.extend(chunk[: max(0, 2_000_000 - len(captured))])
                if control.exists():
                    with contextlib.suppress(json.JSONDecodeError):
                        phase = json.loads(control.read_text())["phase"]
                        if phase in {"allow", "deny"} and phase not in phases:
                            phases.add(phase)
                            fcntl.ioctl(
                                master,
                                __import__("termios").TIOCSWINSZ,
                                struct.pack("HHHH", 18, 52, 0, 0),
                            )
                            os.kill(process.pid, signal.SIGWINCH)
                            os.write(master, b"y" if phase == "allow" else b"\x1b")
            assert process.wait(timeout=2) == 0, captured[-5000:].decode(errors="replace")
            result = json.loads(control.read_text())
            assert result.get("passed")
            assert result["stdin_is_tty"]
            result["captured_bytes"] = len(captured)
            result["scope"] = "Local Linux PTY; no physical-terminal or SSH certification"
            (output / "result.json").write_text(json.dumps(result, indent=2))
            with gzip.open(output / "terminal.ansi.gz", "wb") as trace:
                trace.write(captured)
            sys.stdout.write(json.dumps(result) + "\n")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", type=Path)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    if options.child:
        child(options.child)
    elif options.output:
        run(options.output)
    else:
        parser.error("--output is required")
