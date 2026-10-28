"""Background processes. Every bash command runs as a job so a slow one can outlive its call.

Output goes straight to a file (stdout and stderr interleaved), so completion is the
process exiting - never waiting for pipe EOF, which a backgrounded grandchild would hold
open forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Job:
    id: str
    command: str
    process: asyncio.subprocess.Process
    output_path: Path
    started: float = field(default_factory=time.monotonic)
    reported_offset: int = 0
    killed: bool = False

    @property
    def running(self) -> bool:
        return self.process.returncode is None

    @property
    def exit_code(self) -> int | None:
        return self.process.returncode


class JobManager:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._jobs: dict[str, Job] = {}
        self._counter = 0

    async def start(self, argv: list[str], command: str, cwd: Path, env: dict[str, str]) -> Job:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._counter += 1
        job_id = f"job{self._counter}"
        path = self.directory / f"{job_id}.log"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=fd,
                stderr=fd,
                start_new_session=True,
            )
        finally:
            os.close(fd)
        job = Job(job_id, command, process, path)
        self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        return list(self._jobs.values())

    def read_all(self, job: Job) -> str:
        try:
            return job.output_path.read_bytes().decode("utf-8", "replace")
        except OSError:
            return ""

    def read_new(self, job: Job) -> str:
        raw = b""
        with contextlib.suppress(OSError), job.output_path.open("rb") as stream:
            stream.seek(job.reported_offset)
            raw = stream.read()
        job.reported_offset += len(raw)
        return raw.decode("utf-8", "replace")

    async def wait(self, job: Job, timeout: float | None) -> bool:
        """True when the process finished within ``timeout``."""
        try:
            await asyncio.wait_for(asyncio.shield(job.process.wait()), timeout)
        except TimeoutError:
            return False
        return True

    def kill(self, job: Job) -> None:
        job.killed = True
        if job.process.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(job.process.pid, signal.SIGTERM)

    async def kill_hard(self, job: Job) -> None:
        self.kill(job)
        if not await self.wait(job, 2.0):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(job.process.pid, signal.SIGKILL)
            await self.wait(job, 2.0)

    async def shutdown(self) -> None:
        for job in self._jobs.values():
            if job.running:
                await self.kill_hard(job)
