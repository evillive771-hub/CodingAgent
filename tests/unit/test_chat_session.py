from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

import pytest

from aicoding_agent.agent.background import BackgroundTaskManager
from aicoding_agent.agent.control import SteeringInbox
from aicoding_agent.agent.mutation_journal import MutationJournal, MutationRecoveryError
from aicoding_agent.agent.plan import PlanManager
from aicoding_agent.agent.task_graph import TaskGraphManager
from aicoding_agent.chat.journal import SessionJournalError, SessionMessageJournal
from aicoding_agent.chat.session import ChatSession
from aicoding_agent.chat.store import ChatStoreError, ChatTranscriptStore
from aicoding_agent.context.budget import ContextBudgetConfig, ContextBudgetManager
from aicoding_agent.context.session_memory import (
    ConversationCompactionConfig,
    ConversationCompactor,
    SessionMemoryStore,
    SessionMemoryStrategy,
)
from aicoding_agent.domain.models import (
    AgentResult,
    AgentStatus,
    CodingOutcome,
    TaskContract,
)


class RecordingAgent:
    def __init__(self, results: list[AgentResult]) -> None:
        self.results = deque(results)
        self.calls: list[dict[str, object]] = []

    async def run(
        self,
        contract: TaskContract,
        workspace: Path,
        *,
        run_id: str | None = None,
        conversation_history: list[dict[str, str]] | None = None,
        plan_manager: PlanManager | None = None,
        task_graph_manager: TaskGraphManager | None = None,
        control: SteeringInbox | None = None,
        background_manager: BackgroundTaskManager | None = None,
        conversation_memory: str | None = None,
        session_id: str | None = None,
        message_journal: SessionMessageJournal | None = None,
        resume_messages: list[dict[str, Any]] | None = None,
    ) -> AgentResult:
        self.calls.append(
            {
                "contract": contract,
                "workspace": workspace,
                "run_id": run_id,
                "history": conversation_history,
                "memory": conversation_memory,
                "session_id": session_id,
                "journal": message_journal,
                "resume_messages": resume_messages,
            }
        )
        return self.results.popleft().model_copy(update={"run_id": run_id or "run"})


class SummaryStub:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def summarize(
        self,
        messages: list[dict[str, Any]],
        *,
        previous_summary: str | None,
        focus: str,
        strategy: SessionMemoryStrategy,
    ) -> str:
        self.calls.append(
            {
                "messages": messages,
                "previous_summary": previous_summary,
                "focus": focus,
                "strategy": strategy,
            }
        )
        return "Earlier work summary"


def profile() -> TaskContract:
    return TaskContract(
        task_id="chat-profile",
        objective="Conversational coding profile",
        acceptance_criteria=["Configured project checks pass after mutations"],
        allowed_tools={"read_file", "submit_result"},
    )


@pytest.mark.asyncio
async def test_chat_session_preserves_history_and_persists_turns(tmp_path: Path) -> None:
    first_agent = RecordingAgent(
        [
            AgentResult(
                status=AgentStatus.COMPLETED,
                run_id="placeholder",
                final_text="Fixed the counter and verified it.",
                outcome=CodingOutcome(summary="Fixed counter", changed_files=["counter.js"]),
            ),
            AgentResult(
                status=AgentStatus.WAITING_FOR_USER,
                run_id="placeholder",
                final_text="Should the counter wrap or clamp at zero?",
            ),
        ]
    )
    store = ChatTranscriptStore(tmp_path / "chat")
    session = ChatSession(
        agent=first_agent,
        workspace=tmp_path,
        profile=profile(),
        store=store,
        session_id="demo",
    )

    first = await session.send("Fix the counter bug")
    second = await session.send("Also handle negative values")

    assert first.turn_number == 1
    assert second.status == "waiting_for_user"
    second_call = first_agent.calls[1]
    assert second_call["history"] == [
        {"role": "user", "content": "Fix the counter bug"},
        {"role": "assistant", "content": "Fixed the counter and verified it."},
    ]
    second_contract = second_call["contract"]
    assert isinstance(second_contract, TaskContract)
    assert second_contract.objective == "Also handle negative values"
    assert "ask_user" in second_contract.allowed_tools

    resumed_agent = RecordingAgent(
        [AgentResult(status=AgentStatus.COMPLETED, run_id="placeholder", final_text="Clamped.")]
    )
    resumed = ChatSession(
        agent=resumed_agent,
        workspace=tmp_path,
        profile=profile(),
        store=store,
        session_id="demo",
    )
    await resumed.initialize()
    third = await resumed.send("Clamp at zero")

    assert third.turn_number == 3
    assert len(resumed.history) == 3
    assert len(resumed_agent.calls[0]["history"]) == 4


