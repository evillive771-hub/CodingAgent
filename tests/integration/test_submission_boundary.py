from pathlib import Path

import pytest
from conftest import ScriptedModel

from aicoding_agent.agent.hooks import AsyncHookBus, HookEvent, HookResponse
from aicoding_agent.agent.loop import AgentLoop
from aicoding_agent.domain.models import ModelResponse, TaskContract, ToolCall, VerificationCommand
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("valid", "completed"),
        ("invalid", "budget_exhausted"),
        ("missing_verification", "budget_exhausted"),
        ("edit_after_submit", "budget_exhausted"),
        ("stop_hook", "budget_exhausted"),
    ],
)
async def test_submission_on_last_turn(tmp_path: Path, scenario: str, expected: str) -> None:
    events = []
    hook_turns = []

    class RecordingSink:
        async def emit(self, run_id, event_type, data):
            events.append(event_type)

    async def after_turn(context):
        hook_turns.append(context.turn)

    async def before_stop(context):
        return HookResponse(continue_message="More work is required")

    hooks = AsyncHookBus()
    hooks.register(HookEvent.AFTER_TURN, after_turn)
    if scenario == "stop_hook":
        hooks.register(HookEvent.BEFORE_STOP, before_stop)
    calls = [
        ToolCall(
            id="submit",
            name="submit_result",
            arguments={
                "summary": "" if scenario == "invalid" else "Finished",
                "changed_files": ["app.txt"] if scenario == "missing_verification" else [],
            },
        )
    ]
    if scenario == "edit_after_submit":
        calls.append(
            ToolCall(
                id="edit",
                name="create_file",
                arguments={"path": "app.txt", "content": "new code"},
            )
        )
    model = ScriptedModel([ModelResponse(tool_calls=calls)])
    contract = TaskContract(
        task_id="last-turn",
        objective="Finish within one model turn",
        acceptance_criteria=["Completion still requires host validation"],
        allowed_tools={"submit_result", "create_file"},
        budget={"max_turns": 1},
        verification_commands=(
            [VerificationCommand(id="tests", program="python", args=["test_app.py"])]
            if scenario == "missing_verification"
            else []
        ),
    )
    result = await AgentLoop(
        model=model,
        executor=ToolExecutor(build_builtin_registry()),
        hook_bus=hooks,
        event_sink=RecordingSink(),
    ).run(contract, tmp_path, run_id="boundary")

    assert result.status == expected
    assert len(model.requests) == 1
    assert hook_turns == [1]
    assert events.count(f"agent.{expected}") == 1
    assert not ("agent.completed" in events and "agent.budget_exhausted" in events)
    if expected == "completed":
        assert result.outcome is not None
        assert result.final_text == result.outcome.summary == "Finished"


@pytest.mark.asyncio
async def test_mutation_without_verification_is_submitted_unverified(tmp_path: Path) -> None:
    events: list[str] = []

    class RecordingSink:
        async def emit(self, run_id, event_type, data):
            events.append(event_type)

    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="edit",
                        name="create_file",
                        arguments={"path": "app.txt", "content": "new code"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit",
                        name="submit_result",
                        arguments={"summary": "Changed app", "changed_files": ["app.txt"]},
                    )
                ]
            ),
        ]
    )
    contract = TaskContract(
        task_id="unverified-boundary",
        objective="Change the application",
        acceptance_criteria=["The application is changed"],
        allowed_tools={"create_file", "submit_result"},
    )

    result = await AgentLoop(
        model=model,
        executor=ToolExecutor(build_builtin_registry()),
        event_sink=RecordingSink(),
    ).run(contract, tmp_path, run_id="unverified-boundary")

    assert result.status == "submitted_unverified"
    assert result.outcome is not None
    assert "agent.submitted_unverified" in events
    assert "agent.completed" not in events


@pytest.mark.asyncio
async def test_budget_warning_reserves_final_three_rounds(tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []

    class RecordingSink:
        async def emit(self, run_id, event_type, data):
            events.append((event_type, data))

    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit",
                        name="submit_result",
                        arguments={"summary": "Read-only answer", "changed_files": []},
                    )
                ]
            )
        ]
    )
    contract = TaskContract(
        task_id="budget-warning",
        objective="Answer within the remaining budget",
        acceptance_criteria=["A factual answer is submitted"],
        allowed_tools={"submit_result"},
        budget={"max_turns": 3},
    )

    result = await AgentLoop(
        model=model,
        executor=ToolExecutor(build_builtin_registry()),
        event_sink=RecordingSink(),
    ).run(contract, tmp_path, run_id="budget-warning")

    assert result.status == "completed"
    assert "<budget_warning>" in str(model.requests[0][0])
    warnings = [data for event, data in events if event == "agent.budget_warning"]
    assert warnings == [{"turn": 1, "remaining_turns": 3}]
