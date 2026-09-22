from __future__ import annotations

import asyncio
import hashlib
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import Field, model_validator

from aicoding_agent.domain.models import StrictModel
from aicoding_agent.persistence.base import validate_run_id
from aicoding_agent.tools.base import ToolError
from aicoding_agent.tools.file_lock import WorkspaceFileLockManager


class TeamProtocolError(RuntimeError):
    pass


class TeamLaunchStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    STARTED = "started"
    CANCELLED = "cancelled"


class PlanApprovalStatus(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"


class TeamPlanApproval(StrictModel):
    version: int = Field(ge=1)
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    team_id: str = Field(min_length=1, max_length=128)
    worker: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=64)
    work_version: int = Field(ge=1)
    plan: str = Field(min_length=1, max_length=20_000)
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: PlanApprovalStatus = PlanApprovalStatus.REQUESTED
    feedback: str = Field(default="", max_length=5000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TeamRole(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    responsibility: str = Field(min_length=1, max_length=2000)


class TeamLaunchRecord(StrictModel):
    version: int = Field(ge=1)
    proposal_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    idempotency_key: str = Field(min_length=1, max_length=128)
    team_id: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=1, max_length=20_000)
    roles: list[TeamRole] = Field(min_length=1, max_length=8)
    status: TeamLaunchStatus = TeamLaunchStatus.PROPOSED
    feedback: str = Field(default="", max_length=5000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def role_names_are_unique(self) -> TeamLaunchRecord:
        names = [role.name for role in self.roles]
        if len(set(names)) != len(names):
            raise ValueError("Team role names must be unique")
        return self


class TeamLaunchManager:
    """Append-only user confirmation gate for starting a writable Agent team."""

    def __init__(self, root: Path, *, lock_timeout_seconds: float = 10.0) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks = WorkspaceFileLockManager(timeout_seconds=lock_timeout_seconds)

    async def propose(
        self,
        *,
        team_id: str,
        goal: str,
        roles: list[TeamRole],
        idempotency_key: str,
    ) -> TeamLaunchRecord:
        team_id = validate_run_id(team_id)
        idempotency_key = validate_run_id(idempotency_key)
        goal = goal.strip()
        path = self._path(team_id)
        async with self._locked(path, owner="team-proposal"):
            history = await asyncio.to_thread(self._load_sync, path)
            existing = next(
                (item for item in history if item.idempotency_key == idempotency_key),
                None,
            )
            if existing is not None:
                if existing.goal != goal or existing.roles != roles:
                    raise TeamProtocolError(
                        "Team proposal idempotency key was reused with different content"
                    )
                return existing
            record = TeamLaunchRecord(
                version=len(history) + 1,
                proposal_id=uuid4().hex,
                idempotency_key=idempotency_key,
                team_id=team_id,
                goal=goal,
                roles=roles,
            )
            await asyncio.to_thread(self._append_record_sync, path, record)
            return record

    async def approve(self, team_id: str, proposal_id: str) -> TeamLaunchRecord:
        return await self._transition(
            team_id,
            proposal_id,
            expected={TeamLaunchStatus.PROPOSED},
            target=TeamLaunchStatus.APPROVED,
        )

    async def mark_started(self, team_id: str, proposal_id: str) -> TeamLaunchRecord:
        return await self._transition(
            team_id,
            proposal_id,
            expected={TeamLaunchStatus.APPROVED},
            target=TeamLaunchStatus.STARTED,
        )

    async def cancel(
        self,
        team_id: str,
        proposal_id: str,
        *,
        feedback: str = "",
    ) -> TeamLaunchRecord:
        return await self._transition(
            team_id,
            proposal_id,
            expected={TeamLaunchStatus.PROPOSED, TeamLaunchStatus.APPROVED},
            target=TeamLaunchStatus.CANCELLED,
            feedback=feedback,
        )

    async def require_approved(self, team_id: str, proposal_id: str) -> TeamLaunchRecord:
        record = await self.current(team_id)
        if record is None or record.proposal_id != proposal_id:
            raise TeamProtocolError("Unknown or stale team proposal")
        if record.status != TeamLaunchStatus.APPROVED:
            raise TeamProtocolError(
                f"Team proposal requires user approval; current status is {record.status}"
            )
        return record

    async def current(self, team_id: str) -> TeamLaunchRecord | None:
        team_id = validate_run_id(team_id)
        path = self._path(team_id)
        async with self._locked(path, owner="team-status"):
            history = await asyncio.to_thread(self._load_sync, path)
        return history[-1] if history else None

    async def _transition(
        self,
        team_id: str,
        proposal_id: str,
        *,
        expected: set[TeamLaunchStatus],
        target: TeamLaunchStatus,
        feedback: str = "",
    ) -> TeamLaunchRecord:
        team_id = validate_run_id(team_id)
        path = self._path(team_id)
        async with self._locked(path, owner=f"team-transition:{target}"):
            history = await asyncio.to_thread(self._load_sync, path)
            if not history or history[-1].proposal_id != proposal_id:
                raise TeamProtocolError("Unknown or stale team proposal")
            current = history[-1]
            if current.status == target:
                return current
            if current.status not in expected:
                raise TeamProtocolError(
                    f"Cannot change team proposal from {current.status} to {target}"
                )
            updated = current.model_copy(
                update={
                    "version": len(history) + 1,
                    "status": target,
                    "feedback": feedback.strip()[:5000],
                    "created_at": datetime.now(UTC),
                }
            )
            await asyncio.to_thread(self._append_record_sync, path, updated)
            return updated

    def _path(self, team_id: str) -> Path:
        return self.root / team_id / "launch.jsonl"

    @asynccontextmanager
    async def _locked(self, path: Path, *, owner: str):
        try:
            async with self._locks.acquire(self.root, path, owner=owner):
                yield
        except ToolError as exc:
            raise TeamProtocolError(str(exc)) from exc

    @staticmethod
    def _load_sync(path: Path) -> list[TeamLaunchRecord]:
        if not path.is_file():
            return []
        records: list[TeamLaunchRecord] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            record = TeamLaunchRecord.model_validate_json(line)
            if record.version != len(records) + 1:
                raise TeamProtocolError(
                    f"Invalid team launch version at line {line_number}"
                )
            records.append(record)
        return records

    @staticmethod
    def _append_record_sync(path: Path, record: TeamLaunchRecord) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(record.model_dump_json())
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


class TeamMessageType(StrEnum):
    DIRECT = "direct"
    RESULT = "result"
    IDLE = "idle"
    SHUTDOWN_REQUEST = "shutdown_request"
    SHUTDOWN_RESPONSE = "shutdown_response"
    PLAN_APPROVAL_REQUEST = "plan_approval_request"
    PLAN_APPROVAL_RESPONSE = "plan_approval_response"
    REASSIGNMENT_REQUEST = "reassignment_request"


_REQUEST_TYPES = {
    TeamMessageType.SHUTDOWN_REQUEST,
    TeamMessageType.SHUTDOWN_RESPONSE,
    TeamMessageType.PLAN_APPROVAL_REQUEST,
    TeamMessageType.PLAN_APPROVAL_RESPONSE,
    TeamMessageType.REASSIGNMENT_REQUEST,
}


class TeamMessage(StrictModel):
    message_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    sequence: int = Field(ge=1)
    team_id: str = Field(min_length=1, max_length=128)
    sender: str = Field(min_length=1, max_length=128)
    recipient: str = Field(min_length=1, max_length=128)
    message_type: TeamMessageType
    content: str = Field(default="", max_length=20_000)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    task_id: str | None = Field(default=None, min_length=1, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def protocol_messages_require_request_id(self) -> TeamMessage:
        if self.message_type in _REQUEST_TYPES and self.request_id is None:
            raise ValueError(f"{self.message_type} requires request_id")
        return self


class TeamMessageBus:
    """Durable typed team communication with cursor-based, non-destructive reads."""

    def __init__(
        self,
        root: Path,
        *,
        max_message_chars: int = 30_000,
        poll_seconds: float = 0.05,
        lock_timeout_seconds: float = 10.0,
    ) -> None:
        if max_message_chars < 1000:
            raise ValueError("Team message limit must be at least 1000 characters")
        if poll_seconds <= 0:
            raise ValueError("Team message poll interval must be positive")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_message_chars = max_message_chars
        self.poll_seconds = poll_seconds
        self._locks = WorkspaceFileLockManager(timeout_seconds=lock_timeout_seconds)
        self._conditions: dict[str, asyncio.Condition] = {}
        self._conditions_guard = asyncio.Lock()

    async def send(
        self,
        *,
        team_id: str,
        sender: str,
        recipient: str,
        message_type: TeamMessageType,
        content: str = "",
        request_id: str | None = None,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TeamMessage:
        team_id = validate_run_id(team_id)
        sender = validate_run_id(sender)
        recipient = validate_run_id(recipient)
        path = self._path(team_id)
        try:
            async with self._locks.acquire(
                self.root,
                path,
                owner=f"team-bus:{sender}",
            ):
                existing = await asyncio.to_thread(self._load_sync, path)
                message = TeamMessage(
                    message_id=uuid4().hex,
                    sequence=len(existing) + 1,
                    team_id=team_id,
                    sender=sender,
                    recipient=recipient,
                    message_type=message_type,
                    content=content,
                    request_id=request_id,
                    task_id=task_id,
                    metadata=metadata or {},
                )
                encoded = message.model_dump_json()
                if len(encoded) > self.max_message_chars:
                    raise TeamProtocolError("Team message exceeds its bounded size")
                await asyncio.to_thread(self._append_sync, path, encoded)
        except ToolError as exc:
            raise TeamProtocolError(str(exc)) from exc
        condition = await self._condition(team_id)
        async with condition:
            condition.notify_all()
        return message

    async def read(
        self,
        team_id: str,
        recipient: str,
        *,
        after_sequence: int = 0,
    ) -> list[TeamMessage]:
        team_id = validate_run_id(team_id)
        recipient = validate_run_id(recipient)
        if after_sequence < 0:
            raise ValueError("Team mailbox cursor cannot be negative")
        path = self._path(team_id)
        try:
            async with self._locks.acquire(
                self.root,
                path,
                owner=f"team-bus-reader:{recipient}",
            ):
                messages = await asyncio.to_thread(self._load_sync, path)
        except ToolError as exc:
            raise TeamProtocolError(str(exc)) from exc
        return [
            message
            for message in messages
            if message.recipient == recipient and message.sequence > after_sequence
        ]

    async def wait_for_messages(
        self,
        team_id: str,
        recipient: str,
        *,
        after_sequence: int = 0,
        timeout_seconds: float | None = None,
    ) -> list[TeamMessage]:
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("Team mailbox wait timeout cannot be negative")
        loop = asyncio.get_running_loop()
        deadline = None if timeout_seconds is None else loop.time() + timeout_seconds
        condition = await self._condition(validate_run_id(team_id))
        while True:
            messages = await self.read(
                team_id,
                recipient,
                after_sequence=after_sequence,
            )
            if messages:
                return messages
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return []
            wait_for = self.poll_seconds if remaining is None else min(self.poll_seconds, remaining)
            async with condition:
                try:
                    await asyncio.wait_for(condition.wait(), timeout=wait_for)
                except TimeoutError:
                    pass

    def _path(self, team_id: str) -> Path:
        return self.root / team_id / "messages.jsonl"

    async def _condition(self, team_id: str) -> asyncio.Condition:
        async with self._conditions_guard:
            return self._conditions.setdefault(team_id, asyncio.Condition())

    @staticmethod
    def _load_sync(path: Path) -> list[TeamMessage]:
        if not path.is_file():
            return []
        messages: list[TeamMessage] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            message = TeamMessage.model_validate_json(line)
            if message.sequence != len(messages) + 1:
                raise TeamProtocolError(
                    f"Invalid team message sequence at line {line_number}"
                )
            messages.append(message)
        return messages

    @staticmethod
    def _append_sync(path: Path, encoded: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


class TeamPlanApprovalManager:
    """Bind a human plan decision to one worker, task, and work-version tuple."""

    def __init__(
        self,
        root: Path,
        *,
        message_bus: TeamMessageBus | None = None,
        poll_seconds: float = 0.05,
        lock_timeout_seconds: float = 10.0,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("Plan approval poll interval must be positive")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.message_bus = message_bus
        self.poll_seconds = poll_seconds
        self._locks = WorkspaceFileLockManager(timeout_seconds=lock_timeout_seconds)

    async def request(
        self,
        *,
        team_id: str,
        worker: str,
        task_id: str,
        work_version: int,
        plan: str,
    ) -> TeamPlanApproval:
        team_id = validate_run_id(team_id)
        worker = validate_run_id(worker)
        plan = plan.strip()
        digest = hashlib.sha256(plan.encode("utf-8")).hexdigest()
        path = self._path(team_id, worker)
        async with self._locked(path, owner=f"plan-request:{worker}"):
            history = await asyncio.to_thread(self._load_sync, path)
            current = history[-1] if history else None
            if (
                current is not None
                and current.task_id == task_id
                and current.work_version == work_version
                and current.plan_sha256 == digest
            ):
                return current
            record = TeamPlanApproval(
                version=len(history) + 1,
                request_id=uuid4().hex,
                team_id=team_id,
                worker=worker,
                task_id=task_id,
                work_version=work_version,
                plan=plan,
                plan_sha256=digest,
            )
            await asyncio.to_thread(self._append_sync, path, record)
        if self.message_bus is not None:
            await self.message_bus.send(
                team_id=team_id,
                sender=worker,
                recipient="lead",
                message_type=TeamMessageType.PLAN_APPROVAL_REQUEST,
                content=plan,
                request_id=record.request_id,
                task_id=task_id,
                metadata={"work_version": work_version, "plan_sha256": digest},
            )
        return record

    async def respond(
        self,
        *,
        team_id: str,
        worker: str,
        request_id: str,
        approve: bool,
        feedback: str = "",
    ) -> TeamPlanApproval:
        team_id = validate_run_id(team_id)
        worker = validate_run_id(worker)
        path = self._path(team_id, worker)
        target = PlanApprovalStatus.APPROVED if approve else PlanApprovalStatus.REJECTED
        async with self._locked(path, owner=f"plan-response:{worker}"):
            history = await asyncio.to_thread(self._load_sync, path)
            if not history or history[-1].request_id != request_id:
                raise TeamProtocolError("Unknown or stale plan approval request")
            current = history[-1]
            if current.status == target:
                return current
            if current.status != PlanApprovalStatus.REQUESTED:
                raise TeamProtocolError(
                    f"Plan approval request is already {current.status}"
                )
            record = current.model_copy(
                update={
                    "version": len(history) + 1,
                    "status": target,
                    "feedback": feedback.strip()[:5000],
                    "created_at": datetime.now(UTC),
                }
            )
            await asyncio.to_thread(self._append_sync, path, record)
        if self.message_bus is not None:
            await self.message_bus.send(
                team_id=team_id,
                sender="lead",
                recipient=worker,
                message_type=TeamMessageType.PLAN_APPROVAL_RESPONSE,
                content=record.feedback or target,
                request_id=request_id,
                task_id=record.task_id,
                metadata={"approved": approve, "work_version": record.work_version},
            )
        return record

    async def current(self, team_id: str, worker: str) -> TeamPlanApproval | None:
        team_id = validate_run_id(team_id)
        worker = validate_run_id(worker)
        path = self._path(team_id, worker)
        async with self._locked(path, owner=f"plan-status:{worker}"):
            history = await asyncio.to_thread(self._load_sync, path)
        return history[-1] if history else None

    async def wait_for_decision(
        self,
        *,
        team_id: str,
        worker: str,
        request_id: str,
        timeout_seconds: float,
    ) -> TeamPlanApproval:
        if timeout_seconds <= 0:
            raise ValueError("Plan approval timeout must be positive")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            current = await self.current(team_id, worker)
            if current is None or current.request_id != request_id:
                raise TeamProtocolError("Plan approval request was superseded")
            if current.status != PlanApprovalStatus.REQUESTED:
                return current
            if loop.time() >= deadline:
                raise TeamProtocolError("Timed out waiting for plan approval")
            await asyncio.sleep(min(self.poll_seconds, deadline - loop.time()))

    async def require_approved(
        self,
        *,
        team_id: str,
        worker: str,
        task_id: str,
        work_version: int,
        plan_sha256: str,
    ) -> TeamPlanApproval:
        current = await self.current(team_id, worker)
        if (
            current is None
            or current.task_id != task_id
            or current.work_version != work_version
            or current.plan_sha256 != plan_sha256
        ):
            raise TeamProtocolError("Plan approval does not match current work")
        if current.status != PlanApprovalStatus.APPROVED:
            raise TeamProtocolError(f"Plan is not approved; current status is {current.status}")
        return current

    def _path(self, team_id: str, worker: str) -> Path:
        return self.root / team_id / "plans" / f"{worker}.jsonl"

    @asynccontextmanager
    async def _locked(self, path: Path, *, owner: str):
        try:
            async with self._locks.acquire(self.root, path, owner=owner):
                yield
        except ToolError as exc:
            raise TeamProtocolError(str(exc)) from exc

    @staticmethod
    def _load_sync(path: Path) -> list[TeamPlanApproval]:
        if not path.is_file():
            return []
        records: list[TeamPlanApproval] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            record = TeamPlanApproval.model_validate_json(line)
            if record.version != len(records) + 1:
                raise TeamProtocolError(
                    f"Invalid plan approval version at line {line_number}"
                )
            records.append(record)
        return records

    @staticmethod
    def _append_sync(path: Path, record: TeamPlanApproval) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(record.model_dump_json())
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
