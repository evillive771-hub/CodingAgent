from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from aicoding_agent.tools.base import ToolError


@dataclass(frozen=True, slots=True)
class FileLockLease:
    target: str
    owner: str
    lock_path: Path
    waited_seconds: float


class WorkspaceFileLockManager:
    """In-process plus OS advisory locks for Agents that share one workspace."""

    def __init__(self, *, timeout_seconds: float = 10.0, poll_seconds: float = 0.05) -> None:
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("File lock timeout and poll interval must be positive")
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self._local_locks: dict[str, asyncio.Lock] = {}
        self._local_locks_guard = asyncio.Lock()

    @asynccontextmanager
    async def acquire(
        self,
        workspace: Path,
        target: Path,
        *,
        owner: str = "agent",
    ) -> AsyncIterator[FileLockLease]:
        workspace = workspace.resolve()
        target = target.resolve()
        owner = owner.strip()
        if not owner or len(owner) > 256 or any(char in owner for char in "\r\n\0"):
            raise ToolError("File lock owner must be a bounded single-line identity")
        try:
            relative = target.relative_to(workspace)
        except ValueError as exc:
            raise ToolError("File lock target resolves outside the workspace") from exc

        lock_identity = relative.as_posix()
        if sys.platform == "win32":
            lock_identity = lock_identity.casefold()
        digest = hashlib.sha256(lock_identity.encode("utf-8")).hexdigest()
        lock_path = workspace / ".aicoding-agent" / "locks" / f"{digest}.lock"
        metadata_path = lock_path.with_suffix(".json")
        local_lock = await self._local_lock(lock_path)
        started_at = time.monotonic()
        try:
            await asyncio.wait_for(local_lock.acquire(), timeout=self.timeout_seconds)
        except TimeoutError as exc:
            holder = await asyncio.to_thread(self._read_holder, metadata_path)
            raise self._timeout_error(relative, holder) from exc

        handle: BinaryIO | None = None
        acquired = False
        deadline = started_at + self.timeout_seconds
        try:
            handle = await asyncio.to_thread(self._open_lock_file, lock_path)
            while not acquired:
                acquired = await asyncio.to_thread(self._try_lock, handle)
                if acquired:
                    break
                if time.monotonic() >= deadline:
                    holder = await asyncio.to_thread(self._read_holder, metadata_path)
                    raise self._timeout_error(relative, holder)
                await asyncio.sleep(self.poll_seconds)
            acquired_at = datetime.now(UTC)
            await asyncio.to_thread(
                self._write_metadata,
                metadata_path,
                {
                    "owner": owner,
                    "pid": os.getpid(),
                    "target": relative.as_posix(),
                    "acquired_at": acquired_at.isoformat(),
                },
            )
            yield FileLockLease(
                target=relative.as_posix(),
                owner=owner,
                lock_path=lock_path,
                waited_seconds=max(0.0, time.monotonic() - started_at),
            )
        finally:
            try:
                if handle is not None:
                    release_task = asyncio.create_task(
                        asyncio.to_thread(
                            self._release,
                            handle,
                            acquired=acquired,
                            metadata_path=metadata_path,
                        )
                    )
                    try:
                        await asyncio.shield(release_task)
                    except asyncio.CancelledError:
                        await release_task
                        raise
            finally:
                local_lock.release()

    async def _local_lock(self, lock_path: Path) -> asyncio.Lock:
        key = str(lock_path)
        async with self._local_locks_guard:
            return self._local_locks.setdefault(key, asyncio.Lock())

    @staticmethod
    def _timeout_error(relative: Path, holder: str | None) -> ToolError:
        suffix = f" (current owner: {holder})" if holder else ""
        return ToolError(
            f"Timed out waiting for another Agent to release {relative.as_posix()}{suffix}"
        )

    @staticmethod
    def _open_lock_file(path: Path) -> BinaryIO:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        return handle

    @staticmethod
    def _write_metadata(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read_holder(path: Path) -> str | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        owner = payload.get("owner") if isinstance(payload, dict) else None
        if not isinstance(owner, str) or not owner or len(owner) > 256:
            return None
        return owner.replace("\r", " ").replace("\n", " ")

    @staticmethod
    def _try_lock(handle: BinaryIO) -> bool:
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return False
            return True

        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    @staticmethod
    def _unlock(handle: BinaryIO) -> None:
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @classmethod
    def _release(
        cls,
        handle: BinaryIO,
        *,
        acquired: bool,
        metadata_path: Path,
    ) -> None:
        try:
            if acquired:
                metadata_path.unlink(missing_ok=True)
                cls._unlock(handle)
        finally:
            handle.close()
