import asyncio
from pathlib import Path

import pytest

from aicoding_agent.agent.memory import MemoryManager, MemoryStatus, MemoryStore
from aicoding_agent.agent.permission import PermissionBroker
from aicoding_agent.chat.models import ChatTurn
from aicoding_agent.chat.session import ChatSession
from aicoding_agent.chat.store import ChatTranscriptStore
from aicoding_agent.cli import _handle_chat_input
from aicoding_agent.domain.models import AgentStatus, TaskContract, ToolRisk


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


@pytest.mark.asyncio
async def test_chat_command_resolves_pending_permission_by_prefix(tmp_path: Path) -> None:
    session = ChatSession(
        agent=UnusedAgent(),
        workspace=tmp_path,
        profile=TaskContract(
            task_id="profile",
            objective="Chat profile",
            acceptance_criteria=["Permissions are routed"],
        ),
        store=ChatTranscriptStore(tmp_path / "sessions"),
    )
    await session.initialize()
    broker = PermissionBroker(timeout_seconds=1)
    authorization = asyncio.create_task(
        broker.authorize(
            run_id="chat-permission",
            tool_name="external_lookup",
            risk=ToolRisk.EXTERNAL,
            arguments={"query": "status"},
            reason="Ask",
        )
    )
    for _ in range(100):
        if broker.pending:
            break
        await asyncio.sleep(0.005)
    assert broker.pending

    action = await _handle_chat_input(
        f"/allow {broker.pending[0].request_id[:8]} session",
        session=session,
        workspace=tmp_path,
        session_id="default",
        busy=True,
        permission_broker=broker,
    )

    assert action == "none"
    assert await authorization is True
    assert broker.pending == ()


@pytest.mark.asyncio
async def test_chat_commands_create_fork_and_rewind_sessions(tmp_path: Path) -> None:
    store = ChatTranscriptStore(tmp_path / "sessions")
    await store.append(
        "source",
        ChatTurn(
            turn_number=1,
            run_id="chat-source-0001",
            user_message="first request",
            assistant_message="first answer",
            status=AgentStatus.COMPLETED,
        ),
    )
    session = ChatSession(
        agent=UnusedAgent(),
        workspace=tmp_path,
        profile=TaskContract(
            task_id="profile",
            objective="Chat profile",
            acceptance_criteria=["Session commands are routed"],
        ),
        store=store,
        session_id="source",
    )
    await session.initialize()

    fork_action = await _handle_chat_input(
        "/fork branch 1",
        session=session,
        workspace=tmp_path,
        session_id="source",
        busy=False,
    )
    rewind_action = await _handle_chat_input(
        "/rewind rewound 1",
        session=session,
        workspace=tmp_path,
        session_id="source",
        busy=False,
    )

    assert fork_action == "none"
    assert rewind_action == "none"
    assert len(await store.load("branch")) == 1
    assert len(await store.load("rewound")) == 1


@pytest.mark.asyncio
async def test_chat_memory_commands_require_explicit_user_decisions(tmp_path: Path) -> None:
    manager = MemoryManager(store=MemoryStore(tmp_path / "memory"))
    proposal = await manager.propose(
        topic="tests",
        content="Run the focused test before the full suite.",
        ttl_days=30,
        source_run_id="agent-run",
    )
    rejected = await manager.propose(
        topic="obsolete",
        content="Use the previous build command.",
        ttl_days=30,
        source_run_id="agent-run",
    )
    session = ChatSession(
        agent=UnusedAgent(),
        workspace=tmp_path,
        profile=TaskContract(
            task_id="profile",
            objective="Chat profile",
            acceptance_criteria=["Memory commands are routed"],
        ),
        store=ChatTranscriptStore(tmp_path / "sessions"),
        memory_manager=manager,
    )
    await session.initialize()

    await _handle_chat_input(
        f"/memory-approve {proposal.memory_id[:8]}",
        session=session,
        workspace=tmp_path,
        session_id="default",
        busy=False,
    )
    await _handle_chat_input(
        f"/memory-reject {rejected.memory_id[:8]}",
        session=session,
        workspace=tmp_path,
        session_id="default",
        busy=False,
    )
    await _handle_chat_input(
        "/remember frontend-style 60 Prefer compact component spacing.",
        session=session,
        workspace=tmp_path,
        session_id="default",
        busy=False,
    )

    by_id = {entry.memory_id: entry for entry in manager.entries}
    assert by_id[proposal.memory_id].status == MemoryStatus.APPROVED
    assert by_id[rejected.memory_id].status == MemoryStatus.REJECTED
    recalled = await manager.recall("compact spacing")
    assert len(recalled) == 1
    assert recalled[0].source == "user"


@pytest.mark.asyncio
async def test_context_commands_remain_safe_when_optional_services_are_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = ChatSession(
        agent=UnusedAgent(),
        workspace=tmp_path,
        profile=TaskContract(
            task_id="profile",
            objective="Chat profile",
            acceptance_criteria=["Context commands are routed"],
        ),
        store=ChatTranscriptStore(tmp_path / "sessions"),
    )
    await session.initialize()

    for command in ("/context", "/new-context", "/compact", "/resume"):
        action = await _handle_chat_input(
            command,
            session=session,
            workspace=tmp_path,
            session_id="default",
            busy=False,
        )
        assert action == "none"

    output = capsys.readouterr().out
    assert "Session Memory: unavailable" in output
    assert "rollover failed" in output
    assert "Message-level recovery is unavailable" in output
