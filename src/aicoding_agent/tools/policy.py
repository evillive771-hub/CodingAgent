from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from aicoding_agent.domain.models import TaskContract, ToolRisk
from aicoding_agent.tools.base import BaseTool, ToolError
from aicoding_agent.tools.paths import resolve_workspace_path


class PolicyBehavior(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    behavior: PolicyBehavior
    reason: str = ""


READ_ONLY_GIT_SUBCOMMANDS = {
    "diff",
    "grep",
    "log",
    "ls-files",
    "rev-parse",
    "show",
    "status",
}


class PolicyEngine:
    """Host-owned policy. Unattended ASK decisions fail closed in ToolExecutor."""

    def evaluate(
        self,
        tool: BaseTool[Any],
        arguments: BaseModel,
        *,
        workspace: Path,
        contract: TaskContract,
    ) -> PolicyDecision:
        if tool.name not in contract.allowed_tools:
            return PolicyDecision(
                behavior=PolicyBehavior.DENY,
                reason=f"Tool {tool.name!r} is not allowed by the task contract",
            )

        argument_data = arguments.model_dump()
        raw_path = argument_data.get("path")
        if isinstance(raw_path, str):
            try:
                resolve_workspace_path(
                    workspace,
                    raw_path,
                    allowed_prefixes=contract.allowed_path_prefixes,
                    for_write=tool.risk == ToolRisk.WRITE,
                )
            except ToolError as exc:
                return PolicyDecision(behavior=PolicyBehavior.DENY, reason=str(exc))

        if tool.name in {"run_process", "start_background_process"}:
            program = str(argument_data.get("program", "")).lower()
            program_name = Path(program).stem
            args = tuple(str(item) for item in argument_data.get("args", []))
            cwd = str(argument_data.get("cwd", "."))
            if program_name not in {item.lower() for item in contract.allowed_commands}:
                return PolicyDecision(
                    behavior=PolicyBehavior.DENY,
                    reason=f"Executable {program_name!r} is not allowed by the task contract",
                )
            exact_verification = any(
                program_name == Path(command.program.lower()).stem
                and args == tuple(command.args)
                and cwd == command.cwd
                for command in contract.verification_commands
            )
            safe_git_read = (
                program_name == "git" and bool(args) and args[0] in READ_ONLY_GIT_SUBCOMMANDS
            )
            if tool.name == "start_background_process" and not exact_verification:
                return PolicyDecision(
                    behavior=PolicyBehavior.DENY,
                    reason="Background processes must be exact verification commands",
                )
            if not exact_verification and not safe_git_read:
                return PolicyDecision(
                    behavior=PolicyBehavior.DENY,
                    reason=(
                        "Process is not an exact verification command or an approved "
                        "read-only Git command"
                    ),
                )

        if tool.name == "get_diagnostics":
            diagnostic_id = str(argument_data.get("diagnostic_id", ""))
            command = next(
                (item for item in contract.diagnostic_commands if item.id == diagnostic_id),
                None,
            )
            if command is None:
                return PolicyDecision(
                    behavior=PolicyBehavior.DENY,
                    reason=f"Unknown task-contract diagnostic ID: {diagnostic_id}",
                )
            program_name = Path(command.program.lower()).stem
            if program_name not in {item.lower() for item in contract.allowed_commands}:
                return PolicyDecision(
                    behavior=PolicyBehavior.DENY,
                    reason=f"Diagnostic executable {program_name!r} is not allowed by the contract",
                )
            try:
                resolve_workspace_path(
                    workspace,
                    command.cwd,
                    allowed_prefixes=contract.allowed_path_prefixes,
                )
            except ToolError as exc:
                return PolicyDecision(behavior=PolicyBehavior.DENY, reason=str(exc))

        if tool.name == "propose_team":
            # The tool may only create a local proposal. Team startup remains a separate,
            # host-owned approval transition enforced by TeamLaunchManager and Coordinator.
            return PolicyDecision(behavior=PolicyBehavior.ALLOW)

        if tool.risk == ToolRisk.EXTERNAL:
            return PolicyDecision(
                behavior=PolicyBehavior.ASK,
                reason="External side effects require an explicit workflow approval",
            )
        return PolicyDecision(behavior=PolicyBehavior.ALLOW)
