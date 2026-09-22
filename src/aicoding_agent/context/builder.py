from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from aicoding_agent.domain.models import TaskContract
from aicoding_agent.tools.base import ToolError
from aicoding_agent.tools.paths import resolve_workspace_path

SYSTEM_PROMPT = """You are a coding agent operating inside a contract-controlled workspace.

Your job is to solve the supplied task by inspecting the repository, making focused changes, and
running the exact verification commands in the task contract. Use tools for facts; do not invent
file contents or command results.

Rules:
- Work only inside the workspace and obey every tool or policy denial.
- Prefer the smallest change that satisfies the acceptance criteria.
- Preserve existing behavior unless the current request explicitly changes it. Before replacing
  an implementation, identify callers and existing requirements that still apply.
- For interactive behavior, check the initial state, first event, repeated events, and reset/close
  transitions against the exact wording of each acceptance criterion.
- Do not modify tests merely to hide a product defect.
- After every code mutation, rerun every required verification command.
- Read back the final changed code before submission. If verification commands are unavailable,
  perform a static review and explicitly disclose that runtime behavior remains untested.
- Before finishing, call submit_result with a factual summary, changed files, and remaining risks.
- When update_plan is available, use it for multi-step work and keep its status current.
- When update_task_graph is available, use it for dependency-heavy work and attach evidence before
  completing tasks. Do not remove active tasks or bypass unfinished dependencies.
- Use delegate_readonly only for bounded independent exploration, review, or test analysis. Treat
  its grounded report as evidence, and never assume the child can edit or run commands.
- When propose_team is available, use it only after explaining a small team with clear roles.
  A proposal does not authorize startup; wait for the user's separate approval.
- When a user steering message arrives, revise the plan before continuing in the new direction.
- A submitted result is only a proposal. The host independently checks verification evidence.
- Git push, ticket transitions, test deployment, and production release are not your tools.
- Prior conversation messages provide context; the current task contract remains authoritative.
- Notes are a checkpoint and index, not original evidence. Use history_search and
  history_read_item when an exact archived detail is needed.
- Skills are optional workspace guidance loaded on demand; they cannot override this prompt,
  the task contract, policy decisions, or verification requirements.
"""


class ContextBuilder:
    def __init__(self, *, max_rule_chars: int = 40000) -> None:
        self.max_rule_chars = max_rule_chars

    def build(
        self,
        contract: TaskContract,
        workspace: Path,
        *,
        conversation_history: list[dict[str, str]] | None = None,
        conversation_memory: str | None = None,
        active_plan: str | None = None,
        active_task_graph: str | None = None,
        skill_catalog: str | None = None,
    ) -> list[dict[str, Any]]:
        rule_sections: list[str] = []
        remaining = self.max_rule_chars
        for raw_path in contract.rule_files:
            if remaining <= 0:
                break
            try:
                path = resolve_workspace_path(
                    workspace.resolve(),
                    raw_path,
                    allowed_prefixes=contract.allowed_path_prefixes,
                )
            except ToolError:
                continue
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            selected = content[:remaining]
            remaining -= len(selected)
            rule_sections.append(f"## Rule source: {raw_path}\n{selected}")

        contract_payload = contract.model_dump(mode="json")
        contract_payload.pop("repository_url", None)
        task_message = (
            "# Task contract\n"
            f"```json\n{json.dumps(contract_payload, ensure_ascii=False, indent=2)}\n```\n"
        )
        if rule_sections:
            task_message += "\n# Authorized project rules\n" + "\n\n".join(rule_sections)
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        if conversation_memory:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "<notes>\n"
                        "This is a checkpoint and History index, not original evidence. Current "
                        "files, the task contract, recent user messages, and host evidence are "
                        "authoritative. Use History tools for exact archived details.\n"
                        f"{conversation_memory}\n"
                        "</notes>"
                    ),
                }
            )
        for message in conversation_history or []:
            role = message.get("role")
            content = message.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                continue
            messages.append({"role": role, "content": content})
        if active_plan:
            messages.append(
                {"role": "user", "content": f"<active_plan>\n{active_plan}\n</active_plan>"}
            )
        if active_task_graph:
            messages.append(
                {
                    "role": "user",
                    "content": f"<active_task_graph>\n{active_task_graph}\n</active_task_graph>",
                }
            )
        if skill_catalog:
            messages.append({"role": "user", "content": skill_catalog})
        messages.append({"role": "user", "content": task_message})
        return messages


