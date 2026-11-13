from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

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


class SubagentRole(StrEnum):
    EXPLORE = "explore"
    REVIEW = "review"
    TEST_ANALYST = "test_analyst"


class SubagentStatus(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"


class SubagentFindingInput(StrictModel):
    path: str = Field(min_length=1, max_length=500)
    line: int = Field(ge=1, le=10_000_000)
    observation: str = Field(min_length=1, max_length=2000)


class SubagentFinding(SubagentFindingInput):
    snippet: str = Field(max_length=1000)


class SubagentReport(StrictModel):
    role: SubagentRole
    status: SubagentStatus
    summary: str = Field(min_length=1, max_length=5000)
    findings: list[SubagentFinding] = Field(default_factory=list, max_length=50)
    uncertainties: list[str] = Field(default_factory=list, max_length=50)
    turns: int = Field(default=0, ge=0, le=50)
    tool_calls: int = Field(default=0, ge=0, le=200)


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

    @field_validator("program")
    @classmethod
    def program_must_be_an_executable_name(cls, value: str) -> str:
        if Path(value).name != value or any(char in value for char in ";&|`$<>"):
            raise ValueError("command program must be an executable name, not a path or shell")
        return value

    @property
    def argv(self) -> tuple[str, ...]:
        return (self.program, *self.args)


class DiagnosticParser(StrEnum):
    TYPESCRIPT = "typescript"
    ESLINT_JSON = "eslint_json"
    GENERIC = "generic"


class BrowserVerificationContract(StrictModel):
    """A public browser suite selected from host-owned, frozen configuration."""

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    required: bool = True
    suite_ref: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    timeout_seconds: float = Field(default=120, gt=0, le=1800)
    max_runs: int = Field(default=6, ge=1, le=100)
    max_failures_returned: int = Field(default=10, ge=1, le=50)


class DiagnosticCommand(VerificationCommand):
    parser: DiagnosticParser = DiagnosticParser.GENERIC
    timeout_seconds: float = Field(default=120, gt=0, le=1800)


class BuildCommand(VerificationCommand):
    """A deterministic build command executed by the delivery host, never the model."""

    timeout_seconds: float = Field(default=900, gt=0, le=3600)

    @model_validator(mode="after")
    def secrets_must_not_be_passed_on_the_command_line(self) -> BuildCommand:
        secret_flags = {"--api-key", "--password", "--token", "--access-token", "-p"}
        for argument in self.args:
            lowered = argument.lower()
            if (
                lowered in secret_flags
                or any(lowered.startswith(f"{flag}=") for flag in secret_flags if flag != "-p")
                or any(
                    marker in lowered
                    for marker in ("api_key=", "apikey=", "password=", "access_token=", "token=")
                )
            ):
                raise ValueError("build command secrets must come from a protected host facility")
        return self


class ReleasePolicy(StrictModel):
    auto_deploy_test: bool = True
    production_requires_separate_approval: Literal[True] = True


class TddSpec(StrictModel):
    red_cases: dict[str, list[str]] = Field(default_factory=dict, max_length=100)
    propose_tests: bool = False

    @model_validator(mode="after")
    def declared_cases_must_be_unique(self) -> TddSpec:
        if not self.red_cases and not self.propose_tests:
            raise ValueError("TDD requires target cases or a test proposal stage")
        for suite_id, cases in self.red_cases.items():
            if (not suite_id or len(suite_id) > 64 or not cases or len(cases) > 500
                    or len(cases) != len(set(cases))
                    or any(not case.strip() or len(case) > 500 for case in cases)):
                raise ValueError("TDD target case IDs must be nonempty, unique and bounded")
        return self


class TaskContract(StrictModel):
    task_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    source: TaskSource = TaskSource.MANUAL
    source_ticket_id: str | None = Field(default=None, max_length=256)
    objective: str = Field(min_length=1, max_length=20000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=100)
    repository_url: str | None = Field(default=None, max_length=2000)
    base_revision: str | None = Field(default=None, max_length=256)
    allowed_tools: set[str] = Field(
        default_factory=lambda: {
            "read_file",
            "list_files",
            "search_text",
            "load_skill",
            "get_diagnostics",
            "update_task_graph",
            "delegate_readonly",
            "recall_memory",
            "propose_memory",
            "history_search",
            "history_read_item",
            "history_list_items",
            "propose_team",
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
    browser_verifications: list[BrowserVerificationContract] = Field(default_factory=list)
    tdd: TddSpec | None = None
    build_commands: list[BuildCommand] = Field(default_factory=list)
    artifact_paths: list[str] = Field(default_factory=list, max_length=100)
    diagnostic_commands: list[DiagnosticCommand] = Field(default_factory=list)
    rule_files: list[str] = Field(default_factory=list)
    skill_directories: list[str] = Field(default_factory=lambda: ["skills"])
    target_environment: TargetEnvironment = TargetEnvironment.LOCAL
    release_policy: ReleasePolicy = Field(default_factory=ReleasePolicy)
    requires_plan_approval: bool = False
    execution_plan: str | None = Field(default=None, max_length=20_000)
    budget: Budget = Field(default_factory=Budget)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("repository_url")
    @classmethod
    def repository_url_must_not_embed_credentials(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or any(character in value for character in "\r\n\0"):
            raise ValueError("repository_url must be a bounded single-line Git address")
        parsed = urlsplit(value)
        if parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError("repository_url must not embed credentials, query, or fragment")
        if parsed.scheme in {"http", "https"} and parsed.username is not None:
            raise ValueError("HTTP repository_url must not embed credentials")
        return value

    @field_validator("allowed_path_prefixes", "rule_files", "skill_directories")
    @classmethod
    def paths_must_be_relative(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value or value.startswith(("/", "\\")) or ":" in value:
                raise ValueError("contract paths must be relative to the workspace")
        return values

    @field_validator("artifact_paths")
    @classmethod
    def artifact_paths_must_be_safe(cls, values: list[str]) -> list[str]:
        for value in values:
            normalized = value.replace("\\", "/")
            if (
                not normalized
                or normalized.startswith("/")
                or ":" in normalized
                or ".." in normalized.split("/")
            ):
                raise ValueError("artifact paths must stay within the workspace")
        return values

    @model_validator(mode="after")
    def teambition_requires_ticket_id(self) -> TaskContract:
        verification_ids = [item.id for item in self.verification_commands]
        verification_ids.extend(item.id for item in self.browser_verifications)
        if len(verification_ids) != len(set(verification_ids)):
            raise ValueError("verification IDs must be unique across command and browser suites")
        if self.source == TaskSource.TEAMBITION and not self.source_ticket_id:
            raise ValueError("Teambition tasks require source_ticket_id")
        if self.requires_plan_approval and not (self.execution_plan or "").strip():
            raise ValueError("Tasks requiring plan approval must include execution_plan")
        return self


def canonical_task_contract_payload(
    contract: TaskContract | dict[str, Any],
) -> dict[str, Any]:
    validated = (
        contract if isinstance(contract, TaskContract) else TaskContract.model_validate(contract)
    )
    payload = validated.model_dump(mode="json")
    if payload.get("tdd") is None:
        payload.pop("tdd", None)
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
    BLOCKED = "blocked"
    SUBMITTED_UNVERIFIED = "submitted_unverified"
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
