from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from aicoding_agent.chat.models import ChatTurn
from aicoding_agent.chat.store import ChatTranscriptStore, validate_session_id
from aicoding_agent.domain.models import AgentResult, TargetEnvironment, TaskContract, TaskSource


class ConversationalAgent(Protocol):
    async def run(
        self,
        contract: TaskContract,
        workspace: Path,
        *,
        run_id: str | None = None,
        conversation_history: list[dict[str, str]] | None = None,
    ) -> AgentResult: ...


class ChatSession:
    def __init__(
        self,
        *,
        agent: ConversationalAgent,
        workspace: Path,
        profile: TaskContract,
        store: ChatTranscriptStore,
        session_id: str = "default",
        max_history_turns: int = 12,
        max_history_chars: int = 40000,
    ) -> None:
        if profile.target_environment != TargetEnvironment.LOCAL:
            raise ValueError("Conversational coding only permits the local target environment")
        self.agent = agent
        self.workspace = workspace.resolve()
        self.profile = profile
        self.store = store
        self.session_id = validate_session_id(session_id)
        self.max_history_turns = max_history_turns
        self.max_history_chars = max_history_chars
        self.turns: list[ChatTurn] = []
        self._initialized = False
        self._send_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if not self.workspace.is_dir():
            raise ValueError(f"Workspace does not exist: {self.workspace}")
        self.turns = await self.store.load(self.session_id)
        self._initialized = True

    async def send(self, user_message: str) -> ChatTurn:
        message = user_message.strip()
        if not message:
            raise ValueError("Chat message cannot be empty")
        if len(message) > 20000:
            raise ValueError("Chat message exceeds 20000 characters")
        async with self._send_lock:
            if not self._initialized:
                await self.initialize()
            turn_number = len(self.turns) + 1
            run_id = f"chat-{self.session_id}-{turn_number:04d}"
            contract = self._turn_contract(message, turn_number)
            result = await self.agent.run(
                contract,
                self.workspace,
                run_id=run_id,
                conversation_history=self._conversation_history(),
            )
            turn = ChatTurn(
                turn_number=turn_number,
                run_id=run_id,
                user_message=message,
                assistant_message=self._assistant_message(result),
                status=result.status,
                outcome=result.outcome,
                model_turns=result.turns,
                tool_calls=result.tool_calls,
                reason=result.reason,
                created_at=datetime.now(UTC),
            )
            await self.store.append(self.session_id, turn)
            self.turns.append(turn)
            return turn

    def _turn_contract(self, message: str, turn_number: int) -> TaskContract:
        payload = self.profile.model_dump(mode="json")
        configured_criteria = list(self.profile.acceptance_criteria[:99])
        payload.update(
            {
                "task_id": f"chat:{self.session_id}:{turn_number}",
                "source": TaskSource.MANUAL,
                "source_ticket_id": None,
                "objective": message,
                "acceptance_criteria": [
                    "Address the user's current conversational request exactly.",
                    *configured_criteria,
                ][:100],
                "allowed_tools": sorted(self.profile.allowed_tools | {"ask_user"}),
                "target_environment": TargetEnvironment.LOCAL,
                "created_at": datetime.now(UTC),
            }
        )
        return TaskContract.model_validate(payload)

    def _conversation_history(self) -> list[dict[str, str]]:
        selected: list[ChatTurn] = []
        used_chars = 0
        for turn in reversed(self.turns[-self.max_history_turns :]):
            size = len(turn.user_message) + len(turn.assistant_message)
            if selected and used_chars + size > self.max_history_chars:
                break
            selected.append(turn)
            used_chars += size
        messages: list[dict[str, str]] = []
        for turn in reversed(selected):
            messages.extend(
                [
                    {"role": "user", "content": turn.user_message},
                    {"role": "assistant", "content": turn.assistant_message},
                ]
            )
        return messages

    @staticmethod
    def _assistant_message(result: AgentResult) -> str:
        if result.final_text.strip():
            return result.final_text.strip()[:20000]
        if result.outcome is not None:
            return result.outcome.summary[:20000]
        if result.reason:
            return result.reason[:20000]
        return f"Agent turn ended with status: {result.status}"

    @property
    def history(self) -> Sequence[ChatTurn]:
        return tuple(self.turns)
