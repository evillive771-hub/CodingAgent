from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeliveryStage(StrEnum):
    CREATED = "created"
    PREPARING_WORKSPACE = "preparing_workspace"
    CODING = "coding"
    INSPECTING_CHANGES = "inspecting_changes"
    TEST_DEPLOYING = "test_deploying"
    TEST_VERIFYING = "test_verifying"
    AWAITING_APPROVAL = "awaiting_approval"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


class ApprovalRecord(WorkflowModel):
    decision: Literal["approved", "rejected"]
    subject_hash: str = Field(min_length=64, max_length=64)
    approver: str = Field(min_length=1, max_length=256)
    comment: str = Field(default="", max_length=5000)
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WorkflowRun(WorkflowModel):
    run_id: str
    task_id: str
    source_workspace: str | None = None
    workspace: str
    workspace_lease: dict[str, Any] | None = None
    workspace_released: bool = False
    workspace_cleanup_error: str | None = None
    stage: DeliveryStage = DeliveryStage.CREATED
    contract: dict[str, Any]
    agent_result: dict[str, Any] | None = None
    change_snapshot: dict[str, Any] | None = None
    deployment_receipt: dict[str, Any] | None = None
    verification_report: dict[str, Any] | None = None
    approval_package: dict[str, Any] | None = None
    approval_subject_hash: str | None = None
    approval: dict[str, Any] | None = None
    publish_receipt: dict[str, Any] | None = None
    failure_reason: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WorkflowResult(WorkflowModel):
    run_id: str
    stage: DeliveryStage
    approval_subject_hash: str | None = None
    failure_reason: str | None = None
    workspace_released: bool = False
    workspace_cleanup_error: str | None = None
