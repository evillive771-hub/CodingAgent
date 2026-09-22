from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Protocol

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


class ArtifactEntry(PortModel):
    path: str
    size: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)


class BuildReceipt(PortModel):
    build_id: str
    artifact_hash: str = Field(min_length=64, max_length=64)
    artifact_root: str
    manifest_ref: str
    artifacts: list[ArtifactEntry] = Field(min_length=1)
    command_evidence_refs: list[str] = Field(default_factory=list)


class DeploymentReceipt(PortModel):
    deployment_id: str
    environment: str = "test"
    artifact_hash: str
    location: str
    previous_location: str | None = None
    manifest_ref: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class RollbackReceipt(PortModel):
    rollback_id: str
    deployment_id: str
    ok: bool
    isolated: bool = False
    evidence_refs: list[str] = Field(default_factory=list)


class StageExecutionError(RuntimeError):
    """Adapter failure carrying evidence and any rollback already performed by it."""

    def __init__(
        self,
        message: str,
        *,
        evidence_refs: list[str] | None = None,
        rollback_status: Literal["not_requested", "succeeded", "failed"] = "not_requested",
    ) -> None:
        super().__init__(message)
        self.evidence_refs = tuple(evidence_refs or ())
        self.rollback_status = rollback_status


class VerificationReport(PortModel):
    ok: bool
    summary: str
    evidence_refs: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


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


class ArtifactBuilder(Protocol):
    async def build(
        self,
        workspace: Path,
        contract: TaskContract,
        *,
        change_hash: str,
        idempotency_key: str,
    ) -> BuildReceipt: ...


class TestEnvironmentPublisher(Protocol):
    async def publish(
        self,
        workspace: Path,
        contract: TaskContract,
        *,
        change_hash: str,
        build_receipt: BuildReceipt | None = None,
        idempotency_key: str,
    ) -> DeploymentReceipt: ...

    async def rollback(
        self,
        receipt: DeploymentReceipt,
        *,
        idempotency_key: str,
    ) -> RollbackReceipt | None: ...


class BrowserVerifier(Protocol):
    async def verify(
        self,
        receipt: DeploymentReceipt,
        contract: TaskContract,
    ) -> VerificationReport: ...
