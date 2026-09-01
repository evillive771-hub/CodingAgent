from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class BackgroundStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"


class BackgroundJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=32, max_length=32)
    program: str
    args: list[str] = Field(default_factory=list)
    cwd: str
    status: BackgroundStatus
    timeout_seconds: float
    verification_id: str | None = None
    exit_code: int | None = None
    output: str = ""
    notification_delivered: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BackgroundJobStore:
    def __init__(self, root: Path) -> None:
        self.path = root.resolve() / "jobs.jsonl"
        self._lock = asyncio.Lock()

    async def load_latest(self) -> dict[str, BackgroundJob]:
        async with self._lock:
            return await asyncio.to_thread(self._load_latest)

    async def append(self, job: BackgroundJob) -> None:
        async with self._lock:
            await asyncio.to_thread(self._append, job.model_dump(mode="json"))

    def _load_latest(self) -> dict[str, BackgroundJob]:
        if not self.path.is_file():
            return {}
        jobs: dict[str, BackgroundJob] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                job = BackgroundJob.model_validate_json(line)
                jobs[job.job_id] = job
        return jobs

    def _append(self, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


class BackgroundTaskManager:
    """Run prevalidated commands asynchronously and persist their observable lifecycle."""

    def __init__(
        self,
        *,
        store: BackgroundJobStore | None = None,
        max_output_chars: int = 30000,
    ) -> None:
        self.store = store
        self.max_output_chars = max_output_chars
        self.jobs: dict[str, BackgroundJob] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._initialized = False
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()

    async def initialize(self) -> None:
        async with self._lock:
            if self._initialized:
                return
            self.jobs = await self.store.load_latest() if self.store else {}
            for job_id, job in list(self.jobs.items()):
                if job.status == BackgroundStatus.RUNNING:
                    interrupted = job.model_copy(
                        update={
                            "status": BackgroundStatus.INTERRUPTED,
                            "output": "Host restarted before the background process completed.",
                            "updated_at": datetime.now(UTC),
                        }
                    )
                    self.jobs[job_id] = interrupted
                    if self.store:
                        await self.store.append(interrupted)
            self._initialized = True

    async def start(
        self,
        *,
        program: str,
        args: list[str],
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
        verification_id: str | None,
    ) -> BackgroundJob:
        await self.initialize()
        job = BackgroundJob(
            job_id=uuid4().hex,
            program=program,
            args=args,
            cwd=str(cwd.resolve()),
            status=BackgroundStatus.RUNNING,
            timeout_seconds=timeout_seconds,
            verification_id=verification_id,
        )
        async with self._lock:
            self.jobs[job.job_id] = job
            if self.store:
                await self.store.append(job)
            self._tasks[job.job_id] = asyncio.create_task(self._run(job, env))
        return job

    async def _run(self, job: BackgroundJob, env: dict[str, str]) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                job.program,
                *job.args,
                cwd=job.cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=job.timeout_seconds
                )
                status = (
                    BackgroundStatus.COMPLETED
                    if process.returncode == 0
                    else BackgroundStatus.FAILED
                )
                exit_code = process.returncode
            except TimeoutError:
                process.kill()
                stdout, stderr = await process.communicate()
                status = BackgroundStatus.TIMED_OUT
                exit_code = None
            output = (
                "\n".join(
                    part.decode("utf-8", errors="replace") for part in (stdout, stderr) if part
                ).strip()
                or "<no output>"
            )
        except Exception as exc:
            status = BackgroundStatus.FAILED
            exit_code = None
            output = f"Background process failed internally: {type(exc).__name__}"
        if len(output) > self.max_output_chars:
            half = self.max_output_chars // 2
            output = f"{output[:half]}\n... <output bounded> ...\n{output[-half:]}"
        async with self._lock:
            completed = job.model_copy(
                update={
                    "status": status,
                    "exit_code": exit_code,
                    "output": output,
                    "updated_at": datetime.now(UTC),
                }
            )
            self.jobs[job.job_id] = completed
            if self.store:
                await self.store.append(completed)
            self._changed.set()

    async def collect_completed(self) -> list[BackgroundJob]:
        await self.initialize()
        async with self._lock:
            ready = [
                job
                for job in self.jobs.values()
                if job.status != BackgroundStatus.RUNNING and not job.notification_delivered
            ]
            delivered: list[BackgroundJob] = []
            for job in ready:
                updated = job.model_copy(
                    update={"notification_delivered": True, "updated_at": datetime.now(UTC)}
                )
                self.jobs[job.job_id] = updated
                if self.store:
                    await self.store.append(updated)
                delivered.append(updated)
            if not any(
                job.status != BackgroundStatus.RUNNING and not job.notification_delivered
                for job in self.jobs.values()
            ):
                self._changed.clear()
            return delivered

    async def wait_for_change(self, timeout_seconds: float = 5.0) -> None:
        try:
            await asyncio.wait_for(self._changed.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return

    @property
    def has_running(self) -> bool:
        return any(job.status == BackgroundStatus.RUNNING for job in self.jobs.values())

    def render(self) -> str:
        if not self.jobs:
            return "<no background tasks>"
        return "\n".join(
            f"{job.job_id}: {job.status} {job.program} {' '.join(job.args)}"
            for job in sorted(self.jobs.values(), key=lambda item: item.created_at)
        )
