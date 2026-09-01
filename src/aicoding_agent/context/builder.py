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
- Do not modify tests merely to hide a product defect.
- After every code mutation, rerun every required verification command.
- Before finishing, call submit_result with a factual summary, changed files, and remaining risks.
- A submitted result is only a proposal. The host independently checks verification evidence.
- Git push, ticket transitions, test deployment, and production release are not your tools.
- Prior conversation messages provide context; the current task contract remains authoritative.
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
        for message in conversation_history or []:
            role = message.get("role")
            content = message.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                continue
            messages.append({"role": role, "content": content})
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

    recent_start = max(2, len(copied) - recent_messages)
    for index, message in enumerate(copied):
        if index >= recent_start or message.get("role") != "tool":
            continue
        content = str(message.get("content", ""))
        if len(content) > 2000:
            message["content"] = (
                content[:1000]
                + f"\n... <older tool output compacted: {len(content) - 2000} chars> ...\n"
                + content[-1000:]
            )
    if sum(_message_size(message) for message in copied) <= max_chars:
        return copied

    groups = _message_groups(copied[2:])
    while len(groups) > 1:
        candidate = (
            copied[:2]
            + [
                {
                    "role": "user",
                    "content": (
                        "<context_compacted>Older complete message groups were removed."
                        "</context_compacted>"
                    ),
                }
            ]
            + [message for group in groups for message in group]
        )
        if sum(_message_size(item) for item in candidate) <= max_chars:
            return candidate
        groups.pop(0)

    preserved = (
        copied[:2]
        + [
            {
                "role": "user",
                "content": (
                    "<context_compacted>Older conversation messages were removed. "
                    "The task contract remains authoritative; inspect the workspace and rerun "
                    "verification instead of assuming removed results.</context_compacted>"
                ),
            }
        ]
        + [message for group in groups for message in group]
    )
    return preserved


def _message_groups(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
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
