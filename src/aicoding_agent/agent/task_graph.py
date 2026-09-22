from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import Field, model_validator

from aicoding_agent.domain.models import StrictModel
from aicoding_agent.persistence.base import validate_run_id
from aicoding_agent.tools.base import ToolError
from aicoding_agent.tools.file_lock import WorkspaceFileLockManager


class TaskGraphStoreError(RuntimeError):
    pass


class TaskGraphConflictError(TaskGraphStoreError):
    pass


class TaskNodeStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"


class TaskGraphState(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class TaskNode(StrictModel):
    task_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=4000)
    status: TaskNodeStatus = TaskNodeStatus.PENDING
    blocked_by: list[str] = Field(default_factory=list, max_length=50)
    blocking_reason: str = Field(default="", max_length=2000)
    owner: str = Field(default="agent", min_length=1, max_length=128)
    preferred_owner: str | None = Field(default=None, min_length=1, max_length=128)
    evidence: list[str] = Field(default_factory=list, max_length=50)
    worktree: str | None = Field(default=None, max_length=500)
    attempts: int = Field(default=0, ge=0, le=1000)

    @model_validator(mode="after")
    def status_has_required_state(self) -> TaskNode:
        if self.status == TaskNodeStatus.BLOCKED and not self.blocking_reason.strip():
            raise ValueError("A blocked task requires blocking_reason")
        if self.status != TaskNodeStatus.BLOCKED and self.blocking_reason:
            raise ValueError("Only blocked tasks may set blocking_reason")
        if self.status == TaskNodeStatus.COMPLETED and not self.evidence:
            raise ValueError("A completed task requires evidence")
        if self.status in {TaskNodeStatus.IN_PROGRESS, TaskNodeStatus.COMPLETED}:
            if self.attempts < 1:
                raise ValueError("In-progress and completed tasks require attempts >= 1")
        return self


class TaskGraphSnapshot(StrictModel):
    version: int = Field(ge=1)
    state: TaskGraphState = TaskGraphState.ACTIVE
    direction: str = Field(default="", max_length=20_000)
    tasks: list[TaskNode] = Field(min_length=1, max_length=100)
    supersedes_version: int | None = Field(default=None, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_graph(self) -> TaskGraphSnapshot:
        by_id = {task.task_id: task for task in self.tasks}
        if len(by_id) != len(self.tasks):
            raise ValueError("Task graph IDs must be unique")
        for task in self.tasks:
            if task.task_id in task.blocked_by:
                raise ValueError(f"Task {task.task_id!r} cannot depend on itself")
            missing = [dependency for dependency in task.blocked_by if dependency not in by_id]
            if missing:
                raise ValueError(
                    f"Task {task.task_id!r} has unknown dependencies: {', '.join(missing)}"
                )
        _assert_acyclic(by_id)
        active_by_owner: dict[str, int] = {}
        for task in self.tasks:
            if task.status == TaskNodeStatus.IN_PROGRESS:
                active_by_owner[task.owner] = active_by_owner.get(task.owner, 0) + 1
                if active_by_owner[task.owner] > 1:
                    raise ValueError(
                        f"Owner {task.owner!r} cannot have more than one in-progress task"
                    )
            if task.status in {TaskNodeStatus.IN_PROGRESS, TaskNodeStatus.COMPLETED}:
                incomplete = [
                    dependency
                    for dependency in task.blocked_by
                    if by_id[dependency].status != TaskNodeStatus.COMPLETED
                ]
                if incomplete:
                    raise ValueError(
                        f"Task {task.task_id!r} cannot be {task.status}; "
                        f"dependencies are incomplete: {', '.join(incomplete)}"
                    )
        return self


class TaskGraphStore:
    def __init__(self, root: Path, *, lock_timeout_seconds: float = 10.0) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}
        self._file_locks = WorkspaceFileLockManager(timeout_seconds=lock_timeout_seconds)

    async def load(self, scope_id: str) -> list[TaskGraphSnapshot]:
        path = self._path(scope_id)
        lock = self._locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            try:
                async with self._file_locks.acquire(
                    self.root,
                    path,
                    owner=f"task-graph-reader:{scope_id}",
                ):
                    return await asyncio.to_thread(self._load, path)
            except ToolError as exc:
                raise TaskGraphStoreError(str(exc)) from exc

    async def append(self, scope_id: str, snapshot: TaskGraphSnapshot) -> None:
        path = self._path(scope_id)
        lock = self._locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            try:
                async with self._file_locks.acquire(
                    self.root,
                    path,
                    owner=f"task-graph-writer:{scope_id}",
                ):
                    existing = await asyncio.to_thread(self._load, path)
                    if snapshot.version != len(existing) + 1:
                        raise TaskGraphConflictError(
                            "Task graph changed in another process"
                        )
                    await asyncio.to_thread(
                        self._append,
                        path,
                        snapshot.model_dump(mode="json"),
                    )
            except ToolError as exc:
                raise TaskGraphStoreError(str(exc)) from exc

    def _path(self, scope_id: str) -> Path:
        return self.root / validate_run_id(scope_id) / "task-graph.jsonl"

    @staticmethod
    def _load(path: Path) -> list[TaskGraphSnapshot]:
        if not path.is_file():
            return []
        snapshots: list[TaskGraphSnapshot] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            snapshot = TaskGraphSnapshot.model_validate_json(line)
            if snapshot.version != len(snapshots) + 1:
                raise ValueError(f"Invalid task graph version at line {line_number}")
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


