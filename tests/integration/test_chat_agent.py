from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ScriptedModel

from aicoding_agent.agent.loop import AgentLoop
from aicoding_agent.chat.session import ChatSession
from aicoding_agent.chat.store import ChatTranscriptStore
from aicoding_agent.domain.models import ModelResponse, TaskContract, ToolCall, VerificationCommand
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor


@pytest.mark.asyncio
async def test_conversational_agent_edits_then_answers_follow_up(tmp_path: Path) -> None:
    (tmp_path / "value.txt").write_text("bad\n", encoding="utf-8")
    (tmp_path / "verify.py").write_text(
        "from pathlib import Path\n"
        "raise SystemExit(0 if Path('value.txt').read_text() == 'good\\n' else 1)\n",
        encoding="utf-8",
    )
    profile = TaskContract(
        task_id="chat-profile",
        objective="Interactive test profile",
        acceptance_criteria=["Configured checks pass after mutations"],
        allowed_tools={
            "read_file",
            "apply_text_patch",
            "run_process",
            "submit_result",
        },
        verification_commands=[
            VerificationCommand(id="tests", program="python", args=["verify.py"])
        ],
    )
    model = ScriptedModel(
        [
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
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="verify",
                        name="run_process",
                        arguments={"program": "python", "args": ["verify.py"]},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit-change",
                        name="submit_result",
                        arguments={
                            "summary": "Changed the value to good.",
                            "changed_files": ["value.txt"],
                        },
                    )
                ]
            ),
            ModelResponse(content="Updated value.txt and the verification passed."),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="read",
                        name="read_file",
                        arguments={"path": "value.txt"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit-answer",
                        name="submit_result",
                        arguments={"summary": "The current value is good.", "changed_files": []},
                    )
                ]
            ),
            ModelResponse(content="The current value is `good`."),
        ]
    )
    agent = AgentLoop(model=model, executor=ToolExecutor(build_builtin_registry()))
    session = ChatSession(
        agent=agent,
        workspace=tmp_path,
        profile=profile,
        store=ChatTranscriptStore(tmp_path / ".aicoding-agent" / "chat"),
        session_id="integration",
    )

    first = await session.send("Change value.txt from bad to good")
    second = await session.send("What is the value now?")

    assert first.status == "completed"
    assert second.status == "completed"
    assert second.assistant_message == "The current value is `good`."
    assert (tmp_path / "value.txt").read_text(encoding="utf-8") == "good\n"
    second_turn_messages = model.requests[4][0]
    assert second_turn_messages[1:3] == [
        {"role": "user", "content": "Change value.txt from bad to good"},
        {
            "role": "assistant",
            "content": "Updated value.txt and the verification passed.",
        },
    ]
