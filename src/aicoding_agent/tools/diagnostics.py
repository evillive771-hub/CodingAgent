from __future__ import annotations

import asyncio
import json
import os
import re
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field

from aicoding_agent.domain.models import DiagnosticCommand, DiagnosticParser, ToolRisk
from aicoding_agent.tools.base import BaseTool, ToolContext, ToolError, ToolExecution, ToolInput
from aicoding_agent.tools.paths import resolve_workspace_path

_TSC_PAREN = re.compile(
    r"^(?P<path>.+?)\((?P<line>\d+),(?P<column>\d+)\): "
    r"(?P<severity>error|warning) (?P<code>TS\d+): (?P<message>.+)$",
    re.IGNORECASE,
)
_TSC_COLON = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):(?P<column>\d+)\s+-\s+"
    r"(?P<severity>error|warning) (?P<code>TS\d+): (?P<message>.+)$",
    re.IGNORECASE,
)


class DiagnosticSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class DiagnosticRecord(ToolInput):
    path: str
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    end_line: int | None = Field(default=None, ge=1)
    end_column: int | None = Field(default=None, ge=1)
    severity: DiagnosticSeverity
    code: str = Field(default="", max_length=128)
    message: str = Field(min_length=1, max_length=1000)
    source: str = Field(min_length=1, max_length=64)


class GetDiagnosticsInput(ToolInput):
    diagnostic_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    max_results: int = Field(default=100, ge=1, le=200)


