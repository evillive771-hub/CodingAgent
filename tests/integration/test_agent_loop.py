import asyncio
from pathlib import Path
from typing import Any

import pytest
from conftest import ScriptedModel

from aicoding_agent.agent.control import ControlIntent, SteeringInbox
from aicoding_agent.agent.hooks import AsyncHookBus, HookContext, HookEvent
from aicoding_agent.agent.loop import AgentLoop
from aicoding_agent.agent.plan import PlanItem, PlanManager, PlanStatus
from aicoding_agent.domain.models import ModelResponse, TaskContract, ToolCall, VerificationCommand
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor


@pytest.mark.asyncio
async def test_agent_edits_verifies_and_submits_result(tmp_path: Path) -> None:
    (tmp_path / "value.txt").write_text("bad\n", encoding="utf-8")
    (tmp_path / "verify_value.py").write_text(
        "from pathlib import Path\n"
        "value = Path('value.txt').read_text(encoding='utf-8')\n"
        "raise SystemExit(0 if value == 'good\\n' else 1)\n",
        encoding="utf-8",
    )
    contract = TaskContract(
        task_id="agent-integration",
        objective="Change value.txt from bad to good",
        acceptance_criteria=["The value test passes"],
        verification_commands=[
            VerificationCommand(id="tests", program="python", args=["verify_value.py"])
        ],
    )
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="edit-1",
                        name="apply_text_patch",
                        arguments={
                            "path": "value.txt",
                            "old_text": "bad\n",
                            "new_text": "good\n",
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="verify-1",
                        name="run_process",
                        arguments={"program": "python", "args": ["verify_value.py"]},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit-1",
                        name="submit_result",
                        arguments={
                            "summary": "Corrected the value.",
                            "changed_files": ["value.txt"],
                            "risk_notes": [],
                        },
                    )
                ]
            ),
            ModelResponse(content="Done."),
        ]
    )
    agent = AgentLoop(model=model, executor=ToolExecutor(build_builtin_registry()))

    result = await agent.run(contract, tmp_path, run_id="agent-run")

    assert result.status == "completed"
    assert result.outcome is not None
    assert result.outcome.changed_files == ["value.txt"]
    assert (tmp_path / "value.txt").read_text(encoding="utf-8") == "good\n"


@pytest.mark.asyncio
async def test_stop_gate_requires_verification_after_last_mutation(tmp_path: Path) -> None:
    (tmp_path / "value.txt").write_text("bad\n", encoding="utf-8")
    (tmp_path / "verify_value.py").write_text(
        "from pathlib import Path\n"
        "raise SystemExit(0 if Path('value.txt').read_text() in {'bad\\n', 'good\\n'} else 1)\n",
        encoding="utf-8",
    )
    verify_call = ToolCall(
        id="verify",
        name="run_process",
        arguments={"program": "python", "args": ["verify_value.py"]},
    )
    submit_call = ToolCall(
        id="submit",
        name="submit_result",
        arguments={"summary": "Changed value", "changed_files": ["value.txt"]},
    )
    contract = TaskContract(
        task_id="fresh-evidence",
        objective="Change value",
        acceptance_criteria=["Verification is fresh"],
        verification_commands=[
            VerificationCommand(id="verify", program="python", args=["verify_value.py"])
        ],
    )
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=[verify_call]),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="edit",
                        name="apply_text_patch",
                        arguments={
                            "path": "value.txt",
                            "old_text": "bad\n",
                            "new_text": "good\n",
                        },
                    )
                ]
            ),
            ModelResponse(tool_calls=[submit_call]),
            ModelResponse(content="Done too early"),
            ModelResponse(tool_calls=[verify_call.model_copy(update={"id": "verify-2"})]),
            ModelResponse(tool_calls=[submit_call.model_copy(update={"id": "submit-2"})]),
            ModelResponse(content="Done with fresh evidence"),
        ]
    )
    agent = AgentLoop(model=model, executor=ToolExecutor(build_builtin_registry()))

    result = await agent.run(contract, tmp_path, run_id="fresh-run")

    assert result.status == "completed"
    assert len(model.requests) == 7
    gate_feedback = model.requests[4][0][-1]["content"]
    assert "must be rerun after the last mutation" in gate_feedback


