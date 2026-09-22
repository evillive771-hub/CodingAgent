from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ScriptedModel

from aicoding_agent.agent.loop import AgentLoop
from aicoding_agent.agent.memory import MemoryManager, MemoryStore
from aicoding_agent.agent.mutation_journal import MutationJournal
from aicoding_agent.chat.journal import JournalEventType, SessionMessageJournal
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
                content="Updated value.txt and the verification passed.",
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
                content="The current value is `good`.",
                tool_calls=[
                    ToolCall(
                        id="submit-answer",
                        name="submit_result",
                        arguments={"summary": "The current value is good.", "changed_files": []},
                    )
                ]
            ),
        ]
    )
    agent = AgentLoop(model=model, executor=ToolExecutor(build_builtin_registry()))
    message_journal = SessionMessageJournal(tmp_path / ".aicoding-agent" / "journal")
    session = ChatSession(
        agent=agent,
        workspace=tmp_path,
        profile=profile,
        store=ChatTranscriptStore(tmp_path / ".aicoding-agent" / "chat"),
        session_id="integration",
        message_journal=message_journal,
    )

    first = await session.send("Change value.txt from bad to good")
    second = await session.send("What is the value now?")

    assert first.status == "completed"
    assert second.status == "completed"
    assert second.assistant_message == "The current value is `good`."
    assert (tmp_path / "value.txt").read_text(encoding="utf-8") == "good\n"
    second_turn_messages = model.requests[3][0]
    assert second_turn_messages[1:3] == [
        {"role": "user", "content": "Change value.txt from bad to good"},
        {
            "role": "assistant",
            "content": "Updated value.txt and the verification passed.",
        },
    ]
    journal_entries = await message_journal.load("integration")
    message_entries = [
        entry for entry in journal_entries if entry.event_type == JournalEventType.MESSAGE
    ]
    assert any(entry.payload["message"].get("tool_call_id") == "edit" for entry in message_entries)
    assert sum(entry.event_type == JournalEventType.RUN_FINISHED for entry in journal_entries) == 2


@pytest.mark.asyncio
async def test_real_agent_file_writes_can_be_rewound_to_a_fork(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"
    target.write_text("zero\n", encoding="utf-8")
    profile = TaskContract(
        task_id="rewind-profile",
        objective="Interactive rewind profile",
        acceptance_criteria=["Agent file writes are journaled"],
        allowed_tools={"apply_text_patch", "submit_result"},
    )
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="first-edit",
                        name="apply_text_patch",
                        arguments={
                            "path": "value.txt",
                            "old_text": "zero\n",
                            "new_text": "one\n",
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="first-submit",
                        name="submit_result",
                        arguments={"summary": "Set one", "changed_files": ["value.txt"]},
                    )
                ]
            ),
            ModelResponse(content="Set value to one."),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="second-edit",
                        name="apply_text_patch",
                        arguments={
                            "path": "value.txt",
                            "old_text": "one\n",
                            "new_text": "two\n",
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="second-submit",
                        name="submit_result",
                        arguments={"summary": "Set two", "changed_files": ["value.txt"]},
                    )
                ]
            ),
            ModelResponse(content="Set value to two."),
        ]
    )
    journal = MutationJournal(tmp_path / ".aicoding-agent" / "mutations")
    agent = AgentLoop(
        model=model,
        executor=ToolExecutor(build_builtin_registry(), mutation_journal=journal),
    )
    store = ChatTranscriptStore(tmp_path / ".aicoding-agent" / "chat")
    session = ChatSession(
        agent=agent,
        workspace=tmp_path,
        profile=profile,
        store=store,
        session_id="source",
        mutation_journal=journal,
    )

    assert (await session.send("Set the value to one")).status == "submitted_unverified"
    assert (await session.send("Now set the value to two")).status == "submitted_unverified"
    cloned, recovery = await session.rewind("one-branch", through_turn=1)

    assert target.read_text(encoding="utf-8") == "one\n"
    assert len(cloned) == 1
    assert recovery.restored_files == ["value.txt"]
    assert len(await store.load("source")) == 2
    assert len(await store.load("one-branch")) == 1


@pytest.mark.asyncio
async def test_agent_memory_proposal_is_only_recalled_after_user_approval(
    tmp_path: Path,
) -> None:
    profile = TaskContract(
        task_id="memory-profile",
        objective="Interactive memory profile",
        acceptance_criteria=["Only approved memory is recalled"],
        allowed_tools={"propose_memory", "recall_memory", "submit_result"},
    )
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="proposal",
                        name="propose_memory",
                        arguments={
                            "topic": "test-order",
                            "content": "Run focused tests before the full suite.",
                            "ttl_days": 30,
                        },
                    )
                ]
            ),
            ModelResponse(
                content="I proposed the test-order memory for your approval.",
                tool_calls=[
                    ToolCall(
                        id="proposal-submit",
                        name="submit_result",
                        arguments={"summary": "Proposed a memory.", "changed_files": []},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="recall",
                        name="recall_memory",
                        arguments={"query": "test order"},
                    )
                ]
            ),
            ModelResponse(
                content="The approved guidance says to run focused tests first.",
                tool_calls=[
                    ToolCall(
                        id="recall-submit",
                        name="submit_result",
                        arguments={"summary": "Recalled approved guidance.", "changed_files": []},
                    )
                ]
            ),
        ]
    )
    memory = MemoryManager(store=MemoryStore(tmp_path / ".aicoding-agent" / "memory"))
    agent = AgentLoop(
        model=model,
        executor=ToolExecutor(build_builtin_registry()),
        memory_manager=memory,
    )
    session = ChatSession(
        agent=agent,
        workspace=tmp_path,
        profile=profile,
        store=ChatTranscriptStore(tmp_path / ".aicoding-agent" / "chat"),
        session_id="memory",
        memory_manager=memory,
    )

    first = await session.send("Remember our test order")
    proposal = memory.entries[0]
    assert (await memory.recall("test order")) == []
    await memory.approve(proposal.memory_id[:8])
    second = await session.send("What is our test order?")

    assert first.status == "completed"
    assert second.status == "completed"
    assert "focused tests first" in second.assistant_message
    assert "Run focused tests before the full suite." in str(model.requests[3])
