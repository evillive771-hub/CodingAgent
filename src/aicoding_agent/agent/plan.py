from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aicoding_agent.persistence.base import validate_run_id


class PlanStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class PlanState(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class PlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=2000)
    status: PlanStatus = PlanStatus.PENDING
    evidence: list[str] = Field(default_factory=list, max_length=20)
    source: str = Field(default="agent", min_length=1, max_length=64)


class PlanSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    state: PlanState = PlanState.ACTIVE
    direction: str = Field(default="", max_length=20000)
    items: list[PlanItem] = Field(default_factory=list, max_length=20)
    supersedes_version: int | None = Field(default=None, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def only_one_item_can_be_in_progress(self) -> PlanSnapshot:
        active = sum(item.status == PlanStatus.IN_PROGRESS for item in self.items)
        if active > 1:
            raise ValueError("Only one plan item can be in_progress")
        if self.state == PlanState.ACTIVE and not self.items:
            raise ValueError("An active plan requires at least one item")
        return self


class PlanStore:
    """Append-only, version-checked plan snapshots for one logical session."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._locks: dict[str, asyncio.Lock] = {}

    async def load(self, scope_id: str) -> list[PlanSnapshot]:
        path = self._path(scope_id)
        lock = self._locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(self._load, path)

    async def append(self, scope_id: str, snapshot: PlanSnapshot) -> Path:
        path = self._path(scope_id)
        lock = self._locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            existing = await asyncio.to_thread(self._load, path)
            expected = len(existing) + 1
            if snapshot.version != expected:
                raise ValueError(f"Expected plan version {expected}, received {snapshot.version}")
            await asyncio.to_thread(self._append, path, snapshot.model_dump(mode="json"))
            return path

    def _path(self, scope_id: str) -> Path:
        return self.root / validate_run_id(scope_id) / "plans.jsonl"

    @staticmethod
    def _load(path: Path) -> list[PlanSnapshot]:
        if not path.is_file():
            return []
        snapshots: list[PlanSnapshot] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            snapshot = PlanSnapshot.model_validate_json(line)
            if snapshot.version != len(snapshots) + 1:
                raise ValueError(f"Invalid plan sequence at line {line_number}")
            snapshots.append(snapshot)
        return snapshots

    @staticmethod
    def _append(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


class PlanManager:
    def __init__(self, *, store: PlanStore | None = None, scope_id: str = "default") -> None:
        self.store = store
        self.scope_id = validate_run_id(scope_id)
        self.history: list[PlanSnapshot] = []
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            if not self._initialized:
                self.history = await self.store.load(self.scope_id) if self.store else []
                self._initialized = True

    @property
    def current(self) -> PlanSnapshot | None:
        return self.history[-1] if self.history else None

    async def replace(
        self,
        items: list[PlanItem],
        *,
        direction: str = "",
    ) -> PlanSnapshot:
        await self.initialize()
        async with self._lock:
            previous = self.current
            snapshot = PlanSnapshot(
                version=len(self.history) + 1,
                state=PlanState.ACTIVE,
                direction=direction.strip(),
                items=items,
                supersedes_version=previous.version if previous else None,
            )
            if self.store:
                await self.store.append(self.scope_id, snapshot)
            self.history.append(snapshot)
            return snapshot

    async def supersede(self, direction: str) -> PlanSnapshot | None:
        await self.initialize()
        async with self._lock:
            previous = self.current
            if previous is None:
                return None
            snapshot = PlanSnapshot(
                version=len(self.history) + 1,
                state=PlanState.SUPERSEDED,
                direction=direction.strip(),
                items=previous.items,
                supersedes_version=previous.version,
            )
            if self.store:
                await self.store.append(self.scope_id, snapshot)
            self.history.append(snapshot)
            return snapshot

    def render(self) -> str:
        snapshot = self.current
        if snapshot is None:
            return "<no active plan>"
        symbols = {
            PlanStatus.PENDING: "[ ]",
            PlanStatus.IN_PROGRESS: "[>]",
            PlanStatus.COMPLETED: "[x]",
        }
        lines = [f"Plan v{snapshot.version} ({snapshot.state})"]
        if snapshot.direction:
            lines.append(f"Direction: {snapshot.direction}")
        lines.extend(f"{symbols[item.status]} {item.content}" for item in snapshot.items)
        return "\n".join(lines)
