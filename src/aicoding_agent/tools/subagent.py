from __future__ import annotations

import asyncio
import json

from pydantic import Field

from aicoding_agent.domain.models import (
    SubagentFinding,
    SubagentFindingInput,
    SubagentReport,
    SubagentRole,
    SubagentStatus,
    ToolRisk,
)
from aicoding_agent.tools.base import BaseTool, ToolContext, ToolError, ToolExecution, ToolInput
from aicoding_agent.tools.paths import resolve_workspace_path


class SubmitSubagentReportInput(ToolInput):
    status: SubagentStatus = SubagentStatus.COMPLETED
    summary: str = Field(min_length=1, max_length=5000)
    findings: list[SubagentFindingInput] = Field(default_factory=list, max_length=50)
    uncertainties: list[str] = Field(default_factory=list, max_length=50)


class SubmitSubagentReportTool(BaseTool[SubmitSubagentReportInput]):
    name = "submit_subagent_report"
    description = (
        "Submit the child Agent's final read-only report. Every finding path and line is "
        "re-read and grounded by the host."
    )
    input_model = SubmitSubagentReportInput
    risk = ToolRisk.READ

    async def run(
        self,
        arguments: SubmitSubagentReportInput,
        context: ToolContext,
    ) -> ToolExecution:
        role = context.state.get("subagent_role")
        if not isinstance(role, SubagentRole):
            raise ToolError("submit_subagent_report is only available inside a child Agent")
        grounded: list[SubagentFinding] = []
        for finding in arguments.findings:
            path = resolve_workspace_path(
                context.workspace,
                finding.path,
                allowed_prefixes=context.contract.allowed_path_prefixes,
            )
            if not path.is_file():
                raise ToolError(f"Subagent finding file does not exist: {finding.path}")
            try:
                lines = await asyncio.to_thread(path.read_text, encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise ToolError(f"Could not ground subagent finding: {finding.path}") from exc
            split_lines = lines.splitlines()
            if finding.line > len(split_lines):
                raise ToolError(
                    f"Subagent finding line is outside the file: {finding.path}:{finding.line}"
                )
            grounded.append(
                SubagentFinding(
                    **finding.model_dump(),
                    snippet=split_lines[finding.line - 1][:1000],
                )
            )
        report = SubagentReport(
            role=role,
            status=arguments.status,
            summary=arguments.summary,
            findings=grounded,
            uncertainties=arguments.uncertainties,
        )
        context.state["subagent_report"] = report
        return ToolExecution(
            output="Read-only subagent report recorded.",
            data={"report": report.model_dump(mode="json")},
        )


class DelegateReadOnlyInput(ToolInput):
    role: SubagentRole = SubagentRole.EXPLORE
    task: str = Field(min_length=1, max_length=5000)
    context_paths: list[str] = Field(default_factory=list, max_length=30)
    max_turns: int = Field(default=8, ge=1, le=12)


class DelegateReadOnlyTool(BaseTool[DelegateReadOnlyInput]):
    name = "delegate_readonly"
    description = (
        "Delegate bounded repository exploration, review, or test analysis to an isolated "
        "read-only child Agent. The child cannot edit, run processes, or delegate recursively."
    )
    input_model = DelegateReadOnlyInput
    risk = ToolRisk.READ
    concurrency_safe = True

    async def run(
        self,
        arguments: DelegateReadOnlyInput,
        context: ToolContext,
    ) -> ToolExecution:
        if context.subagent_manager is None:
            raise ToolError("This Agent run has no read-only subagent manager")
        report = await context.subagent_manager.run(
            role=arguments.role,
            task=arguments.task,
            context_paths=arguments.context_paths,
            max_turns=arguments.max_turns,
            parent_context=context,
        )
        limit = context.contract.budget.max_tool_output_chars
        serialized, output, truncated = _bounded_report(report, limit=limit)
        return ToolExecution(
            output=output,
            data={
                "role": report.role,
                "status": report.status,
                "finding_count": len(report.findings),
                "returned_findings": len(serialized["findings"]),
                "truncated": truncated,
                "turns": report.turns,
                "tool_calls": report.tool_calls,
            },
        )


def _bounded_report(
    report: SubagentReport,
    *,
    limit: int,
) -> tuple[dict[str, object], str, bool]:
    serialized = report.model_dump(mode="json")
    original_findings = list(serialized["findings"])
    output = json.dumps(serialized, ensure_ascii=False, indent=2)
    while len(output) > limit and serialized["findings"]:
        serialized["findings"].pop()
        output = json.dumps(serialized, ensure_ascii=False, indent=2)
    if len(output) > limit:
        serialized["summary"] = str(serialized["summary"])[:1000]
        serialized["uncertainties"] = [str(item)[:500] for item in serialized["uncertainties"][:5]]
        output = json.dumps(serialized, ensure_ascii=False, indent=2)
    if len(output) > limit:
        serialized = {
            "role": str(report.role),
            "status": str(report.status),
            "summary": report.summary[: max(1, limit // 2)],
            "findings": [],
            "uncertainties": ["Report details exceeded the parent output budget"],
            "turns": report.turns,
            "tool_calls": report.tool_calls,
        }
        output = json.dumps(serialized, ensure_ascii=False, separators=(",", ":"))
    return serialized, output[:limit], len(serialized["findings"]) < len(original_findings)
