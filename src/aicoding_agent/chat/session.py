from __future__ import annotations

import asyncio
import inspect
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from aicoding_agent.agent.background import BackgroundTaskManager
from aicoding_agent.agent.control import SteeringInbox
from aicoding_agent.agent.memory import MemoryManager
from aicoding_agent.agent.mutation_journal import MutationJournal, MutationRecoveryResult
from aicoding_agent.agent.plan import PlanManager
from aicoding_agent.agent.task_graph import TaskGraphManager
from aicoding_agent.agent.team import (
    TeamLaunchManager,
    TeamLaunchRecord,
    TeamMessage,
    TeamMessageBus,
    TeamMessageType,
    TeamPlanApproval,
    TeamPlanApprovalManager,
)
from aicoding_agent.chat.journal import (
    RecoverableRun,
    SessionJournalError,
    SessionMessageJournal,
    close_ambiguous_tool_calls,
)
from aicoding_agent.chat.models import ChatTurn
from aicoding_agent.chat.store import ChatTranscriptStore, validate_session_id
from aicoding_agent.context.session_memory import ConversationCompactor, ConversationContext
from aicoding_agent.domain.models import (
    AgentResult,
    AgentStatus,
    TargetEnvironment,
    TaskContract,
    TaskSource,
)

if TYPE_CHECKING:
    from aicoding_agent.regression.controller import RegressionController


