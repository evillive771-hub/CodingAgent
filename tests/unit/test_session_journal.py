from pathlib import Path

import pytest

from aicoding_agent.chat.journal import SessionMessageJournal, sanitize_recovered_messages
from aicoding_agent.domain.models import AgentResult, AgentStatus


@pytest.mark.asyncio
async def test_message_journal_detects_and_restores_a_safe_interrupted_run(
    tmp_path: Path,
) -> None:
    journal = SessionMessageJournal(tmp_path)
    await journal.start_run(
        "default", run_id="chat-default-0001", turn_number=1, objective="Fix login"
    )
    await journal.append_messages(
        "default",
        run_id="chat-default-0001",
        api_round_id="round-1",
        messages=[
            {"role": "user", "content": "Fix login"},
            {"role": "assistant", "content": "I will inspect it."},
        ],
    )

    recovered = await journal.latest_recoverable("default")

    assert recovered is not None
    assert recovered.safe_to_resume is True
    assert recovered.objective == "Fix login"
    assert len(recovered.messages) == 2

    await journal.finish_run(
        "default",
        run_id="chat-default-0001",
        result=AgentResult(status=AgentStatus.COMPLETED, run_id="chat-default-0001"),
    )
    assert await journal.latest_recoverable("default") is None


@pytest.mark.asyncio
async def test_unresolved_tool_call_disables_automatic_resume(tmp_path: Path) -> None:
    journal = SessionMessageJournal(tmp_path)
    await journal.start_run(
        "default", run_id="chat-default-0001", turn_number=1, objective="Edit a file"
    )
    await journal.append_messages(
        "default",
        run_id="chat-default-0001",
        api_round_id="round-1",
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "write-1", "type": "function"}],
            }
        ],
    )

    recovered = await journal.latest_recoverable("default")

    assert recovered is not None
    assert recovered.safe_to_resume is False
    assert recovered.unresolved_tool_call_ids == ["write-1"]


def test_recovery_filters_orphan_results_and_adds_safe_continuation() -> None:
    messages = [
        {"role": "tool", "tool_call_id": "orphan", "content": "ignored"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "read-1", "type": "function"}],
        },
        {"role": "tool", "tool_call_id": "read-1", "content": "done"},
    ]

    recovered, unresolved = sanitize_recovered_messages(messages)

    assert unresolved == set()
    assert all(message.get("tool_call_id") != "orphan" for message in recovered)
    assert "resume_after_interrupt" in recovered[-1]["content"]