@pytest.mark.asyncio
async def test_agent_can_pause_for_user_clarification(tmp_path: Path) -> None:
    contract = TaskContract(
        task_id="clarification",
        objective="Implement the requested behavior",
        acceptance_criteria=["The ambiguous choice is clarified"],
        allowed_tools={"ask_user"},
    )
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="ask-1",
                        name="ask_user",
                        arguments={"question": "Should this support mobile layouts too?"},
                    )
                ]
            )
        ]
    )
    agent = AgentLoop(model=model, executor=ToolExecutor(build_builtin_registry()))

    result = await agent.run(contract, tmp_path, run_id="clarification-run")

    assert result.status == "waiting_for_user"
    assert result.final_text == "Should this support mobile layouts too?"


@pytest.mark.asyncio
async def test_read_only_conversation_turn_keeps_history_without_running_tests(
    tmp_path: Path,
) -> None:
    contract = TaskContract(
        task_id="read-only-chat",
        objective="Explain the existing code",
        acceptance_criteria=["Answer the user's question"],
        verification_commands=[
            VerificationCommand(id="tests", program="python", args=["missing-test.py"])
        ],
    )
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit-read-only",
                        name="submit_result",
                        arguments={
                            "summary": "The module validates user input.",
                            "changed_files": [],
                        },
                    )
                ]
            ),
            ModelResponse(content="It validates user input before saving."),
        ]
    )
    agent = AgentLoop(model=model, executor=ToolExecutor(build_builtin_registry()))

    result = await agent.run(
        contract,
        tmp_path,
        run_id="read-only-run",
        conversation_history=[
            {"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier answer"},
        ],
    )

    assert result.status == "completed"
    assert model.requests[0][0][1:3] == [
        {"role": "user", "content": "Earlier question"},
        {"role": "assistant", "content": "Earlier answer"},
    ]


@pytest.mark.asyncio
async def test_user_steering_supersedes_plan_and_forces_replanning(tmp_path: Path) -> None:
    plan = PlanManager(scope_id="steering")
    await plan.replace([PlanItem(content="Fix search", status=PlanStatus.IN_PROGRESS)])
    control = SteeringInbox(scope_id="steering")
    await control.submit(ControlIntent.STEER, "Fix login first")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="plan-1",
                        name="update_plan",
                        arguments={
                            "direction": "Fix login first",
                            "items": [{"content": "Fix login", "status": "completed"}],
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit-1",
                        name="submit_result",
                        arguments={"summary": "Login inspected", "changed_files": []},
                    )
                ]
            ),
            ModelResponse(content="Done."),
        ]
    )
    contract = TaskContract(
        task_id="steering-test",
        objective="Inspect the requested code",
        acceptance_criteria=["Address the latest direction"],
        allowed_tools={"update_plan", "submit_result"},
    )

    result = await AgentLoop(model=model, executor=ToolExecutor(build_builtin_registry())).run(
        contract, tmp_path, plan_manager=plan, control=control
    )

    assert result.status == "completed"
    assert plan.current is not None
    assert plan.current.version == 3
    assert plan.current.direction == "Fix login first"
    assert "<user_steering>Fix login first" in model.requests[0][0][-1]["content"]


@pytest.mark.asyncio
async def test_user_can_interrupt_at_safe_boundary(tmp_path: Path) -> None:
    control = SteeringInbox(scope_id="interrupt")
    await control.submit(ControlIntent.INTERRUPT)
    model = ScriptedModel([])
    contract = TaskContract(
        task_id="interrupt-test",
        objective="Stop safely",
        acceptance_criteria=["The run is interrupted"],
    )

    result = await AgentLoop(model=model, executor=ToolExecutor(build_builtin_registry())).run(
        contract, tmp_path, control=control
    )

    assert result.status == "interrupted"
    assert result.turns == 0