class TaskGraphManager:
    def __init__(
        self,
        *,
        store: TaskGraphStore | None = None,
        scope_id: str = "default",
    ) -> None:
        self.store = store
        self.scope_id = validate_run_id(scope_id)
        self.history: list[TaskGraphSnapshot] = []
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            if self._initialized:
                return
            self.history = await self.store.load(self.scope_id) if self.store else []
            self._initialized = True

    @property
    def current(self) -> TaskGraphSnapshot | None:
        return self.history[-1] if self.history else None

    async def replace(
        self,
        tasks: list[TaskNode],
        *,
        direction: str = "",
    ) -> TaskGraphSnapshot:
        await self.initialize()
        async with self._lock:
            for _ in range(10):
                await self._refresh_locked()
                previous = self.current
                snapshot = TaskGraphSnapshot(
                    version=(previous.version + 1) if previous is not None else 1,
                    state=TaskGraphState.ACTIVE,
                    direction=direction.strip(),
                    tasks=tasks,
                    supersedes_version=previous.version if previous is not None else None,
                )
                if previous is not None and previous.state == TaskGraphState.ACTIVE:
                    _validate_active_transition(previous, snapshot)
                try:
                    if self.store is not None:
                        await self.store.append(self.scope_id, snapshot)
                except TaskGraphConflictError:
                    continue
                self.history.append(snapshot)
                return snapshot
            raise TaskGraphConflictError("Task graph remained busy during replace")

    async def supersede(self, direction: str) -> TaskGraphSnapshot | None:
        await self.initialize()
        async with self._lock:
            for _ in range(10):
                await self._refresh_locked()
                previous = self.current
                if previous is None:
                    return None
                snapshot = TaskGraphSnapshot(
                    version=previous.version + 1,
                    state=TaskGraphState.SUPERSEDED,
                    direction=direction.strip(),
                    tasks=previous.tasks,
                    supersedes_version=previous.version,
                )
                try:
                    if self.store is not None:
                        await self.store.append(self.scope_id, snapshot)
                except TaskGraphConflictError:
                    continue
                self.history.append(snapshot)
                return snapshot
            raise TaskGraphConflictError("Task graph remained busy during supersede")

    async def claim_next(self, owner: str) -> TaskNode | None:
        await self.initialize()
        owner = owner.strip()
        if not owner or len(owner) > 128:
            raise ValueError("Task claim owner must contain 1-128 characters")
        async with self._lock:
            for _ in range(10):
                await self._refresh_locked()
                current = self._require_active()
                if any(
                    task.status == TaskNodeStatus.IN_PROGRESS and task.owner == owner
                    for task in current.tasks
                ):
                    raise ValueError(f"Owner {owner!r} already has an in-progress task")
                by_id = {task.task_id: task for task in current.tasks}
                selected = next(
                    (
                        task
                        for task in current.tasks
                        if task.status == TaskNodeStatus.PENDING
                        and task.preferred_owner in {None, owner}
                        and all(
                            by_id[dependency].status == TaskNodeStatus.COMPLETED
                            for dependency in task.blocked_by
                        )
                    ),
                    None,
                )
                if selected is None:
                    return None
                claimed = selected.model_copy(
                    update={
                        "status": TaskNodeStatus.IN_PROGRESS,
                        "owner": owner,
                        "attempts": selected.attempts + 1,
                    }
                )
                try:
                    await self._append_task_update(current, claimed)
                except TaskGraphConflictError:
                    continue
                return claimed
            raise TaskGraphConflictError("Task graph remained busy during claim")

    async def assign_pending(self, task_id: str, owner: str) -> TaskNode:
        owner = owner.strip()
        if not owner or len(owner) > 128:
            raise ValueError("Task assignment owner must contain 1-128 characters")
        return await self._update_pending(task_id, {"preferred_owner": owner})

    async def clear_assignment(self, task_id: str) -> TaskNode:
        return await self._update_pending(task_id, {"preferred_owner": None})

    async def retry_blocked(self, task_id: str, *, owner: str | None = None) -> TaskNode:
        if owner is not None:
            owner = owner.strip()
            if not owner or len(owner) > 128:
                raise ValueError("Retry owner must contain 1-128 characters")
        await self.initialize()
        async with self._lock:
            for _ in range(10):
                await self._refresh_locked()
                current = self._require_active()
                by_id = {task.task_id: task for task in current.tasks}
                selected = by_id.get(task_id)
                if selected is None:
                    raise ValueError(f"Unknown blocked task: {task_id}")
                if selected.status != TaskNodeStatus.BLOCKED:
                    raise ValueError(f"Task {task_id!r} is not blocked")
                incomplete = [
                    dependency
                    for dependency in selected.blocked_by
                    if by_id[dependency].status != TaskNodeStatus.COMPLETED
                ]
                if incomplete:
                    raise ValueError(
                        "Cannot retry while dependencies are incomplete: "
                        + ", ".join(incomplete)
                    )
                updated = selected.model_copy(
                    update={
                        "status": TaskNodeStatus.PENDING,
                        "blocking_reason": "",
                        "owner": "agent",
                        "preferred_owner": owner,
                        "evidence": [],
                        "worktree": None,
                    }
                )
                try:
                    await self._append_task_update(current, updated)
                except TaskGraphConflictError:
                    continue
                return updated
            raise TaskGraphConflictError("Task graph remained busy during retry")

    async def attach_worktree(self, task_id: str, owner: str, worktree: str) -> TaskNode:
        return await self._update_claimed(task_id, owner, {"worktree": worktree})

    async def complete_claim(
        self,
        task_id: str,
        owner: str,
        *,
        evidence: list[str],
        worktree: str | None = None,
    ) -> TaskNode:
        if not evidence:
            raise ValueError("A completed claim requires evidence")
        update: dict[str, object] = {
            "status": TaskNodeStatus.COMPLETED,
            "evidence": evidence,
        }
        if worktree is not None:
            update["worktree"] = worktree
        return await self._update_claimed(task_id, owner, update)

    async def block_claim(
        self,
        task_id: str,
        owner: str,
        *,
        reason: str,
        worktree: str | None = None,
    ) -> TaskNode:
        reason = reason.strip()
        if not reason:
            raise ValueError("A blocked claim requires a reason")
        update: dict[str, object] = {
            "status": TaskNodeStatus.BLOCKED,
            "blocking_reason": reason[:2000],
        }
        if worktree is not None:
            update["worktree"] = worktree
        return await self._update_claimed(task_id, owner, update)

    async def block_unreachable(self) -> list[TaskNode]:
        await self.initialize()
        async with self._lock:
            for _ in range(10):
                await self._refresh_locked()
                current = self._require_active()
                tasks = list(current.tasks)
                changed: list[TaskNode] = []
                while True:
                    by_id = {task.task_id: task for task in tasks}
                    round_changed = False
                    for index, task in enumerate(tasks):
                        if task.status != TaskNodeStatus.PENDING:
                            continue
                        blocked = [
                            dependency
                            for dependency in task.blocked_by
                            if by_id[dependency].status == TaskNodeStatus.BLOCKED
                        ]
                        if not blocked:
                            continue
                        updated = task.model_copy(
                            update={
                                "status": TaskNodeStatus.BLOCKED,
                                "blocking_reason": (
                                    "Dependency blocked: " + ", ".join(sorted(blocked))
                                ),
                            }
                        )
                        tasks[index] = updated
                        changed.append(updated)
                        round_changed = True
                    if not round_changed:
                        break
                if not changed:
                    return []
                try:
                    await self._append_snapshot(current, tasks)
                except TaskGraphConflictError:
                    continue
                return changed
            raise TaskGraphConflictError("Task graph remained busy during block propagation")

    async def _update_claimed(
        self,
        task_id: str,
        owner: str,
        update: dict[str, object],
    ) -> TaskNode:
        await self.initialize()
        async with self._lock:
            for _ in range(10):
                await self._refresh_locked()
                current = self._require_active()
                selected = next((task for task in current.tasks if task.task_id == task_id), None)
                if selected is None:
                    raise ValueError(f"Unknown task graph claim: {task_id}")
                if selected.status != TaskNodeStatus.IN_PROGRESS or selected.owner != owner:
                    raise ValueError(f"Task {task_id!r} is not claimed by {owner!r}")
                updated = selected.model_copy(update=update)
                try:
                    await self._append_task_update(current, updated)
                except TaskGraphConflictError:
                    continue
                return updated
            raise TaskGraphConflictError("Task graph remained busy during claim update")

    async def _update_pending(
        self,
        task_id: str,
        update: dict[str, object],
    ) -> TaskNode:
        await self.initialize()
        async with self._lock:
            for _ in range(10):
                await self._refresh_locked()
                current = self._require_active()
                selected = next((task for task in current.tasks if task.task_id == task_id), None)
                if selected is None:
                    raise ValueError(f"Unknown pending task: {task_id}")
                if selected.status != TaskNodeStatus.PENDING:
                    raise ValueError(f"Task {task_id!r} is not pending")
                updated = selected.model_copy(update=update)
                try:
                    await self._append_task_update(current, updated)
                except TaskGraphConflictError:
                    continue
                return updated
            raise TaskGraphConflictError("Task graph remained busy during assignment")

    async def _append_task_update(
        self,
        current: TaskGraphSnapshot,
        updated: TaskNode,
    ) -> None:
        tasks = [updated if task.task_id == updated.task_id else task for task in current.tasks]
        await self._append_snapshot(current, tasks)

    async def _append_snapshot(
        self,
        current: TaskGraphSnapshot,
        tasks: list[TaskNode],
    ) -> TaskGraphSnapshot:
        snapshot = TaskGraphSnapshot(
            version=current.version + 1,
            state=TaskGraphState.ACTIVE,
            direction=current.direction,
            tasks=tasks,
            supersedes_version=current.version,
        )
        _validate_active_transition(current, snapshot)
        if self.store is not None:
            await self.store.append(self.scope_id, snapshot)
        self.history.append(snapshot)
        return snapshot

    async def _refresh_locked(self) -> None:
        if self.store is not None:
            self.history = await self.store.load(self.scope_id)

    def _require_active(self) -> TaskGraphSnapshot:
        current = self.current
        if current is None:
            raise ValueError("Task graph is empty")
        if current.state != TaskGraphState.ACTIVE:
            raise ValueError("Task graph is superseded")
        return current

    def render(self) -> str:
        current = self.current
        if current is None:
            return "<no task graph>"
        lines = [f"Task graph v{current.version} ({current.state})"]
        if current.direction:
            lines.append(f"Direction: {current.direction}")
        for task in current.tasks:
            dependencies = f" <- {','.join(task.blocked_by)}" if task.blocked_by else ""
            lines.append(
                f"[{task.status}] {task.task_id}: {task.title} "
                f"(owner={task.owner}, attempts={task.attempts}){dependencies}"
            )
            if task.blocking_reason:
                lines.append(f"  blocked: {task.blocking_reason}")
        return "\n".join(lines)


def _assert_acyclic(by_id: dict[str, TaskNode]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            raise ValueError(f"Task graph contains a dependency cycle at {task_id!r}")
        visiting.add(task_id)
        for dependency in by_id[task_id].blocked_by:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in by_id:
        visit(task_id)


def _validate_active_transition(
    previous: TaskGraphSnapshot,
    current: TaskGraphSnapshot,
) -> None:
    previous_by_id = {task.task_id: task for task in previous.tasks}
    current_by_id = {task.task_id: task for task in current.tasks}
    removed = sorted(set(previous_by_id) - set(current_by_id))
    if removed:
        raise ValueError(
            "Active task graph updates cannot remove tasks; supersede first: " + ", ".join(removed)
        )
    for task_id, old in previous_by_id.items():
        new = current_by_id[task_id]
        if new.attempts < old.attempts:
            raise ValueError(f"Task {task_id!r} attempts cannot decrease")
        if old.status == TaskNodeStatus.COMPLETED and new.status != TaskNodeStatus.COMPLETED:
            raise ValueError(f"Completed task {task_id!r} cannot regress")