class GetDiagnosticsTool(BaseTool[GetDiagnosticsInput]):
    name = "get_diagnostics"
    description = (
        "Run one task-contract diagnostic command by ID and return bounded structured findings. "
        "Diagnostics help investigation but do not count as completion verification."
    )
    input_model = GetDiagnosticsInput
    risk = ToolRisk.PROCESS

    async def run(self, arguments: GetDiagnosticsInput, context: ToolContext) -> ToolExecution:
        command = next(
            (
                item
                for item in context.contract.diagnostic_commands
                if item.id == arguments.diagnostic_id
            ),
            None,
        )
        if command is None:
            raise ToolError(f"Unknown task-contract diagnostic ID: {arguments.diagnostic_id}")
        cwd = resolve_workspace_path(
            context.workspace,
            command.cwd,
            allowed_prefixes=context.contract.allowed_path_prefixes,
        )
        if not cwd.is_dir():
            raise ToolError(f"Diagnostic cwd does not exist: {command.cwd}")
        try:
            process = await asyncio.create_subprocess_exec(
                command.program,
                *command.args,
                cwd=str(cwd),
                env=_safe_process_environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise ToolError(f"Could not start diagnostic executable: {command.program}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=command.timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise ToolError(
                f"Diagnostic {command.id!r} timed out after {command.timeout_seconds:g}s"
            ) from exc
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        records = parse_diagnostics(
            command,
            stdout=stdout_text,
            stderr=stderr_text,
            workspace=context.workspace,
            cwd=cwd,
            max_results=arguments.max_results,
        )
        output_limit = context.contract.budget.max_tool_output_chars
        bounded_records = _bound_records(records, max_chars=max(1000, output_limit // 4))
        errors = sum(item.severity == DiagnosticSeverity.ERROR for item in bounded_records)
        warnings = sum(item.severity == DiagnosticSeverity.WARNING for item in bounded_records)
        combined = "\n".join(part for part in (stdout_text, stderr_text) if part).strip()
        raw_preview = _bounded_text(
            combined or "<no diagnostic output>",
            min(max(500, output_limit // 8), 5_000),
        )
        structured = "\n".join(_render_record(item) for item in bounded_records)
        output = (
            f"diagnostic_id={command.id} exit_code={process.returncode} "
            f"errors={errors} warnings={warnings} findings={len(bounded_records)}\n"
            f"{structured or raw_preview}"
        )
        return ToolExecution(
            output=_bounded_text(output, max(1000, output_limit // 2)),
            data={
                "diagnostic_id": command.id,
                "parser": command.parser,
                "argv": [command.program, *command.args],
                "cwd": command.cwd,
                "exit_code": process.returncode,
                "errors": errors,
                "warnings": warnings,
                "truncated": (
                    len(records) >= arguments.max_results or len(bounded_records) < len(records)
                ),
                "diagnostics": [item.model_dump(mode="json") for item in bounded_records],
                "raw_preview": raw_preview,
            },
        )


def parse_diagnostics(
    command: DiagnosticCommand,
    *,
    stdout: str,
    stderr: str,
    workspace: Path,
    cwd: Path,
    max_results: int,
) -> list[DiagnosticRecord]:
    if command.parser == DiagnosticParser.ESLINT_JSON:
        return _parse_eslint_json(stdout, workspace=workspace, max_results=max_results)
    if command.parser == DiagnosticParser.TYPESCRIPT:
        return _parse_typescript(
            "\n".join(part for part in (stdout, stderr) if part),
            workspace=workspace,
            cwd=cwd,
            max_results=max_results,
        )
    return []


def _parse_eslint_json(
    output: str,
    *,
    workspace: Path,
    max_results: int,
) -> list[DiagnosticRecord]:
    try:
        payload = json.loads(output or "[]")
    except json.JSONDecodeError as exc:
        raise ToolError("ESLint diagnostic output is not valid JSON") from exc
    if not isinstance(payload, list):
        raise ToolError("ESLint diagnostic output must be a JSON array")
    records: list[DiagnosticRecord] = []
    for file_result in payload:
        if not isinstance(file_result, dict):
            continue
        relative = _safe_relative_path(file_result.get("filePath"), workspace=workspace)
        if relative is None:
            continue
        messages = file_result.get("messages")
        if not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, dict) or not isinstance(message.get("message"), str):
                continue
            records.append(
                DiagnosticRecord(
                    path=relative,
                    line=max(1, _integer(message.get("line"), default=1)),
                    column=max(1, _integer(message.get("column"), default=1)),
                    end_line=_optional_positive_integer(message.get("endLine")),
                    end_column=_optional_positive_integer(message.get("endColumn")),
                    severity=(
                        DiagnosticSeverity.ERROR
                        if _integer(message.get("severity")) >= 2
                        else DiagnosticSeverity.WARNING
                    ),
                    code=str(message.get("ruleId") or "")[:128],
                    message=message["message"][:1000],
                    source="eslint",
                )
            )
            if len(records) >= max_results:
                return records
    return records


def _parse_typescript(
    output: str,
    *,
    workspace: Path,
    cwd: Path,
    max_results: int,
) -> list[DiagnosticRecord]:
    records: list[DiagnosticRecord] = []
    for line in output.splitlines():
        match = _TSC_PAREN.match(line.strip()) or _TSC_COLON.match(line.strip())
        if match is None:
            continue
        relative = _safe_relative_path(match.group("path"), workspace=workspace, cwd=cwd)
        if relative is None:
            continue
        records.append(
            DiagnosticRecord(
                path=relative,
                line=int(match.group("line")),
                column=int(match.group("column")),
                severity=DiagnosticSeverity(match.group("severity").lower()),
                code=match.group("code").upper(),
                message=match.group("message")[:1000],
                source="typescript",
            )
        )
        if len(records) >= max_results:
            break
    return records


def _safe_relative_path(
    raw_path: Any,
    *,
    workspace: Path,
    cwd: Path | None = None,
) -> str | None:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    path = Path(raw_path.strip())
    candidate = path if path.is_absolute() else (cwd or workspace) / path
    try:
        return candidate.resolve().relative_to(workspace.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _render_record(record: DiagnosticRecord) -> str:
    code = f" {record.code}" if record.code else ""
    return f"{record.path}:{record.line}:{record.column}: {record.severity}{code}: {record.message}"


def _bound_records(
    records: list[DiagnosticRecord],
    *,
    max_chars: int,
) -> list[DiagnosticRecord]:
    selected: list[DiagnosticRecord] = []
    used = 0
    for record in records:
        size = len(record.model_dump_json())
        if selected and used + size > max_chars:
            break
        selected.append(record)
        used += size
    return selected


def _integer(value: Any, *, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _optional_positive_integer(value: Any) -> int | None:
    parsed = _integer(value)
    return parsed if parsed >= 1 else None


def _bounded_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    half = max(1, limit // 2)
    omitted = len(value) - (half * 2)
    return f"{value[:half]}\n... <{omitted} characters omitted> ...\n{value[-half:]}"


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
