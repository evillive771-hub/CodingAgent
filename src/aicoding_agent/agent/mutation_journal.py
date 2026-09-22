from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import Field

from aicoding_agent.domain.models import StrictModel
from aicoding_agent.persistence.base import validate_run_id
from aicoding_agent.tools.base import ToolError
from aicoding_agent.tools.file_lock import WorkspaceFileLockManager


class MutationRecoveryError(RuntimeError):
    pass


class FileSnapshot(StrictModel):
    exists: bool
    content: str | None = Field(default=None, max_length=1_000_000)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FileMutationRecord(StrictModel):
    mutation_id: str = Field(min_length=32, max_length=32)
    sequence: int = Field(ge=1)
    run_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=1000)
    before: FileSnapshot
    after: FileSnapshot
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class IrreversibleEffectRecord(StrictModel):
    sequence: int = Field(ge=1)
    run_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=128)
    risk: str = Field(min_length=1, max_length=32)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MutationRecoveryResult(StrictModel):
    recovery_id: str
    run_ids: list[str]
    restored_files: list[str]
    removed_files: list[str]
    skipped_records: int = Field(ge=0)
    already_completed: bool = False


class MutationJournal:
    """Append-only reversible snapshots for host file tools; never handles external effects."""

    def __init__(self, root: Path, *, max_file_chars: int = 1_000_000) -> None:
        if not 1_000 <= max_file_chars <= 5_000_000:
            raise ValueError("Mutation journal file limit must be between 1,000 and 5,000,000")
        self.root = root.resolve()
        self.max_file_chars = max_file_chars
        self._locks: dict[str, asyncio.Lock] = {}

    async def capture(self, path: Path) -> FileSnapshot:
        return await asyncio.to_thread(self._capture_sync, path.resolve())

    async def record(
        self,
        *,
        run_id: str,
        tool_name: str,
        workspace: Path,
        path: Path,
        before: FileSnapshot,
    ) -> FileMutationRecord:
        run_id = validate_run_id(run_id)
        workspace = workspace.resolve()
        path = path.resolve()
        try:
            relative = path.relative_to(workspace).as_posix()
        except ValueError as exc:
            raise ToolError("Mutation journal target is outside the workspace") from exc
        after = await self.capture(path)
        if before.sha256 == after.sha256:
            raise ToolError("Mutation tool reported a change but file content did not change")
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            existing = await self.load(run_id, use_lock=False)
            record = FileMutationRecord(
                mutation_id=uuid4().hex,
                sequence=len(existing) + 1,
                run_id=run_id,
                tool_name=tool_name,
                path=relative,
                before=before,
                after=after,
            )
            await asyncio.to_thread(self._append, self._records_path(run_id), record)
            return record

    async def restore_snapshot(self, path: Path, snapshot: FileSnapshot) -> None:
        await asyncio.to_thread(self._restore_sync, path.resolve(), snapshot)

    async def record_irreversible_effect(
        self,
        *,
        run_id: str,
        tool_name: str,
        risk: str,
    ) -> IrreversibleEffectRecord:
        run_id = validate_run_id(run_id)
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            existing = await asyncio.to_thread(
                self._load_effects,
                self._effects_path(run_id),
            )
            record = IrreversibleEffectRecord(
                sequence=len(existing) + 1,
                run_id=run_id,
                tool_name=tool_name,
                risk=risk,
            )
            await asyncio.to_thread(self._append, self._effects_path(run_id), record)
            return record

    async def load_irreversible_effects(
        self,
        run_id: str,
    ) -> list[IrreversibleEffectRecord]:
        run_id = validate_run_id(run_id)
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(
                self._load_effects,
                self._effects_path(run_id),
            )

    async def load(
        self,
        run_id: str,
        *,
        use_lock: bool = True,
    ) -> list[FileMutationRecord]:
        run_id = validate_run_id(run_id)
        if not use_lock:
            return await asyncio.to_thread(self._load, self._records_path(run_id))
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(self._load, self._records_path(run_id))

    async def recover_runs(
        self,
        *,
        workspace: Path,
        run_ids: list[str],
        file_locks: WorkspaceFileLockManager | None = None,
        owner: str = "recovery",
    ) -> MutationRecoveryResult:
        workspace = workspace.resolve()
        validated_run_ids = [validate_run_id(run_id) for run_id in run_ids]
        if len(set(validated_run_ids)) != len(validated_run_ids):
            raise MutationRecoveryError("Recovery run IDs must be unique and chronological")
        records: list[FileMutationRecord] = []
        for run_id in validated_run_ids:
            records.extend(await self.load(run_id))
        effects: list[IrreversibleEffectRecord] = []
        for run_id in validated_run_ids:
            effects.extend(await self.load_irreversible_effects(run_id))
        if effects:
            names = ", ".join(sorted({effect.tool_name for effect in effects}))
            raise MutationRecoveryError(
                "Recovery refused because selected turns started non-reversible "
                f"process or external tools: {names}"
            )
        recovery_id = _recovery_id(validated_run_ids, records)
        marker = self.root / "recoveries" / f"{recovery_id}.json"
        if not records:
            if marker.is_file():
                return MutationRecoveryResult(
                    recovery_id=recovery_id,
                    run_ids=validated_run_ids,
                    restored_files=[],
                    removed_files=[],
                    skipped_records=0,
                    already_completed=True,
                )
            result = MutationRecoveryResult(
                recovery_id=recovery_id,
                run_ids=validated_run_ids,
                restored_files=[],
                removed_files=[],
                skipped_records=0,
            )
            await asyncio.to_thread(self._write_marker, marker, result)
            return result
        _validate_record_chains(records)
        paths = sorted({record.path for record in records})
        manager = file_locks or WorkspaceFileLockManager()
        restored: set[str] = set()
        removed: set[str] = set()
        skipped = 0
        async with AsyncExitStack() as stack:
            for relative in paths:
                await stack.enter_async_context(
                    manager.acquire(
                        workspace,
                        workspace / relative,
                        owner=owner,
                    )
                )
            if marker.is_file():
                _validate_recovered_boundaries(workspace, records, self._capture_sync)
                return MutationRecoveryResult(
                    recovery_id=recovery_id,
                    run_ids=validated_run_ids,
                    restored_files=[],
                    removed_files=[],
                    skipped_records=len(records),
                    already_completed=True,
                )
            _validate_current_boundaries(workspace, records, self._capture_sync)
            for record in reversed(records):
                path = workspace / record.path
                current = await self.capture(path)
                if current.sha256 == record.before.sha256:
                    skipped += 1
                    continue
                if current.sha256 != record.after.sha256:
                    raise MutationRecoveryError(
                        f"Recovery conflict at {record.path}; current content is not journaled"
                    )
                await self.restore_snapshot(path, record.before)
                if record.before.exists:
                    restored.add(record.path)
                else:
                    removed.add(record.path)
        result = MutationRecoveryResult(
            recovery_id=recovery_id,
            run_ids=validated_run_ids,
            restored_files=sorted(restored),
            removed_files=sorted(removed),
            skipped_records=skipped,
        )
        await asyncio.to_thread(self._write_marker, marker, result)
        return result

    def _capture_sync(self, path: Path) -> FileSnapshot:
        if not path.exists():
            return FileSnapshot(exists=False, sha256=_snapshot_hash(False, None))
        if not path.is_file():
            raise ToolError(f"Mutation target is not a file: {path.name}")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError("Mutation journal only supports UTF-8 text files") from exc
        except OSError as exc:
            raise ToolError(f"Could not snapshot mutation target: {path.name}") from exc
        if len(content) > self.max_file_chars:
            raise ToolError(
                f"Mutation target exceeds journal limit of {self.max_file_chars} characters"
            )
        return FileSnapshot(
            exists=True,
            content=content,
            sha256=_snapshot_hash(True, content),
        )

    def _records_path(self, run_id: str) -> Path:
        return self.root / "runs" / run_id / "mutations.jsonl"

    def _effects_path(self, run_id: str) -> Path:
        return self.root / "runs" / run_id / "effects.jsonl"

    @staticmethod
    def _append(
        path: Path,
        record: FileMutationRecord | IrreversibleEffectRecord,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(record.model_dump_json())
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _load(path: Path) -> list[FileMutationRecord]:
        if not path.is_file():
            return []
        records: list[FileMutationRecord] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            record = FileMutationRecord.model_validate_json(line)
            if record.sequence != len(records) + 1:
                raise MutationRecoveryError(
                    f"Invalid mutation journal sequence at line {line_number}"
                )
            records.append(record)
        return records

    @staticmethod
    def _load_effects(path: Path) -> list[IrreversibleEffectRecord]:
        if not path.is_file():
            return []
        records: list[IrreversibleEffectRecord] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            record = IrreversibleEffectRecord.model_validate_json(line)
            if record.sequence != len(records) + 1:
                raise MutationRecoveryError(
                    f"Invalid irreversible-effect sequence at line {line_number}"
                )
            records.append(record)
        return records

    @staticmethod
    def _restore_sync(path: Path, snapshot: FileSnapshot) -> None:
        if not snapshot.exists:
            path.unlink(missing_ok=True)
            return
        assert snapshot.content is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.recovery.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="") as handle:
                handle.write(snapshot.content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _write_marker(path: Path, result: MutationRecoveryResult) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(result.model_dump_json(indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)


def _snapshot_hash(exists: bool, content: str | None) -> str:
    marker = "file\0" if exists else "missing\0"
    return hashlib.sha256((marker + (content or "")).encode("utf-8")).hexdigest()


def _recovery_id(run_ids: list[str], records: list[FileMutationRecord]) -> str:
    payload = {
        "run_ids": run_ids,
        "mutations": [record.mutation_id for record in records],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_record_chains(records: list[FileMutationRecord]) -> None:
    latest: dict[str, FileMutationRecord] = {}
    for record in records:
        previous = latest.get(record.path)
        if previous is not None and previous.after.sha256 != record.before.sha256:
            raise MutationRecoveryError(
                f"Mutation journal chain is discontinuous for {record.path}"
            )
        latest[record.path] = record


def _validate_current_boundaries(
    workspace: Path,
    records: list[FileMutationRecord],
    capture: Callable[[Path], FileSnapshot],
) -> None:
    by_path: dict[str, set[str]] = {}
    for record in records:
        by_path.setdefault(record.path, set()).update({record.before.sha256, record.after.sha256})
    for relative, allowed_hashes in by_path.items():
        current = capture((workspace / relative).resolve())
        if current.sha256 not in allowed_hashes:
            raise MutationRecoveryError(
                f"Recovery conflict at {relative}; current content is not a journal boundary"
            )


def _validate_recovered_boundaries(
    workspace: Path,
    records: list[FileMutationRecord],
    capture: Callable[[Path], FileSnapshot],
) -> None:
    original_by_path: dict[str, FileSnapshot] = {}
    for record in records:
        original_by_path.setdefault(record.path, record.before)
    for relative, expected in original_by_path.items():
        current = capture((workspace / relative).resolve())
        if current.sha256 != expected.sha256:
            raise MutationRecoveryError(
                f"Recovery was already applied but {relative} changed afterward"
            )