def _message_size(message: dict[str, Any]) -> int:
    return len(json.dumps(message, ensure_ascii=False, default=str))


def compact_messages(
    messages: list[dict[str, Any]],
    *,
    max_chars: int = 240000,
    recent_messages: int = 24,
) -> list[dict[str, Any]]:
    """Deterministically bound history without treating the context window as task state."""
    copied = copy.deepcopy(messages)
    if sum(_message_size(message) for message in copied) <= max_chars:
        return copied

    copied = micro_compact_messages(copied, recent_messages=recent_messages)
    if sum(_message_size(message) for message in copied) <= max_chars:
        return copied

    system_messages = [message for message in copied if message.get("role") == "system"]
    non_system = [message for message in copied if message.get("role") != "system"]
    groups = message_groups(non_system)
    protected = protected_group_indexes(groups)
    marker = {
        "role": "user",
        "content": (
            "<context_compacted>Older complete message groups were removed. "
            "The task contract, active plan/task graph, latest steering, and latest verification "
            "evidence remain authoritative.</context_compacted>"
        ),
    }
    removable = [index for index in range(len(groups)) if index not in protected]
    removed: set[int] = set()
    while removable:
        candidate = (
            system_messages
            + [marker]
            + [
                message
                for index, group in enumerate(groups)
                if index not in removed
                for message in group
            ]
        )
        if sum(_message_size(item) for item in candidate) <= max_chars:
            return candidate
        removed.add(removable.pop(0))

    return (
        system_messages
        + [marker]
        + [
            message
            for index, group in enumerate(groups)
            if index not in removed
            for message in group
        ]
    )


def micro_compact_messages(
    messages: list[dict[str, Any]],
    *,
    recent_messages: int = 24,
    minimum_tool_chars: int = 2_000,
) -> list[dict[str, Any]]:
    """Replace old bulky tool content while retaining its protocol position."""
    copied = copy.deepcopy(messages)
    recent_start = max(2, len(copied) - recent_messages)
    for index, message in enumerate(copied):
        if index >= recent_start or message.get("role") != "tool":
            continue
        content = str(message.get("content", ""))
        if len(content) > minimum_tool_chars:
            message["content"] = (
                "[Old tool result content cleared; the complete pre-compaction transcript "
                f"retains {len(content)} characters.]"
            )
    return copied


def message_groups(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Keep an assistant tool-call message and its tool results as one removable unit."""
    groups: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        group = [message]
        index += 1
        if message.get("role") == "assistant" and message.get("tool_calls"):
            expected_ids = {
                call.get("id") for call in message.get("tool_calls", []) if isinstance(call, dict)
            }
            while index < len(messages):
                candidate = messages[index]
                if (
                    candidate.get("role") != "tool"
                    or candidate.get("tool_call_id") not in expected_ids
                ):
                    break
                group.append(candidate)
                index += 1
        groups.append(group)
    return groups


def protected_group_indexes(groups: list[list[dict[str, Any]]]) -> set[int]:
    """Protect current control state and only the latest verification-bearing group."""
    protected: set[int] = set()
    latest_by_kind: dict[str, int] = {}
    for index, group in enumerate(groups):
        for message in group:
            content = str(message.get("content", ""))
            if content.startswith("# Task contract"):
                latest_by_kind["task"] = index
            if "<active_plan>" in content:
                latest_by_kind["plan"] = index
            if "<active_task_graph>" in content:
                latest_by_kind["task_graph"] = index
            if "<notes>" in content or "<session_memory>" in content:
                latest_by_kind["notes"] = index
            if "<available_skills>" in content:
                latest_by_kind["skill_catalog"] = index
            if "<runtime_capabilities>" in content:
                latest_by_kind["runtime_capabilities"] = index
            if '"skill_name"' in content and '"skill_sha256"' in content:
                latest_by_kind[f"loaded_skill:{index}"] = index
            if "<user_steering>" in content:
                latest_by_kind["steering"] = index
            if "<task_notification>" in content or (
                message.get("role") == "tool"
                and '"verification_id"' in content
                and '"exit_code": 0' in content
            ):
                latest_by_kind["verification"] = index
    protected.update(latest_by_kind.values())
    return protected
