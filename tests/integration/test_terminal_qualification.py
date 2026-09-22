from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from conftest import ScriptedModel

from aicoding_agent.agent.loop import AgentLoop
from aicoding_agent.domain.models import ModelResponse, TaskContract, ToolCall, VerificationCommand
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor

TERMINAL_EVENTS = {
    "agent.completed",
    "agent.submitted_unverified",
    "agent.failed",
    "agent.budget_exhausted",
    "agent.waiting_for_user",
}


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append(
            {
                "run_id": run_id,
                "event_type": event_type,
                "data": data,
                "sequence": len(self.events) + 1,
            }
        )


def response(call_id: str, name: str, arguments: dict) -> ModelResponse:
    return ModelResponse(tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)])


def fresh_verification(events: list[dict]) -> bool:
    last_mutation = 0
    last_verification = 0
    for event in events:
        if event["event_type"] != "tool.finished":
            continue
        data = event["data"]
        operation = data.get("data") or {}
        sequence = int(operation.get("operation_sequence") or 0)
        if data.get("ok") is True and data.get("is_mutation") is True:
            last_mutation = max(last_mutation, sequence)
        if (
            data.get("ok") is True
            and operation.get("verification_id")
            and operation.get("exit_code") == 0
        ):
            last_verification = max(last_verification, sequence)
    return last_mutation > 0 and last_verification > last_mutation


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", range(3))
@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("code_only", "submitted_unverified"),
        ("verified", "completed"),
        ("verification_failed", "budget_exhausted"),
        ("stale_verification", "budget_exhausted"),
    ],
)
async def test_terminal_semantics_qualification(
    tmp_path: Path,
    scenario: str,
    expected: str,
    variant: int,
) -> None:
    """Twelve deterministic cases qualify terminal routing before benchmark scoring."""
    await asyncio.to_thread(subprocess.run, ["git", "init", "-q"], cwd=tmp_path, check=True)
    filename = f"app-{variant}.txt"
    verification_script = tmp_path / f"verify-{variant}.py"
    verification_script.write_text(
        "from pathlib import Path\n"
        f"raise SystemExit(0 if Path({filename!r}).read_text() == 'good\\n' else 1)\n",
        encoding="utf-8",
    )
    verify = VerificationCommand(
        id=f"public-tests-{variant}",
        program="python",
        args=[verification_script.name],
    )
    responses = [
        response(
            f"edit-{variant}",
            "create_file",
            {"path": filename, "content": "good\n"},
        )
    ]
    verification_commands: list[VerificationCommand] = []
    if scenario != "code_only":
        verification_commands = [verify]
        if scenario == "verification_failed":
            verification_script.write_text("raise SystemExit(1)\n", encoding="utf-8")
        responses.append(
            response(
                f"verify-{variant}",
                "run_process",
                {"program": "python", "args": [verification_script.name]},
            )
        )
    if scenario == "stale_verification":
        responses.append(
            response(
                f"edit-again-{variant}",
                "apply_text_patch",
                {"path": filename, "old_text": "good\n", "new_text": "changed\n"},
            )
        )
    responses.append(
        response(
            f"submit-{variant}",
            "submit_result",
            {"summary": f"scenario={scenario}", "changed_files": [filename]},
        )
    )
    sink = RecordingSink()
    contract = TaskContract(
        task_id=f"qualification:{scenario}:{variant}",
        objective="Exercise terminal evidence routing",
        acceptance_criteria=["The terminal status matches verification freshness"],
        allowed_tools={"create_file", "apply_text_patch", "run_process", "submit_result"},
        verification_commands=verification_commands,
        budget={"max_turns": len(responses)},
    )
    result = await AgentLoop(
        model=ScriptedModel(responses),
        executor=ToolExecutor(build_builtin_registry()),
        event_sink=sink,
    ).run(contract, tmp_path, run_id=f"qualification-{scenario}-{variant}")

    assert result.status == expected
    terminal = [event for event in sink.events if event["event_type"] in TERMINAL_EVENTS]
    assert len(terminal) == 1
    assert terminal[0]["event_type"] == f"agent.{expected}"
    assert any(event["event_type"] == "tool.finished" for event in sink.events)
    assert json.loads(json.dumps(sink.events)) == sink.events
    if result.status == "completed":
        assert fresh_verification(sink.events)
