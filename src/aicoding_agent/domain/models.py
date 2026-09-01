from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskSource(StrEnum):
    MANUAL = "manual"
    TEAMBITION = "teambition"


class TargetEnvironment(StrEnum):
    LOCAL = "local"
    TEST = "test"


class ToolRisk(StrEnum):
    READ = "read"
    WRITE = "write"
    PROCESS = "process"
    EXTERNAL = "external"


class Budget(StrictModel):
    max_turns: int = Field(default=30, ge=1, le=200)
    max_tool_calls: int = Field(default=120, ge=1, le=1000)
    max_stop_blocks: int = Field(default=5, ge=0, le=20)
    max_elapsed_seconds: int = Field(default=3600, ge=30, le=86400)
    max_tool_output_chars: int = Field(default=30000, ge=1000, le=200000)


class VerificationCommand(StrictModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    program: str = Field(min_length=1, max_length=128)
    args: list[str] = Field(default_factory=list, max_length=64)
    cwd: str = "."

    @field_validator("cwd")
    @classmethod
    def cwd_must_be_relative(cls, value: str) -> str:
        if not value or value.startswith(("/", "\\")) or ":" in value:
            raise ValueError("verification cwd must be relative to the workspace")
        return value

    @property
    def argv(self) -> tuple[str, ...]:
        return (self.program, *self.args)


class ReleasePolicy(StrictModel):
    auto_deploy_test: bool = True
    production_requires_separate_approval: Literal[True] = True


class TaskContract(StrictModel):
    task_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    source: TaskSource = TaskSource.MANUAL
    source_ticket_id: str | None = Field(default=None, max_length=256)
    objective: str = Field(min_length=1, max_length=20000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=100)
    repository_url: str | None = None
    base_revision: str | None = Field(default=None, max_length=256)
    allowed_tools: set[str] = Field(
        default_factory=lambda: {
            "read_file",
            "list_files",
            "search_text",
            "apply_text_patch",
            "create_file",
            "run_process",
            "submit_result",
        }
    )
    allowed_commands: set[str] = Field(
        default_factory=lambda: {"git", "node", "npm", "npx", "pnpm", "python", "pytest"}
    )
    allowed_path_prefixes: list[str] = Field(default_factory=lambda: ["."])
    verification_commands: list[VerificationCommand] = Field(default_factory=list)
    rule_files: list[str] = Field(default_factory=list)
    target_environment: TargetEnvironment = TargetEnvironment.LOCAL
    release_policy: ReleasePolicy = Field(default_factory=ReleasePolicy)
    budget: Budget = Field(default_factory=Budget)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("allowed_path_prefixes", "rule_files")
    @classmethod
    def paths_must_be_relative(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value or value.startswith(("/", "\\")) or ":" in value:
                raise ValueError("contract paths must be relative to the workspace")
        return values

    @model_validator(mode="after")
    def teambition_requires_ticket_id(self) -> TaskContract:
        if self.source == TaskSource.TEAMBITION and not self.source_ticket_id:
            raise ValueError("Teambition tasks require source_ticket_id")
        return self


def canonical_task_contract_payload(
    contract: TaskContract | dict[str, Any],
) -> dict[str, Any]:
    validated = (
        contract if isinstance(contract, TaskContract) else TaskContract.model_validate(contract)
    )
    payload = validated.model_dump(mode="json")
    payload["allowed_tools"] = sorted(validated.allowed_tools)
    payload["allowed_commands"] = sorted(validated.allowed_commands)
    return payload


class ToolCall(StrictModel):
    id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(StrictModel):
    content: str = ""
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: dict[str, int | float] = Field(default_factory=dict)
    finish_reason: str | None = None


class ToolResult(StrictModel):
    tool_call_id: str
    tool_name: str
    ok: bool
    output: str
    data: dict[str, Any] = Field(default_factory=dict)
    is_mutation: bool = False
    policy_denied: bool = False


class CodingOutcome(StrictModel):
    summary: str = Field(min_length=1, max_length=10000)
    changed_files: list[str] = Field(default_factory=list, max_length=500)
    risk_notes: list[str] = Field(default_factory=list, max_length=100)


class AgentStatus(StrEnum):
    COMPLETED = "completed"
    WAITING_FOR_USER = "waiting_for_user"
    INTERRUPTED = "interrupted"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"


class AgentResult(StrictModel):
    status: AgentStatus
    run_id: str
    final_text: str = ""
    outcome: CodingOutcome | None = None
    turns: int = 0
    tool_calls: int = 0
    reason: str | None = None
