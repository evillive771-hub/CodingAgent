from pathlib import Path

import pytest
from conftest import ScriptedModel

from aicoding_agent.agent.loop import AgentLoop
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
