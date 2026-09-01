from pathlib import Path

import pytest

from aicoding_agent.chat.session import ChatSession
from aicoding_agent.chat.store import ChatTranscriptStore
from aicoding_agent.cli import _handle_chat_input
from aicoding_agent.domain.models import TaskContract


class UnusedAgent:
    async def run(self, *args, **kwargs):
        raise AssertionError("The command parser should not start the Agent")


@pytest.mark.asyncio
async def test_busy_chat_input_queues_steer_next_and_interrupt(tmp_path: Path) -> None:
    session = ChatSession(
        agent=UnusedAgent(),
        workspace=tmp_path,
        profile=TaskContract(
            task_id="profile",
            objective="Chat profile",
            acceptance_criteria=["Commands are routed"],
        ),
        store=ChatTranscriptStore(tmp_path / "sessions"),
        session_id="control-test",
    )
    await session.initialize()

    await _handle_chat_input(
        "change direction",
        session=session,
        workspace=tmp_path,
        session_id="control-test",
        busy=True,
    )
    await _handle_chat_input(
        "/next follow up",
        session=session,
        workspace=tmp_path,
        session_id="control-test",
        busy=True,
    )
    await _handle_chat_input(
        "/interrupt",
        session=session,
        workspace=tmp_path,
        session_id="control-test",
        busy=True,
    )

    assert [message.intent for message in session.control.pending] == [
        "steer",
        "next",
        "interrupt",
    ]


@pytest.mark.asyncio
async def test_idle_natural_text_starts_a_turn(tmp_path: Path) -> None:
    session = ChatSession(
        agent=UnusedAgent(),
        workspace=tmp_path,
        profile=TaskContract(
            task_id="profile",
            objective="Chat profile",
            acceptance_criteria=["Natural text starts"],
        ),
        store=ChatTranscriptStore(tmp_path / "sessions"),
    )
    await session.initialize()

    action = await _handle_chat_input(
        "fix the counter",
        session=session,
        workspace=tmp_path,
        session_id="default",
        busy=False,
    )

    assert action == "start:fix the counter"