@pytest.mark.asyncio
async def test_chat_store_rejects_unsafe_session_id(tmp_path: Path) -> None:
    store = ChatTranscriptStore(tmp_path / "chat")

    with pytest.raises(ValueError, match="session_id"):
        await store.load("../outside")

    assert not (tmp_path / "outside").exists()


def completed_result(message: str) -> AgentResult:
    return AgentResult(
        status=AgentStatus.COMPLETED,
        run_id="placeholder",
        final_text=message,
    )


@pytest.mark.asyncio
async def test_chat_session_fork_copies_selected_history_with_provenance(
    tmp_path: Path,
) -> None:
    store = ChatTranscriptStore(tmp_path / "chat")
    session = ChatSession(
        agent=RecordingAgent([completed_result("first"), completed_result("second")]),
        workspace=tmp_path,
        profile=profile(),
        store=store,
        session_id="source",
    )
    first = await session.send("first request")
    await session.send("second request")

    cloned = await session.fork("branch", through_turn=1)

    assert len(cloned) == 1
    assert cloned[0].run_id == "fork-branch-0001"
    assert cloned[0].origin_run_id == first.run_id
    assert len(await store.load("source")) == 2
    assert await store.load("branch") == cloned
    with pytest.raises(ChatStoreError, match="already exists"):
        await session.fork("branch")


@pytest.mark.asyncio
async def test_chat_rewind_recovers_later_file_changes_then_forks_history(
    tmp_path: Path,
) -> None:
    journal = MutationJournal(tmp_path / "journal")
    store = ChatTranscriptStore(tmp_path / "chat")
    target = tmp_path / "value.txt"
    target.write_text("original\n", encoding="utf-8")
    session = ChatSession(
        agent=RecordingAgent(
            [completed_result("first"), completed_result("second"), completed_result("third")]
        ),
        workspace=tmp_path,
        profile=profile(),
        store=store,
        session_id="source",
        mutation_journal=journal,
    )

    first = await session.send("first request")
    before_first = await journal.capture(target)
    target.write_text("first\n", encoding="utf-8")
    await journal.record(
        run_id=first.run_id,
        tool_name="apply_text_patch",
        workspace=tmp_path,
        path=target,
        before=before_first,
    )
    second = await session.send("second request")
    before_second = await journal.capture(target)
    target.write_text("second\n", encoding="utf-8")
    await journal.record(
        run_id=second.run_id,
        tool_name="apply_text_patch",
        workspace=tmp_path,
        path=target,
        before=before_second,
    )
    third = await session.send("third request")
    created = tmp_path / "created.txt"
    before_created = await journal.capture(created)
    created.write_text("third\n", encoding="utf-8")
    await journal.record(
        run_id=third.run_id,
        tool_name="create_file",
        workspace=tmp_path,
        path=created,
        before=before_created,
    )

    cloned, recovery = await session.rewind("rewound", through_turn=1)

    assert len(cloned) == 1
    assert target.read_text(encoding="utf-8") == "first\n"
    assert not created.exists()
    assert recovery.restored_files == ["value.txt"]
    assert recovery.removed_files == ["created.txt"]
    assert len(await store.load("source")) == 3
    assert len(await store.load("rewound")) == 1


@pytest.mark.asyncio
async def test_chat_rewind_conflict_does_not_create_target_session(tmp_path: Path) -> None:
    journal = MutationJournal(tmp_path / "journal")
    store = ChatTranscriptStore(tmp_path / "chat")
    target = tmp_path / "value.txt"
    target.write_text("before\n", encoding="utf-8")
    session = ChatSession(
        agent=RecordingAgent([completed_result("first"), completed_result("second")]),
        workspace=tmp_path,
        profile=profile(),
        store=store,
        session_id="source",
        mutation_journal=journal,
    )
    await session.send("first request")
    second = await session.send("second request")
    before = await journal.capture(target)
    target.write_text("agent\n", encoding="utf-8")
    await journal.record(
        run_id=second.run_id,
        tool_name="apply_text_patch",
        workspace=tmp_path,
        path=target,
        before=before,
    )
    target.write_text("external\n", encoding="utf-8")

    with pytest.raises(MutationRecoveryError, match="not a journal boundary"):
        await session.rewind("must-not-exist", through_turn=1)

    assert await store.load("must-not-exist") == []
    assert target.read_text(encoding="utf-8") == "external\n"


