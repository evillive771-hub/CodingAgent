"""Read-only runtime facts for the model; never a substitute for executed tests."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from aicoding_agent.domain.models import TaskContract
from aicoding_agent.tools.base import ToolError
from aicoding_agent.tools.paths import resolve_workspace_path


async def inspect_runtime(
    workspace: Path, contract: TaskContract, *, registered_tools: set[str]
) -> dict[str, Any]:
    """Inspect the local execution environment without running project code or installing it."""
    return await asyncio.to_thread(_inspect_runtime, workspace, contract, registered_tools)


def _inspect_runtime(
    workspace: Path, contract: TaskContract, registered_tools: set[str]
) -> dict[str, Any]:
    tools = contract.allowed_tools & registered_tools
    commands: list[dict[str, Any]] = []
    for command in contract.verification_commands[:32]:
        blockers: list[str] = []
        if "run_process" not in tools and "start_background_process" not in tools:
            blockers.append("no_process_tool")
        if Path(command.program.lower()).stem not in {
            item.lower() for item in contract.allowed_commands
        }:
            blockers.append("executable_not_allowed")
        try:
            if shutil.which(command.program) is None:
                blockers.append("executable_not_on_path")
        except (OSError, ValueError):
            blockers.append("executable_inspection_error")
        try:
            cwd = resolve_workspace_path(
                workspace, command.cwd, allowed_prefixes=contract.allowed_path_prefixes
            )
            if not cwd.is_dir():
                blockers.append("cwd_missing")
        except ToolError:
            blockers.append("cwd_not_allowed")
        except (OSError, ValueError, RuntimeError):
            blockers.append("cwd_inspection_error")
        commands.append({"id": command.id, "blockers": blockers})
    return {
        "execution_environment": "local_agent_process",
        "workspace_root_git_metadata": _path_present(workspace / ".git", directory=False),
        "workspace_root_node_modules": _path_present(workspace / "node_modules", directory=True),
        "process_tools": sorted(tools & {"run_process", "start_background_process"}),
        "verification_command_count": len(contract.verification_commands),
        "browser_verifications": [
            {"id": item.id, "required": item.required, "suite_ref": item.suite_ref}
            for item in contract.browser_verifications[:32]
        ],
        "browser_tool_available": "run_playwright" in tools,
        "verification_preflight": commands,
        "verification_preflight_truncated": len(contract.verification_commands) > len(commands),
    }


def _path_present(path: Path, *, directory: bool) -> bool | None:
    try:
        return path.is_dir() if directory else path.exists()
    except (OSError, ValueError, RuntimeError):
        return None


def render_runtime(facts: dict[str, Any]) -> str:
    return (
        "<runtime_capabilities>\n"
        "Host observations at run start, not test results. Process tools execute in the local "
        "agent workspace, not a separate evaluator/container. Dependencies installed elsewhere "
        "are not automatically available here. Missing root .git or node_modules is only a "
        "filesystem observation; nested/workspace dependencies may still exist. Null means "
        "inspection was unavailable. Process tools permit only exact contract verification "
        "commands and approved read-only Git commands. The get_diagnostics tool separately "
        "permits configured diagnostic_commands. "
        "run_playwright executes only configured fixed public browser suites; assertion "
        "failures are feedback for further coding, not successful verification. An empty "
        "blocker list does not prove a command or its dependencies will work; run it to verify. "
        "If no verification is configured, report changes as unverified. Do not invent test "
        "success or repeatedly try commands blocked by policy.\n"
        + json.dumps(facts, ensure_ascii=False, sort_keys=True)
        + "\n</runtime_capabilities>"
    )
