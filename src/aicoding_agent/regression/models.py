from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from aicoding_agent.domain.models import BrowserVerificationContract, VerificationCommand


class RegressionTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    objective: str
    acceptance_criteria: list[str]
    commands: list[VerificationCommand] = Field(default_factory=list)
    browsers: list[BrowserVerificationContract] = Field(default_factory=list)
    test_fingerprints: dict[str, str] = Field(default_factory=dict)


class RegistryState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 0
    fingerprint: str = ""
    tasks: list[RegressionTask] = Field(default_factory=list)


class AcceptedBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int
    source_workspace: str = ""
    snapshot_path: str
    candidate_fingerprint: str
    registry_version: int
    registry_fingerprint: str
    report_path: str | None = None
    report_hash: str | None = None
    verified: bool = False
    git_revision: str | None = None