@pytest.mark.asyncio
async def test_chat_session_automatically_rolls_history_and_injects_notes(
    tmp_path: Path,
) -> None:
    agent = RecordingAgent(
        [
            AgentResult(status=AgentStatus.COMPLETED, run_id="x", final_text="answer-1"),
            AgentResult(status=AgentStatus.COMPLETED, run_id="x", final_text="answer-2"),
            AgentResult(status=AgentStatus.COMPLETED, run_id="x", final_text="answer-3"),
        ]
    )
    summarizer = SummaryStub()
    budget = ContextBudgetManager(
        ContextBudgetConfig(
            context_window_tokens=8_192,
            max_output_tokens=512,
            summary_output_tokens=256,
            compact_buffer_tokens=512,
            warning_buffer_tokens=1_024,
        )
    )
    compactor = ConversationCompactor(
        summarizer=summarizer,
        budget=budget,
        store=SessionMemoryStore(tmp_path / "session-memory"),
        config=ConversationCompactionConfig(
            min_recent_tokens=0,
            max_recent_tokens=10_000,
            min_recent_messages=1,
            max_recent_messages=2,
        ),
    )
    session = ChatSession(
        agent=agent,
        workspace=tmp_path,
        profile=profile(),
        store=ChatTranscriptStore(tmp_path / "chat"),
        session_id="memory",
        max_history_turns=1,
        conversation_compactor=compactor,
    )

    await session.send("one")
    await session.send("two")
    await session.send("three")

    assert summarizer.calls
    assert agent.calls[-1]["memory"] == "Earlier work summary"
    assert len(agent.calls[-1]["history"]) == 2
    status = await session.context_status()
    assert "Notes: v1" in status
    assert "history_window=W000001" in status


@pytest.mark.asyncio
async def test_chat_session_resumes_safe_journal_without_replaying_tools(tmp_path: Path) -> None:
    journal = SessionMessageJournal(tmp_path / "journal")
    await journal.start_run(
        "resume", run_id="chat-resume-0001", turn_number=1, objective="Explain the code"
    )
    await journal.append_messages(
        "resume",
        run_id="chat-resume-0001",
        api_round_id="bootstrap",
        messages=[{"role": "user", "content": "Explain the code"}],
    )
    agent = RecordingAgent(
        [AgentResult(status=AgentStatus.COMPLETED, run_id="x", final_text="Recovered")]
    )
    session = ChatSession(
        agent=agent,
        workspace=tmp_path,
        profile=profile(),
        store=ChatTranscriptStore(tmp_path / "chat"),
        session_id="resume",
        message_journal=journal,
    )

    turn = await session.resume()

    assert turn.assistant_message == "Recovered"
    assert agent.calls[0]["resume_messages"] == [{"role": "user", "content": "Explain the code"}]
    assert await journal.latest_recoverable("resume") is None


@pytest.mark.asyncio
async def test_chat_session_refuses_ambiguous_tool_resume(tmp_path: Path) -> None:
    journal = SessionMessageJournal(tmp_path / "journal")
    await journal.start_run(
        "unsafe", run_id="chat-unsafe-0001", turn_number=1, objective="Change file"
    )
    await journal.append_messages(
        "unsafe",
        run_id="chat-unsafe-0001",
        api_round_id="round-1",
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "edit-1", "type": "function"}],
            }
        ],
    )
    agent = RecordingAgent(
        [AgentResult(status=AgentStatus.COMPLETED, run_id="x", final_text="Inspected")]
    )
    session = ChatSession(
        agent=agent,
        workspace=tmp_path,
        profile=profile(),
        store=ChatTranscriptStore(tmp_path / "chat"),
        session_id="unsafe",
        message_journal=journal,
    )

    with pytest.raises(SessionJournalError, match="ambiguous"):
        await session.resume()

    turn = await session.resume(allow_ambiguous=True)

    assert turn.assistant_message == "Inspected"
    resumed = str(agent.calls[0]["resume_messages"])
    assert "ambiguous_not_replayed" in resumed
    assert "ambiguous_resume" in resumed


@pytest.mark.asyncio
async def test_chat_session_can_discard_an_interrupted_run(tmp_path: Path) -> None:
    journal = SessionMessageJournal(tmp_path / "journal")
    await journal.start_run(
        "discard", run_id="chat-discard-0001", turn_number=1, objective="Old request"
    )
    session = ChatSession(
        agent=RecordingAgent([]),
        workspace=tmp_path,
        profile=profile(),
        store=ChatTranscriptStore(tmp_path / "chat"),
        session_id="discard",
        message_journal=journal,
    )

    turn = await session.discard_recovery()

    assert turn.status == AgentStatus.INTERRUPTED
    assert await journal.latest_recoverable("discard") is None
