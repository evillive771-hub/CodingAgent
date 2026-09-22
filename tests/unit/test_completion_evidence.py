import asyncio
from pathlib import Path

import pytest
from conftest import ScriptedModel

from aicoding_agent.agent.background import BackgroundTaskManager
from aicoding_agent.agent.gate import CompletionGate
from aicoding_agent.agent.goal import ModelGoalEvaluator
from aicoding_agent.agent.loop import AgentLoop
from aicoding_agent.domain.models import (
    CodingOutcome,
    ModelResponse,
    TaskContract,
    ToolCall,
    VerificationCommand,
)
from aicoding_agent.tools.base import ToolContext
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor


def setup_context(tmp_path: Path, *, verify: bool = True) -> ToolContext:
    return ToolContext(
        workspace=tmp_path,
        contract=TaskContract(
            task_id="evidence-regression",
            objective="Fix the component",
            acceptance_criteria=["The component returns the required value"],
            allowed_tools={"read_file", "create_file", "run_process", "start_background_process"},
            verification_commands=(
                [VerificationCommand(id="tests", program="python", args=["check.py"])]
                if verify else []
            ),
        ),
        background_manager=BackgroundTaskManager(),
    )


def submit(context: ToolContext) -> None:
    context.state["coding_outcome"] = CodingOutcome(
        summary="Implemented the component", changed_files=["component.py"]
    )


async def run_check(context: ToolContext, *, background: bool = False, process_limit: float = 5):
    return await ToolExecutor(build_builtin_registry()).execute(
        ToolCall(
            id="check",
            name="start_background_process" if background else "run_process",
            arguments={"program": "python", "args": ["check.py"], "timeout_seconds": process_limit},
        ),
        context,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exit", "timeout", "spawn"])
async def test_later_unsuccessful_verification_invalidates_success(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    context = setup_context(tmp_path)
    script = tmp_path / "check.py"
    script.write_text("pass\n", encoding="utf-8")
    assert (await run_check(context)).data["exit_code"] == 0
    submit(context)
    assert CompletionGate().evaluate(context.contract, context).action == "complete"

    if failure == "exit":
        script.write_text("raise SystemExit(1)\n", encoding="utf-8")
    elif failure == "timeout":
        script.write_text("import time; time.sleep(10)\n", encoding="utf-8")
    else:
        async def cannot_spawn(*args, **kwargs):
            raise OSError("executable unavailable")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", cannot_spawn)
    result = await run_check(context, process_limit=0.1 if failure == "timeout" else 5)
    assert not result.ok or result.data["exit_code"] != 0
    assert CompletionGate().evaluate(context.contract, context).action == "block"
    assert "verification:tests" not in AgentLoop._goal_input(
        context.contract, context
    ).available_evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("intervening", ["none", "mutation", "failed_retry"])
async def test_background_success_uses_start_generation(
    tmp_path: Path, intervening: str
) -> None:
    context = setup_context(tmp_path)
    script = tmp_path / "check.py"
    script.write_text("pass\n", encoding="utf-8")
    assert (await run_check(context, background=True)).ok
    manager = context.background_manager
    assert manager is not None
    # Finish execution but deliberately delay notification delivery.
    await asyncio.gather(*manager._tasks.values())
    if intervening == "mutation":
        result = await ToolExecutor(build_builtin_registry()).execute(
            ToolCall(id="edit", name="create_file", arguments={"path": "new.py", "content": "x=1"}),
            context,
        )
        assert result.ok and result.is_mutation
    elif intervening == "failed_retry":
        script.write_text("raise SystemExit(1)\n", encoding="utf-8")
        assert (await run_check(context)).data["exit_code"] == 1

    loop = AgentLoop(model=ScriptedModel([]), executor=ToolExecutor(build_builtin_registry()))
    await loop._inject_background_notifications(manager, [], run_id="test", tool_context=context)
    submit(context)
    decision = CompletionGate().evaluate(context.contract, context)
    assert decision.action == ("complete" if intervening == "none" else "block")


@pytest.mark.asyncio
async def test_fresh_bounded_code_is_visible_to_isolated_evaluator(tmp_path: Path) -> None:
    context = setup_context(tmp_path, verify=False)
    executor = ToolExecutor(build_builtin_registry())
    for name in ("first.py", "component.py"):
        created = await executor.execute(
            ToolCall(
                id="write", name="create_file",
                arguments={"path": name, "content": "VALUE = 42\n"},
            ),
            context,
        )
        assert created.ok
        submit(context)
        before_read = AgentLoop._goal_input(context.contract, context)
        assert not before_read.host_evidence_ids
        read = await executor.execute(
            ToolCall(id="read", name="read_file", arguments={"path": name}), context
        )
        assert read.ok

    goal = AgentLoop._goal_input(context.contract, context)
    assert len(goal.host_evidence_ids) == 1
    evidence_id = goal.host_evidence_ids[0]
    assert "VALUE = 42" in goal.available_evidence[evidence_id]
    model = ScriptedModel([
        ModelResponse(tool_calls=[ToolCall(
            id="judge", name="submit_goal_evaluation",
            arguments={"status": "complete", "reason": "Static code returns 42",
                       "evidence_refs": [evidence_id]},
        )])
    ])
    assert (await ModelGoalEvaluator(model).evaluate(goal)).evaluation.status == "complete"
    decision = CompletionGate().evaluate(context.contract, context)
    assert decision.action == "unverified"
    assert "cannot be marked complete" in decision.reason


@pytest.mark.asyncio
async def test_read_evidence_is_bounded_and_read_errors_are_not_evidence(tmp_path: Path) -> None:
    context = setup_context(tmp_path, verify=False)
    (tmp_path / "component.py").write_text("#" * 8000, encoding="utf-8")
    executor = ToolExecutor(build_builtin_registry())
    for index in range(12):
        await executor.execute(
            ToolCall(id=str(index), name="read_file", arguments={"path": "component.py"}), context
        )
    result = await executor.execute(
        ToolCall(id="missing", name="read_file", arguments={"path": "missing.py"}), context
    )
    assert not result.ok
    submit(context)
    goal = AgentLoop._goal_input(context.contract, context)
    assert len(goal.host_evidence_ids) == 8
    assert all(len(goal.available_evidence[key]) < 4300 for key in goal.host_evidence_ids)


def test_stale_verification_is_not_sent_to_evaluator(tmp_path: Path) -> None:
    context = setup_context(tmp_path)
    context.state["last_mutation_sequence"] = 5
    context.state["verification_success"] = {"tests": 3}
    submit(context)
    assert not AgentLoop._goal_input(context.contract, context).host_evidence_ids
