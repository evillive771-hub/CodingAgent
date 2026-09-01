from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from aicoding_agent.domain.models import TaskContract


class PortModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TicketSnapshot(PortModel):
    ticket_id: str
    title: str
    description: str
    status: str
    assignee: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class ChangeSnapshot(PortModel):
    change_hash: str
    base_revision: str | None = None
    changed_files: list[str] = Field(default_factory=list)


class WorkspaceLease(PortModel):
    lease_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=256)
    task_id: str = Field(min_length=1, max_length=128)
    repository_path: str
    path: str
    base_revision: str = Field(min_length=7, max_length=128)
    contract_hash: str = Field(min_length=64, max_length=64)


class DeploymentReceipt(PortModel):
    deployment_id: str
    environment: str = "test"
    artifact_hash: str
    location: str


class VerificationReport(PortModel):
    ok: bool
    summary: str
    evidence_refs: list[str] = Field(default_factory=list)


class PublishReceipt(PortModel):
    revision: str
    remote: str
    branch: str | None = None


class TeambitionPort(Protocol):
    async def fetch_ticket(self, ticket_id: str) -> TicketSnapshot: ...

    async def transition_to_testing(
        self,
        ticket_id: str,
        *,
        revision: str,
        evidence_refs: list[str],
        idempotency_key: str,
    ) -> None: ...


class ChangeInspector(Protocol):
    async def snapshot(self, workspace: Path) -> ChangeSnapshot: ...


class WorkspaceManager(Protocol):
    async def prepare(
        self,
        repository: Path,
        contract: TaskContract,
        *,
        run_id: str,
    ) -> WorkspaceLease: ...

    async def release(self, lease: WorkspaceLease) -> None: ...


class ChangePublisher(Protocol):
    async def publish(
        self,
        workspace: Path,
        contract: TaskContract,
        *,
        expected_change_hash: str,
        idempotency_key: str,
    ) -> PublishReceipt: ...


class TestEnvironmentPublisher(Protocol):
    async def publish(
        self,
        workspace: Path,
        contract: TaskContract,
        *,
        change_hash: str,
        idempotency_key: str,
    ) -> DeploymentReceipt: ...

    async def rollback(
        self,
        receipt: DeploymentReceipt,
        *,
        idempotency_key: str,
    ) -> None: ...


class BrowserVerifier(Protocol):
    async def verify(
        self,
        receipt: DeploymentReceipt,
        contract: TaskContract,
    ) -> VerificationReport: ...