class ConversationalAgent(Protocol):
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
        plan_manager: PlanManager | None = None,
        task_graph_manager: TaskGraphManager | None = None,
        control: SteeringInbox | None = None,
        background_manager: BackgroundTaskManager | None = None,
        mutation_journal: MutationJournal | None = None,
        memory_manager: MemoryManager | None = None,
        conversation_compactor: ConversationCompactor | None = None,
        message_journal: SessionMessageJournal | None = None,
        team_launch_manager: TeamLaunchManager | None = None,
        team_plan_approval_manager: TeamPlanApprovalManager | None = None,
        team_message_bus: TeamMessageBus | None = None,
        regression_controller: RegressionController | None = None,
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
        self.plan_manager = plan_manager or PlanManager(scope_id=self.session_id)
        self.task_graph_manager = task_graph_manager or TaskGraphManager(scope_id=self.session_id)
        self.control = control or SteeringInbox(scope_id=self.session_id)
        self.background_manager = background_manager or BackgroundTaskManager()
        self.mutation_journal = mutation_journal
        self.memory_manager = memory_manager
        self.conversation_compactor = conversation_compactor
        self.message_journal = message_journal
        self.team_launch_manager = team_launch_manager
        self.team_plan_approval_manager = team_plan_approval_manager
        self.team_message_bus = team_message_bus
        self.regression_controller = regression_controller
        self.turns: list[ChatTurn] = []
        self._last_context: ConversationContext | None = None
        self._initialized = False
        self._send_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if not self.workspace.is_dir():
            raise ValueError(f"Workspace does not exist: {self.workspace}")
        self.turns = await self.store.load(self.session_id)
        await self.plan_manager.initialize()
        await self.task_graph_manager.initialize()
        await self.control.initialize()
        await self.background_manager.initialize()
        if self.memory_manager is not None:
            await self.memory_manager.initialize()
        await self._reconcile_finished_journal_runs()
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
            if self.message_journal is not None:
                interrupted = await self.message_journal.latest_recoverable(self.session_id)
                if interrupted is not None:
                    raise SessionJournalError(
                        f"Interrupted run {interrupted.run_id} must be resumed or discarded first"
                    )
            turn_number = len(self.turns) + 1
            run_id = f"chat-{self.session_id}-{turn_number:04d}"
            if self.message_journal is not None:
                await self.message_journal.start_run(
                    self.session_id,
                    run_id=run_id,
                    turn_number=turn_number,
                    objective=message,
                )
            return await self._execute_turn(
                message,
                turn_number=turn_number,
                run_id=run_id,
            )

    async def compact(self, focus: str = "", *, full: bool = False) -> ConversationContext:
        """Backward-compatible alias for creating a fresh context window."""
        return await self.new_context(focus, full=full)

    async def new_context(self, focus: str = "", *, full: bool = False) -> ConversationContext:
        if not self._initialized:
            await self.initialize()
        if self.conversation_compactor is None:
            raise ValueError("Context rollover is unavailable")
        context = await self.conversation_compactor.prepare(
            self._all_conversation_messages(),
            scope_id=self._memory_scope_id,
            force=True,
            force_full=full,
            focus=focus.strip(),
        )
        self._last_context = context
        return context

    async def context_status(self) -> str:
        if not self._initialized:
            await self.initialize()
        if self.conversation_compactor is None:
            return (
                "Session Memory: unavailable\n"
                f"Recent history limits: {self.max_history_turns} turns / "
                f"{self.max_history_chars} characters"
            )
        context = await self.conversation_compactor.status(
            self._all_conversation_messages(),
            scope_id=self._memory_scope_id,
        )
        self._last_context = context
        budget = self.conversation_compactor.budget
        budget_status = budget.status(self._all_conversation_messages())
        return (
            f"Context: {context.context_tokens} estimated tokens "
            f"(raw history {context.raw_tokens}; window {budget_status.context_window_tokens})\n"
            f"Thresholds: warning={budget_status.warning_threshold_tokens}; "
            f"rollover={budget_status.compact_threshold_tokens}; "
            f"auto={self.conversation_compactor.auto_enabled}\n"
            f"Notes: v{context.snapshot_version or 0}; "
            f"history_window={context.history_window_id or 'none'}; "
            f"strategy={context.strategy or 'none'}; rolled_over={context.compacted}\n"
            f"Recent messages: {len(context.messages)}; "
            f"consecutive failures={context.consecutive_failures}; "
            f"last provider prompt tokens={budget_status.last_prompt_tokens or 'unknown'}"
            + (f"\nLast rollover error: {context.error}" if context.error else "")
        )

    async def recoverable_run(self) -> RecoverableRun | None:
        if not self._initialized:
            await self.initialize()
        if self.message_journal is None:
            return None
        return await self.message_journal.latest_recoverable(self.session_id)

    async def team_status(self, team_id: str) -> TeamLaunchRecord | None:
        if self.team_launch_manager is None:
            raise ValueError("Team launch management is unavailable")
        return await self.team_launch_manager.current(team_id)

    async def approve_team(self, team_id: str, proposal_id: str) -> TeamLaunchRecord:
        if self.team_launch_manager is None:
            raise ValueError("Team launch management is unavailable")
        return await self.team_launch_manager.approve(team_id, proposal_id)

    async def cancel_team(
        self,
        team_id: str,
        proposal_id: str,
        *,
        feedback: str = "",
    ) -> TeamLaunchRecord:
        if self.team_launch_manager is None:
            raise ValueError("Team launch management is unavailable")
        return await self.team_launch_manager.cancel(
            team_id,
            proposal_id,
            feedback=feedback,
        )

    async def team_plan_status(
        self,
        team_id: str,
        worker: str,
    ) -> TeamPlanApproval | None:
        if self.team_plan_approval_manager is None:
            raise ValueError("Team plan approval is unavailable")
        return await self.team_plan_approval_manager.current(team_id, worker)

    async def respond_team_plan(
        self,
        *,
        team_id: str,
        worker: str,
        request_id: str,
        approve: bool,
        feedback: str = "",
    ) -> TeamPlanApproval:
        if self.team_plan_approval_manager is None:
            raise ValueError("Team plan approval is unavailable")
        return await self.team_plan_approval_manager.respond(
            team_id=team_id,
            worker=worker,
            request_id=request_id,
            approve=approve,
            feedback=feedback,
        )

    async def send_team_message(
        self,
        *,
        team_id: str,
        worker: str,
        content: str,
    ) -> TeamMessage:
        if self.team_message_bus is None:
            raise ValueError("Team messaging is unavailable")
        return await self.team_message_bus.send(
            team_id=team_id,
            sender="lead",
            recipient=worker,
            message_type=TeamMessageType.DIRECT,
            content=content,
        )

    async def request_team_shutdown(self, *, team_id: str, worker: str) -> TeamMessage:
        if self.team_message_bus is None:
            raise ValueError("Team messaging is unavailable")
        return await self.team_message_bus.send(
            team_id=team_id,
            sender="lead",
            recipient=worker,
            message_type=TeamMessageType.SHUTDOWN_REQUEST,
            content="Graceful shutdown requested by the user.",
            request_id=uuid4().hex,
        )

    async def resume(self, *, allow_ambiguous: bool = False) -> ChatTurn:
        async with self._send_lock:
            if not self._initialized:
                await self.initialize()
            if self.message_journal is None:
                raise SessionJournalError("Message-level recovery is unavailable")
            recovered = await self.message_journal.latest_recoverable(self.session_id)
            if recovered is None:
                raise SessionJournalError("No interrupted Agent run is available")
            resume_messages = recovered.messages
            if not recovered.safe_to_resume and not allow_ambiguous:
                calls = ", ".join(recovered.unresolved_tool_call_ids)
                raise SessionJournalError(
                    "Automatic resume refused because tool execution is ambiguous: "
                    + calls
                    + ". Inspect state, then use /resume --force or /resume --discard"
                )
            if not recovered.safe_to_resume:
                resume_messages = close_ambiguous_tool_calls(
                    resume_messages, recovered.unresolved_tool_call_ids
                )
            if recovered.turn_number != len(self.turns) + 1:
                raise SessionJournalError("Recovered turn does not follow the completed transcript")
            return await self._execute_turn(
                recovered.objective,
                turn_number=recovered.turn_number,
                run_id=recovered.run_id,
                resume_messages=resume_messages,
            )

    async def discard_recovery(self) -> ChatTurn:
        async with self._send_lock:
            if not self._initialized:
                await self.initialize()
            if self.message_journal is None:
                raise SessionJournalError("Message-level recovery is unavailable")
            recovered = await self.message_journal.latest_recoverable(self.session_id)
            if recovered is None:
                raise SessionJournalError("No interrupted Agent run is available")
            if recovered.turn_number != len(self.turns) + 1:
                raise SessionJournalError("Recovered turn does not follow the completed transcript")
            result = AgentResult(
                status=AgentStatus.INTERRUPTED,
                run_id=recovered.run_id,
                final_text="Interrupted Agent run discarded without replaying tools.",
                reason="The user discarded message-level recovery",
            )
            turn = ChatTurn(
                turn_number=recovered.turn_number,
                run_id=recovered.run_id,
                user_message=recovered.objective,
                assistant_message=result.final_text,
                status=result.status,
                reason=result.reason,
                created_at=datetime.now(UTC),
            )
            await self.store.append(self.session_id, turn)
            self.turns.append(turn)
            await self.message_journal.finish_run(
                self.session_id,
                run_id=recovered.run_id,
                result=result,
            )
            return turn

    async def fork(
        self,
        target_session_id: str,
        *,
        through_turn: int | None = None,
    ) -> list[ChatTurn]:
        if not self._initialized:
            await self.initialize()
        return await self.store.fork(
            self.session_id,
            target_session_id,
            through_turn=through_turn,
        )

    async def rewind(
        self,
        target_session_id: str,
        *,
        through_turn: int,
    ) -> tuple[list[ChatTurn], MutationRecoveryResult]:
        if not self._initialized:
            await self.initialize()
        if not 1 <= through_turn <= len(self.turns):
            raise ValueError(
                f"Rewind turn must be between 1 and {len(self.turns)}, received {through_turn}"
            )
        later_run_ids = [turn.run_id for turn in self.turns[through_turn:]]
        if later_run_ids and self.mutation_journal is None:
            raise ValueError("Rewind requires the protected mutation journal")
        recovery: MutationRecoveryResult | None = None

        async def recover_before_fork() -> None:
            nonlocal recovery
            assert self.mutation_journal is not None
            recovery = await self.mutation_journal.recover_runs(
                workspace=self.workspace,
                run_ids=later_run_ids,
            )

        cloned = await self.store.fork(
            self.session_id,
            target_session_id,
            through_turn=through_turn,
            before_write=recover_before_fork if later_run_ids else None,
        )
        if recovery is None:
            recovery = MutationRecoveryResult(
                recovery_id="not-required",
                run_ids=[],
                restored_files=[],
                removed_files=[],
                skipped_records=0,
            )
        return cloned, recovery

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
                "allowed_tools": sorted(
                    self.profile.allowed_tools
                    | {
                        "ask_user",
                        "background_status",
                        "start_background_process",
                        "update_plan",
                        "update_task_graph",
                        "recall_memory",
                        "propose_memory",
                        "history_search",
                        "history_read_item",
                        "history_list_items",
                    }
                ),
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

    async def _conversation_context(self) -> tuple[list[dict[str, str]], str | None]:
        if self.conversation_compactor is None or not self.turns:
            return self._conversation_history(), None
        all_messages = self._all_conversation_messages()
        legacy_chars = sum(
            len(turn.user_message) + len(turn.assistant_message) for turn in self.turns
        )
        legacy_limit_exceeded = (
            len(self.turns) > self.max_history_turns or legacy_chars > self.max_history_chars
        )
        context = await self.conversation_compactor.prepare(
            all_messages,
            scope_id=self._memory_scope_id,
            force=legacy_limit_exceeded and self.conversation_compactor.auto_enabled,
        )
        self._last_context = context
        if context.error and context.summary is None:
            return self._conversation_history(), None
        history = [
            {"role": str(message["role"]), "content": str(message.get("content", ""))}
            for message in context.messages
            if message.get("role") in {"user", "assistant"}
        ]
        return history, context.summary

    def _all_conversation_messages(self) -> list[dict[str, Any]]:
        return [
            message
            for turn in self.turns
            for message in (
                {"role": "user", "content": turn.user_message},
                {"role": "assistant", "content": turn.assistant_message},
            )
        ]

    async def _execute_turn(
        self,
        message: str,
        *,
        turn_number: int,
        run_id: str,
        resume_messages: list[dict[str, Any]] | None = None,
    ) -> ChatTurn:
        contract = self._turn_contract(message, turn_number)
        conversation_history, conversation_memory = await self._conversation_context()
        optional_arguments: dict[str, Any] = {
            "run_id": run_id,
            "conversation_history": conversation_history,
            "conversation_memory": conversation_memory,
            "plan_manager": self.plan_manager,
            "task_graph_manager": self.task_graph_manager,
            "control": self.control,
            "background_manager": self.background_manager,
            "session_id": self.session_id,
            "message_journal": self.message_journal,
            "resume_messages": resume_messages,
            "history_scope_id": self._memory_scope_id,
        }
        signature = inspect.signature(self.agent.run)
        accepts_arbitrary_keywords = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        if not accepts_arbitrary_keywords:
            optional_arguments = {
                key: value
                for key, value in optional_arguments.items()
                if key in signature.parameters
            }
        if self.regression_controller is None:
            result = await self.agent.run(contract, self.workspace, **optional_arguments)
        else:
            outcome = await self.regression_controller.run(
                contract, self.workspace, run_id=run_id, agent=self.agent,
                agent_kwargs=optional_arguments, adopt=True,
                adoption_journal=self.mutation_journal,
            )
            result = outcome.agent_result
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
        if self.message_journal is not None:
            await self.message_journal.finish_run(
                self.session_id,
                run_id=run_id,
                result=result,
            )
        return turn

    async def _reconcile_finished_journal_runs(self) -> None:
        if self.message_journal is None:
            return
        completed = {turn.run_id: turn for turn in self.turns}
        while True:
            recovered = await self.message_journal.latest_recoverable(self.session_id)
            if recovered is None or recovered.run_id not in completed:
                return
            turn = completed[recovered.run_id]
            await self.message_journal.finish_run(
                self.session_id,
                run_id=turn.run_id,
                result=AgentResult(
                    status=turn.status,
                    run_id=turn.run_id,
                    final_text=turn.assistant_message,
                    outcome=turn.outcome,
                    turns=turn.model_turns,
                    tool_calls=turn.tool_calls,
                    reason=turn.reason,
                ),
            )

    @property
    def _memory_scope_id(self) -> str:
        return f"chat-{self.session_id}"

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
