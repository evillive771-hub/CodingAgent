from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from uuid import uuid4

from pydantic import Field, field_validator

from aicoding_agent.agent.plan import PlanItem, PlanStatus
from aicoding_agent.domain.models import CodingOutcome, ToolRisk
from aicoding_agent.tools.base import (
    BaseTool,
    ToolContext,
    ToolError,
    ToolExecution,
    ToolInput,
    ToolRegistry,
)
from aicoding_agent.tools.paths import is_protected_relative, resolve_workspace_path


def _bounded_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    half = max(1, limit // 2)
    omitted = len(value) - (half * 2)
    return f"{value[:half]}\n... <{omitted} characters omitted> ...\n{value[-half:]}"


def _write_utf8_atomic(path: Path, content: str, *, create_only: bool = False) -> None:
    """Publish fully written content while the host-owned file lock is held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    existing_mode = None
    if path.exists() and not create_only:
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if existing_mode is not None:
            temporary.chmod(existing_mode)
        if create_only:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise ToolError(f"Path already exists: {path.name}") from exc
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_process_environment() -> dict[str, str]:
    allowed = {
        "COMSPEC",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


class ReadFileInput(ToolInput):
    path: str
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=400, ge=1, le=2000)


class ReadFileTool(BaseTool[ReadFileInput]):
    name = "read_file"
    description = "Read a bounded range of a UTF-8 text file inside the workspace."
    input_model = ReadFileInput
    risk = ToolRisk.READ
    concurrency_safe = True

    async def run(self, arguments: ReadFileInput, context: ToolContext) -> ToolExecution:
        path = resolve_workspace_path(
            context.workspace,
            arguments.path,
            allowed_prefixes=context.contract.allowed_path_prefixes,
        )
        if not path.is_file():
            raise ToolError(f"File does not exist: {arguments.path}")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ToolError("File is not valid UTF-8 text") from exc
        start = arguments.start_line - 1
        selected = lines[start : start + arguments.max_lines]
        rendered = "\n".join(
            f"{line_number}: {line}"
            for line_number, line in enumerate(selected, start=arguments.start_line)
        )
        return ToolExecution(
            output=rendered or "<empty range>",
            data={"path": arguments.path, "line_count": len(lines)},
        )


class ListFilesInput(ToolInput):
    path: str = "."
    pattern: str = "**/*"
    max_results: int = Field(default=300, ge=1, le=2000)


class ListFilesTool(BaseTool[ListFilesInput]):
    name = "list_files"
    description = "List files under a workspace directory with a bounded glob query."
    input_model = ListFilesInput
    risk = ToolRisk.READ
    concurrency_safe = True

    async def run(self, arguments: ListFilesInput, context: ToolContext) -> ToolExecution:
        root = resolve_workspace_path(
            context.workspace,
            arguments.path,
            allowed_prefixes=context.contract.allowed_path_prefixes,
        )
        if not root.is_dir():
            raise ToolError(f"Directory does not exist: {arguments.path}")
        results: list[str] = []
        for candidate in root.glob(arguments.pattern):
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(context.workspace)
            lowered = {part.lower() for part in relative.parts}
            if is_protected_relative(relative) or lowered & {"node_modules", "dist", "build"}:
                continue
            results.append(relative.as_posix())
            if len(results) >= arguments.max_results:
                break
        results.sort()
        return ToolExecution(
            output="\n".join(results) or "<no files>",
            data={"count": len(results), "truncated": len(results) >= arguments.max_results},
        )


class SearchTextInput(ToolInput):
    query: str = Field(min_length=1, max_length=1000)
    path: str = "."
    pattern: str = "**/*"
    max_results: int = Field(default=200, ge=1, le=1000)
    case_sensitive: bool = False


class SearchTextTool(BaseTool[SearchTextInput]):
    name = "search_text"
    description = "Search for a literal text string in UTF-8 workspace files."
    input_model = SearchTextInput
    risk = ToolRisk.READ
    concurrency_safe = True

    async def run(self, arguments: SearchTextInput, context: ToolContext) -> ToolExecution:
        root = resolve_workspace_path(
            context.workspace,
            arguments.path,
            allowed_prefixes=context.contract.allowed_path_prefixes,
        )
        needle = arguments.query if arguments.case_sensitive else arguments.query.casefold()
        matches: list[str] = []
        for candidate in root.glob(arguments.pattern):
            if not candidate.is_file() or candidate.stat().st_size > 2_000_000:
                continue
            relative = candidate.relative_to(context.workspace)
            lowered = {part.lower() for part in relative.parts}
            if is_protected_relative(relative) or lowered & {"node_modules", "dist", "build"}:
                continue
            resolved_candidate = candidate.resolve()
            try:
                resolved_candidate.relative_to(context.workspace)
            except ValueError:
                continue
            try:
                lines = resolved_candidate.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for line_number, line in enumerate(lines, start=1):
                haystack = line if arguments.case_sensitive else line.casefold()
                if needle in haystack:
                    matches.append(f"{relative.as_posix()}:{line_number}: {line[:500]}")
                    if len(matches) >= arguments.max_results:
                        return ToolExecution(
                            output="\n".join(matches),
                            data={"count": len(matches), "truncated": True},
                        )
        return ToolExecution(
            output="\n".join(matches) or "<no matches>",
            data={"count": len(matches), "truncated": False},
        )


class ApplyTextPatchInput(ToolInput):
    path: str
    old_text: str = Field(min_length=1, max_length=200000)
    new_text: str = Field(max_length=200000)
    expected_replacements: int = Field(default=1, ge=1, le=100)


class ApplyTextPatchTool(BaseTool[ApplyTextPatchInput]):
    name = "apply_text_patch"
    description = (
        "Replace an exact text fragment in an existing workspace file. "
        "The operation fails unless the expected match count is exact."
    )
    input_model = ApplyTextPatchInput
    risk = ToolRisk.WRITE

    async def run(self, arguments: ApplyTextPatchInput, context: ToolContext) -> ToolExecution:
        path = resolve_workspace_path(
            context.workspace,
            arguments.path,
            allowed_prefixes=context.contract.allowed_path_prefixes,
            for_write=True,
        )
        if not path.is_file():
            raise ToolError(f"File does not exist: {arguments.path}")
        try:
            original = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError("File is not valid UTF-8 text") from exc
        actual = original.count(arguments.old_text)
        if actual != arguments.expected_replacements:
            raise ToolError(
                f"Patch expected {arguments.expected_replacements} matches but found {actual}"
            )
        updated = original.replace(
            arguments.old_text,
            arguments.new_text,
            arguments.expected_replacements,
        )
        _write_utf8_atomic(path, updated)
        return ToolExecution(
            output=f"Updated {arguments.path} ({actual} replacement(s))",
            data={"path": arguments.path, "replacements": actual},
            is_mutation=True,
        )


class CreateFileInput(ToolInput):
    path: str
    content: str = Field(max_length=500000)


class CreateFileTool(BaseTool[CreateFileInput]):
    name = "create_file"
    description = "Create a new UTF-8 text file inside the workspace; never overwrites a file."
    input_model = CreateFileInput
    risk = ToolRisk.WRITE

    async def run(self, arguments: CreateFileInput, context: ToolContext) -> ToolExecution:
        path = resolve_workspace_path(
            context.workspace,
            arguments.path,
            allowed_prefixes=context.contract.allowed_path_prefixes,
            for_write=True,
        )
        if path.exists():
            raise ToolError(f"Path already exists: {arguments.path}")
        try:
            _write_utf8_atomic(path, arguments.content, create_only=True)
        except ToolError as exc:
            raise ToolError(f"Path already exists: {arguments.path}") from exc
        return ToolExecution(
            output=f"Created {arguments.path}",
            data={"path": arguments.path},
            is_mutation=True,
        )


class RunProcessInput(ToolInput):
    program: str = Field(min_length=1, max_length=128)
    args: list[str] = Field(default_factory=list, max_length=64)
    cwd: str = "."
    timeout_seconds: float = Field(default=120.0, gt=0, le=1800)

    @field_validator("program")
    @classmethod
    def program_must_be_a_name(cls, value: str) -> str:
        if Path(value).name != value or any(char in value for char in ";&|`$<>"):
            raise ValueError("program must be an executable name, not a path or shell expression")
        return value


class RunProcessTool(BaseTool[RunProcessInput]):
    name = "run_process"
    description = (
        "Run an exact contract-approved verification command or read-only Git command. "
        "Arguments are passed without a shell."
    )
    input_model = RunProcessInput
    risk = ToolRisk.PROCESS

    async def run(self, arguments: RunProcessInput, context: ToolContext) -> ToolExecution:
        cwd = resolve_workspace_path(
            context.workspace,
            arguments.cwd,
            allowed_prefixes=context.contract.allowed_path_prefixes,
        )
        if not cwd.is_dir():
            raise ToolError(f"Process cwd does not exist: {arguments.cwd}")
        try:
            process = await asyncio.create_subprocess_exec(
                arguments.program,
                *arguments.args,
                cwd=str(cwd),
                env=_safe_process_environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise ToolError(f"Could not start executable: {arguments.program}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=arguments.timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise ToolError(f"Process timed out after {arguments.timeout_seconds:g}s") from exc

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        combined = "\n".join(part for part in (stdout_text, stderr_text) if part).strip()
        limit = context.contract.budget.max_tool_output_chars
        output = _bounded_text(combined or "<no output>", limit)

        verification_id = _verification_id(arguments, context)
        return ToolExecution(
            output=f"exit_code={process.returncode}\n{output}",
            data={
                "argv": [arguments.program, *arguments.args],
                "cwd": arguments.cwd,
                "exit_code": process.returncode,
                "verification_id": verification_id,
            },
        )


def _verification_id(arguments: RunProcessInput, context: ToolContext) -> str | None:
    argv = (Path(arguments.program.lower()).stem, *arguments.args)
    for command in context.contract.verification_commands:
        expected = (Path(command.program.lower()).stem, *command.args)
        if argv == expected and arguments.cwd == command.cwd:
            return command.id
    return None


class StartBackgroundProcessInput(RunProcessInput):
    pass


class StartBackgroundProcessTool(BaseTool[StartBackgroundProcessInput]):
    name = "start_background_process"
    description = (
        "Start an exact contract-approved verification command in the background. "
        "Completion is injected later as a task notification."
    )
    input_model = StartBackgroundProcessInput
    risk = ToolRisk.PROCESS

    async def run(
        self, arguments: StartBackgroundProcessInput, context: ToolContext
    ) -> ToolExecution:
        if context.background_manager is None:
            raise ToolError("This Agent run has no background task manager")
        cwd = resolve_workspace_path(
            context.workspace,
            arguments.cwd,
            allowed_prefixes=context.contract.allowed_path_prefixes,
        )
        if not cwd.is_dir():
            raise ToolError(f"Process cwd does not exist: {arguments.cwd}")
        job = await context.background_manager.start(
            program=arguments.program,
            args=arguments.args,
            cwd=cwd,
            env=_safe_process_environment(),
            timeout_seconds=arguments.timeout_seconds,
            verification_id=_verification_id(arguments, context),
        )
        return ToolExecution(
            output=f"Background task started: {job.job_id}",
            data={"background_job_id": job.job_id, "status": job.status},
        )


class BackgroundStatusInput(ToolInput):
    pass


class BackgroundStatusTool(BaseTool[BackgroundStatusInput]):
    name = "background_status"
    description = "Show bounded status lines for background tasks in this Agent session."
    input_model = BackgroundStatusInput
    risk = ToolRisk.READ
    concurrency_safe = True

    async def run(self, arguments: BackgroundStatusInput, context: ToolContext) -> ToolExecution:
        if context.background_manager is None:
            raise ToolError("This Agent run has no background task manager")
        return ToolExecution(output=context.background_manager.render())


class SubmitResultInput(ToolInput):
    summary: str = Field(min_length=1, max_length=10000)
    changed_files: list[str] = Field(default_factory=list, max_length=500)
    risk_notes: list[str] = Field(default_factory=list, max_length=100)


class SubmitResultTool(BaseTool[SubmitResultInput]):
    name = "submit_result"
    description = (
        "Submit the structured coding outcome after all required checks pass. "
        "This proposes completion; the host Stop Gate makes the final decision."
    )
    input_model = SubmitResultInput
    risk = ToolRisk.READ

    async def run(self, arguments: SubmitResultInput, context: ToolContext) -> ToolExecution:
        outcome = CodingOutcome.model_validate(arguments.model_dump())
        context.state["coding_outcome"] = outcome
        return ToolExecution(
            output="Coding outcome recorded; end the turn so the host can evaluate completion.",
            data={"outcome": outcome.model_dump()},
        )


class AskUserInput(ToolInput):
    question: str = Field(min_length=1, max_length=5000)


class AskUserTool(BaseTool[AskUserInput]):
    name = "ask_user"
    description = (
        "Pause the current turn and ask the user one concrete clarification question. "
        "Use only when the missing answer materially changes the implementation."
    )
    input_model = AskUserInput
    risk = ToolRisk.READ

    async def run(self, arguments: AskUserInput, context: ToolContext) -> ToolExecution:
        context.state["user_question"] = arguments.question.strip()
        return ToolExecution(
            output="The question will be shown to the user and this turn will pause.",
            data={"waiting_for_user": True},
        )


class UpdatePlanItemInput(ToolInput):
    content: str = Field(min_length=1, max_length=2000)
    status: PlanStatus = PlanStatus.PENDING
    evidence: list[str] = Field(default_factory=list, max_length=20)


class UpdatePlanInput(ToolInput):
    items: list[UpdatePlanItemInput] = Field(min_length=1, max_length=20)
    direction: str = Field(default="", max_length=20000)


class UpdatePlanTool(BaseTool[UpdatePlanInput]):
    name = "update_plan"
    description = (
        "Replace the visible execution plan with 1-20 validated items. "
        "Use at most one in_progress item and update the plan after user steering."
    )
    input_model = UpdatePlanInput
    risk = ToolRisk.WRITE

    async def run(self, arguments: UpdatePlanInput, context: ToolContext) -> ToolExecution:
        if context.plan_manager is None:
            raise ToolError("This Agent run has no plan manager")
        snapshot = await context.plan_manager.replace(
            [
                PlanItem(
                    content=item.content,
                    status=item.status,
                    evidence=item.evidence,
                    source="agent",
                )
                for item in arguments.items
            ],
            direction=arguments.direction,
        )
        return ToolExecution(
            output=context.plan_manager.render(),
            data={"plan_version": snapshot.version, "plan_state": snapshot.state},
        )


def build_builtin_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (
        ReadFileTool(),
        ListFilesTool(),
        SearchTextTool(),
        ApplyTextPatchTool(),
        CreateFileTool(),
        RunProcessTool(),
        StartBackgroundProcessTool(),
        BackgroundStatusTool(),
        SubmitResultTool(),
        AskUserTool(),
        UpdatePlanTool(),
    ):
        registry.register(tool)
    return registry
