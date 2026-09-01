from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from aicoding_agent.domain.events import EventRecord
from aicoding_agent.persistence.base import validate_run_id


class JsonlEventSink:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._locks: dict[str, asyncio.Lock] = {}
        self._sequences: dict[str, int] = {}

    async def emit(self, run_id: str, event_type: str, data: dict[str, Any]) -> None:
        validate_run_id(run_id)
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            path = self.root / run_id / "events.jsonl"
            if run_id not in self._sequences:
                self._sequences[run_id] = await asyncio.to_thread(self._last_sequence, path)
            sequence = self._sequences.get(run_id, 0) + 1
            self._sequences[run_id] = sequence
            record = EventRecord(
                run_id=run_id,
                event_type=event_type,
                data=data,
                sequence=sequence,
            )
            await asyncio.to_thread(self._append, path, record.safe_dict())

    @staticmethod
    def _last_sequence(path: Path) -> int:
        if not path.is_file():
            return 0
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines):
                if not line.strip():
                    continue
                payload = json.loads(line)
                sequence = payload.get("sequence")
                return int(sequence) if sequence is not None else 0
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0
        return 0

    @staticmethod
    def _append(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


class AtomicCheckpointStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    async def save(self, run_id: str, payload: dict[str, Any]) -> Path:
        validate_run_id(run_id)
        target = self.root / run_id / "checkpoint.json"
        await asyncio.to_thread(self._save, target, payload)
        return target

    async def load(self, run_id: str) -> dict[str, Any] | None:
        validate_run_id(run_id)
        target = self.root / run_id / "checkpoint.json"
        if not target.is_file():
            return None
        return await asyncio.to_thread(self._load, target)

    @staticmethod
    def _save(target: Path, payload: dict[str, Any]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        serialized = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)

    @staticmethod
    def _load(target: Path) -> dict[str, Any]:
        value = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Checkpoint root must be a JSON object")
        return value