@pytest.mark.asyncio
async def test_steering_during_model_call_cancels_stale_tool_execution(
    tmp_path: Path,
) -> None:
    class DelayedFirstModel:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.requests: list[list[dict[str, Any]]] = []
            self.call_number = 0

        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
        ) -> ModelResponse:
            del tools
            self.requests.append(messages)
            self.call_number += 1
            if self.call_number == 1:
                self.started.set()
                await self.release.wait()
                return ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="stale-edit",
                            name="apply_text_patch",
                            arguments={
                                "path": "value.txt",
                                "old_text": "original\n",
                                "new_text": "stale direction\n",
                            },
                        )
                    ]
                )
            if self.call_number == 2:
                return ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="replan",
                            name="update_plan",
                            arguments={
                                "direction": "Inspect only",
                                "items": [
                                    {"content": "Inspect without editing", "status": "completed"}
                                ],
                            },
                        )
                    ]
                )
            if self.call_number == 3:
                return ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="submit",
                            name="submit_result",
                            arguments={"summary": "Inspected only", "changed_files": []},
                        )
                    ]
                )
            return ModelResponse(content="Done without changing the file.")

        async def aclose(self) -> None:
            return None

    (tmp_path / "value.txt").write_text("original\n", encoding="utf-8")
    plan = PlanManager(scope_id="mid-call-steering")
    await plan.replace([PlanItem(content="Edit the file", status=PlanStatus.IN_PROGRESS)])
    control = SteeringInbox(scope_id="mid-call-steering")
    model = DelayedFirstModel()
    contract = TaskContract(
        task_id="mid-call-steering",
        objective="Follow the current user direction",
        acceptance_criteria=["The newest direction wins"],
        allowed_tools={"apply_text_patch", "update_plan", "submit_result"},
    )

    run_task = asyncio.create_task(
        AgentLoop(model=model, executor=ToolExecutor(build_builtin_registry())).run(
            contract,
            tmp_path,
            plan_manager=plan,
            control=control,
        )
    )
    await model.started.wait()
    await control.submit(ControlIntent.STEER, "Inspect only")
    model.release.set()
    result = await run_task

    assert result.status == "completed"
    assert (tmp_path / "value.txt").read_text(encoding="utf-8") == "original\n"
    assert plan.current is not None
    assert plan.current.direction == "Inspect only"
    assert plan.current.version == 3
    assert any(
        "Cancelled before execution by a user control message" in str(message.get("content"))
        for message in model.requests[1]
    )
    assert any(
        "<user_steering>Inspect only</user_steering>" in str(message.get("content"))
        for message in model.requests[1]
    )


@pytest.mark.asyncio
async def test_steering_between_mutations_cancels_remaining_stale_tools(
    tmp_path: Path,
) -> None:
    (tmp_path / "first.txt").write_text("old\n", encoding="utf-8")
    (tmp_path / "second.txt").write_text("old\n", encoding="utf-8")
    plan = PlanManager(scope_id="between-tools")
    await plan.replace([PlanItem(content="Edit both files", status=PlanStatus.IN_PROGRESS)])
    control = SteeringInbox(scope_id="between-tools")
    hook_bus = AsyncHookBus()

    async def steer_after_first_tool(context: HookContext) -> None:
        if context.tool_call is not None and context.tool_call.id == "first-edit":
            await control.submit(ControlIntent.STEER, "Keep the second file unchanged")

    hook_bus.register(HookEvent.POST_TOOL_USE, steer_after_first_tool)
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="first-edit",
                        name="apply_text_patch",
                        arguments={
                            "path": "first.txt",
                            "old_text": "old\n",
                            "new_text": "new\n",
                        },
                    ),
                    ToolCall(
                        id="second-edit",
                        name="apply_text_patch",
                        arguments={
                            "path": "second.txt",
                            "old_text": "old\n",
                            "new_text": "stale\n",
                        },
                    ),
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="replan",
                        name="update_plan",
                        arguments={
                            "direction": "Keep the second file unchanged",
                            "items": [{"content": "Keep second.txt", "status": "completed"}],
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit",
                        name="submit_result",
                        arguments={
                            "summary": "Updated only first.txt",
                            "changed_files": ["first.txt"],
                        },
                    )
                ]
            ),
            ModelResponse(content="Done."),
        ]
    )
    contract = TaskContract(
        task_id="between-tools",
        objective="Follow steering at every safe boundary",
        acceptance_criteria=["Do not run stale mutations"],
        allowed_tools={"apply_text_patch", "update_plan", "submit_result"},
    )
    executor = ToolExecutor(build_builtin_registry(), hook_bus=hook_bus)

    result = await AgentLoop(
        model=model,
        executor=executor,
        hook_bus=hook_bus,
    ).run(contract, tmp_path, plan_manager=plan, control=control)

    assert result.status == "completed"
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "new\n"
    assert (tmp_path / "second.txt").read_text(encoding="utf-8") == "old\n"
    assert "Cancelled before execution" in str(model.requests[1][0][-2:])
