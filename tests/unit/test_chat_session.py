from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest

from aicoding_agent.chat.session import ChatSession
from aicoding_agent.chat.store import ChatTranscriptStore
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
    ) -> AgentResult:
        self.calls.append(
            {
                "contract": contract,
                "workspace": workspace,
                "run_id": run_id,
                "history": conversation_history,
            }
        )
        return self.results.popleft().model_copy(update={"run_id": run_id or "run"})


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
