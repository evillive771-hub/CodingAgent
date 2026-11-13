from __future__ import annotations

import asyncio
import copy
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from aicoding_agent.adapters.browser_tests import BrowserTestRunner
from aicoding_agent.agent.background import BackgroundStatus, BackgroundTaskManager
from aicoding_agent.agent.control import ControlIntent, SteeringInbox
from aicoding_agent.agent.gate import CompletionGate, GateAction
from aicoding_agent.agent.goal import (
    GoalEvaluationError,
    GoalEvaluationInput,
    GoalEvaluator,
    GoalStatus,
)
from aicoding_agent.agent.hooks import AsyncHookBus, HookContext, HookEvent
from aicoding_agent.agent.memory import MemoryManager
from aicoding_agent.agent.plan import PlanManager
from aicoding_agent.agent.preflight import inspect_runtime, render_runtime
from aicoding_agent.agent.recovery import RecoveryController
from aicoding_agent.agent.subagent import ReadOnlySubagentManager
from aicoding_agent.agent.task_graph import TaskGraphManager
from aicoding_agent.agent.team import TeamLaunchManager
from aicoding_agent.chat.journal import SessionMessageJournal
from aicoding_agent.context.archive import ContextEvidenceArchive
from aicoding_agent.context.builder import ContextBuilder, compact_messages
from aicoding_agent.context.compaction import AgentContextCompactor, run_memory_scope
from aicoding_agent.domain.models import (
    AgentResult,
    AgentStatus,
    TaskContract,
    ToolCall,
    ToolResult,
)
from aicoding_agent.model.base import ChatModel, ModelError
from aicoding_agent.persistence.base import EventSink, NullEventSink
from aicoding_agent.persistence.jsonl import AtomicCheckpointStore
from aicoding_agent.skills import SkillCatalog, SkillCatalogError
from aicoding_agent.tools.base import ToolContext
from aicoding_agent.tools.executor import ToolExecutor


class AgentLoop:
    async def propose_tests(
        self,
        contract: TaskContract,
        workspace: Path,
        *,
        run_id: str | None = None,
        shared_budget: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Produce a bounded test draft with an isolated, read-only stage registry."""
        from aicoding_agent.tools.base import ToolRegistry
        from aicoding_agent.tools.builtin import ListFilesTool, ReadFileTool, SearchTextTool
        from aicoding_agent.tools.tdd import SubmitTestProposalTool, TddProposal

        registry = ToolRegistry()
        for tool in (ReadFileTool(), ListFilesTool(), SearchTextTool(), SubmitTestProposalTool()):
            registry.register(tool)
        stage_contract = contract.model_copy(update={"allowed_tools": registry.names})
        run_id = run_id or uuid4().hex
        context = ToolContext(
            workspace=workspace,
            contract=stage_contract,
            run_id=run_id,
            shared_budget=shared_budget,
            tdd_proposal_mode=True,
            emit=lambda event, data: self.event_sink.emit(run_id, event, data),
        )
        executor = ToolExecutor(registry, policy=self.executor.policy)
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are preparing TDD tests only. Read the workspace "
                    "and submit a test proposal. "
                    "Do not implement or modify business code. Reuse existing tests when possible. "
                    "Test drafts must be under tests/, with test_*.py, *.test.* or *.spec.* names. "
                    "red_cases maps host suite IDs to exact failing case IDs. "
                    "requirement_links maps "
                    "each case ID to its zero-based acceptance criterion index. Tests must assert "
                    "the requested behavior, not deliberately break code or weaken existing tests. "
                    "The host will review and freeze the proposal before running RED."
                ),
            },
            {"role": "user", "content": contract.model_dump_json()},
        ]
        deadline = time.monotonic() + contract.budget.max_elapsed_seconds
        tool_count = 0
        await self.event_sink.emit(run_id, "tdd.proposal_started", {})
        for _ in range(contract.budget.max_turns):
            if shared_budget is not None:
                await shared_budget.charge_turn()
            remaining = min(
                deadline - time.monotonic(),
                shared_budget.remaining_seconds if shared_budget else float("inf"),
            )
            if remaining <= 0:
                raise TimeoutError("Test proposal time budget exhausted")
            async with asyncio.timeout(remaining):
                response = await self.model.complete(messages, registry.definitions())
                messages.append(
                    self._assistant_message(
                        response.content, response.reasoning_content, response.tool_calls
                    )
                )
                for call in response.tool_calls:
                    tool_count += 1
                    if tool_count > contract.budget.max_tool_calls:
                        raise RuntimeError("Test proposal tool budget exhausted")
                    if shared_budget is not None:
                        await shared_budget.charge_tools()
                    result = await executor.execute(call, context)
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": result.output}
                    )
                    if context.state.get("test_proposal") is not None:
                        proposal = TddProposal.model_validate(context.state["test_proposal"])
                        await self.event_sink.emit(
                            run_id,
                            "tdd.proposal_submitted",
                            {
                                "files": [item.path for item in proposal.files],
                                "red_cases": proposal.red_cases,
                            },
                        )
                        return proposal
            if not response.tool_calls:
                messages.append(
                    {
                        "role": "user",
                        "content": "Submit the test proposal using submit_test_proposal.",
                    }
                )
        raise RuntimeError("Test proposal turn budget exhausted")

    def __init__(
        self,
        *,
        model: ChatModel,
        executor: ToolExecutor,
        context_builder: ContextBuilder | None = None,
        completion_gate: CompletionGate | None = None,
        event_sink: EventSink | None = None,
        context_max_chars: int = 240000,
        hook_bus: AsyncHookBus | None = None,
        context_archive: ContextEvidenceArchive | None = None,
        background_manager: BackgroundTaskManager | None = None,
        recovery_controller: RecoveryController | None = None,
        goal_evaluator: GoalEvaluator | None = None,
        subagent_manager: ReadOnlySubagentManager | None = None,
        memory_manager: MemoryManager | None = None,
        context_compactor: AgentContextCompactor | None = None,
        team_launch_manager: TeamLaunchManager | None = None,
        browser_runner: BrowserTestRunner | None = None,
    ) -> None:
        self.model = model
        self.executor = executor
        self.context_builder = context_builder or ContextBuilder()
        self.completion_gate = completion_gate or CompletionGate()
        self.event_sink = event_sink or NullEventSink()
        self.context_max_chars = context_max_chars
        self.hook_bus = hook_bus or AsyncHookBus()
        self.context_archive = context_archive
        self.background_manager = background_manager
        self.recovery_controller = recovery_controller or RecoveryController()
        self.goal_evaluator = goal_evaluator
        self.subagent_manager = subagent_manager
        self.memory_manager = memory_manager
        self.context_compactor = context_compactor
        self.team_launch_manager = team_launch_manager
        self.browser_runner = browser_runner

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
        history_scope_id: str | None = None,
        shared_budget: Any = None,
        tdd_authorized: bool = False,
    ) -> AgentResult:
        resolved_workspace = workspace.resolve()
        if not resolved_workspace.is_dir():
            raise ValueError(f"Workspace does not exist: {resolved_workspace}")
        run_id = run_id or uuid4().hex
        if contract.tdd is not None and not tdd_authorized:
            result = AgentResult(status=AgentStatus.BLOCKED, run_id=run_id,
                                 reason="TDD coding requires host-verified RED evidence")
            await self.event_sink.emit(run_id, "agent.blocked", {"reason": result.reason})
            return result
        background_manager = background_manager or self.background_manager
        started_at = time.monotonic()
        initial_deadline_epoch = datetime.now(UTC).timestamp() + contract.budget.max_elapsed_seconds
        browser_budget_store: AtomicCheckpointStore | None = None
        browser_budget: dict[str, Any] = {}
        if contract.browser_verifications:
            try:
                if self.browser_runner is None:
                    raise ValueError("Browser verification requires a host --browser-suites config")
                await self.browser_runner.freeze(resolved_workspace)
                if "run_playwright" not in contract.allowed_tools:
                    raise ValueError(
                        "Browser verification requires run_playwright in allowed_tools"
                    )
                for verification in contract.browser_verifications:
                    if not self.browser_runner.has_suite(verification.suite_ref):
                        raise ValueError(f"Unregistered browser suite: {verification.suite_ref}")
                await self.executor.enable_browser_guard(resolved_workspace)
                suite_fingerprints = self.browser_runner.frozen_suite_fingerprints(
                    resolved_workspace
                )
                browser_budget_store = (
                    AtomicCheckpointStore(self.browser_runner.evidence_root / "budgets")
                    if shared_budget is None
                    else None
                )
                previous_budget = (
                    await browser_budget_store.load(run_id) if browser_budget_store else None
                )
                if previous_budget is not None:
                    if previous_budget.get("workspace") != str(resolved_workspace) or (
                        previous_budget.get("suite_fingerprints") != suite_fingerprints
                    ):
                        raise ValueError(
                            "Resumed browser run cannot replace its workspace or suite"
                        )
                    if previous_budget["browser_verifications"] != [
                        item.model_dump(mode="json") for item in contract.browser_verifications
                    ] or previous_budget["budget"] != contract.budget.model_dump(mode="json"):
                        raise ValueError("Resumed browser run cannot replace its tests or budget")
                    browser_budget = previous_budget
                    remaining = (
                        float(browser_budget["deadline_epoch"]) - datetime.now(UTC).timestamp()
                    )
                    started_at = time.monotonic() - (
                        contract.budget.max_elapsed_seconds - remaining
                    )
                else:
                    browser_budget = {
                        "deadline_epoch": initial_deadline_epoch,
                        "turns": 0,
                        "tool_calls": 0,
                        "workspace": str(resolved_workspace),
                        "suite_fingerprints": suite_fingerprints,
                        "budget": contract.budget.model_dump(mode="json"),
                        "browser_verifications": [
                            item.model_dump(mode="json") for item in contract.browser_verifications
                        ],
                    }
                    if browser_budget_store is not None:
                        await browser_budget_store.save(run_id, browser_budget)
            except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
                result = AgentResult(
                    status=AgentStatus.FAILED,
                    run_id=run_id,
                    reason=f"Browser verification configuration failed: {exc}",
                )
                await self.event_sink.emit(run_id, "agent.failed", result.model_dump(mode="json"))
                return result
        recovery = self.recovery_controller.new_session()
        skill_catalog: SkillCatalog | None = None
        if "load_skill" in contract.allowed_tools:
            try:
                skill_catalog = await SkillCatalog.discover(resolved_workspace, contract)
            except SkillCatalogError as exc:
                await self.event_sink.emit(
                    run_id,
                    "agent.failed",
                    {"error_type": type(exc).__name__, "message": str(exc)},
                )
                return AgentResult(
                    status=AgentStatus.FAILED,
                    run_id=run_id,
                    reason=f"Skill catalog configuration failed: {exc}",
                )
        tool_context = ToolContext(
            workspace=resolved_workspace,
            contract=contract,
            emit=lambda event_type, data: self.event_sink.emit(run_id, event_type, data),
            plan_manager=plan_manager,
            run_id=run_id,
            background_manager=background_manager,
            skill_catalog=skill_catalog,
            task_graph_manager=task_graph_manager,
            subagent_manager=self.subagent_manager,
            memory_manager=self.memory_manager,
            history_store=(
                self.context_compactor.conversation_compactor.history_store
                if self.context_compactor is not None
                else None
            ),
            history_scope_id=history_scope_id or run_memory_scope(run_id),
            team_launch_manager=self.team_launch_manager,
            browser_runner=self.browser_runner,
            deadline_at=(
                shared_budget.deadline_at
                if shared_budget is not None
                else started_at + contract.budget.max_elapsed_seconds
            ),
            shared_budget=shared_budget,
        )
        if plan_manager is not None:
            await plan_manager.initialize()
        if task_graph_manager is not None:
            await task_graph_manager.initialize()
        if control is not None:
            await control.initialize()
        if background_manager is not None:
            await background_manager.initialize()
        if self.memory_manager is not None:
            await self.memory_manager.initialize()
        runtime_facts = await inspect_runtime(
            resolved_workspace, contract, registered_tools=self.executor.registry.names
        )
        await self.event_sink.emit(run_id, "agent.runtime_preflight", runtime_facts)
        if resume_messages is None:
            messages = self.context_builder.build(
                contract,
                resolved_workspace,
                conversation_history=conversation_history,
                conversation_memory=conversation_memory,
                active_plan=plan_manager.render() if plan_manager is not None else None,
                active_task_graph=(
                    task_graph_manager.render() if task_graph_manager is not None else None
                ),
                skill_catalog=(
                    skill_catalog.render_prompt_catalog() if skill_catalog is not None else None
                ),
            )
            messages[-1]["content"] += "\n" + render_runtime(runtime_facts)
        else:
            messages = copy.deepcopy(resume_messages)
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "<resume_checkpoint>Continue from the last safely persisted message. "
                        "Do not repeat completed tool side effects. Re-check current files before "
                        "making further changes.</resume_checkpoint>\n"
                        + render_runtime(runtime_facts)
                    ),
                }
            )
        journaled_messages = 0

        async def persist_new_messages(api_round_id: str) -> None:
            nonlocal journaled_messages
            if message_journal is None or session_id is None:
                journaled_messages = len(messages)
                return
            pending = messages[journaled_messages:]
            if pending:
                await message_journal.append_messages(
                    session_id,
                    run_id=run_id,
                    messages=pending,
                    api_round_id=api_round_id,
                )
                journaled_messages = len(messages)

        if resume_messages is None:
            await persist_new_messages("bootstrap")
        else:
            # Recovered messages are already journaled; append only the resume checkpoint.
            journaled_messages = max(0, len(messages) - 1)
            await persist_new_messages("resume")
        prompt_hooks = await self.hook_bus.trigger(
            HookEvent.USER_PROMPT_SUBMIT,
            HookContext(
                run_id=run_id,
                workspace=resolved_workspace,
                contract=contract,
                user_message=contract.objective,
            ),
        )
        if prompt_hooks.block_reason:
            return AgentResult(
                status=AgentStatus.FAILED,
                run_id=run_id,
                reason=f"UserPromptSubmit hook blocked the run: {prompt_hooks.block_reason}",
            )
        messages.extend(
            {"role": "user", "content": message} for message in prompt_hooks.inject_messages
        )
        exposed_tools = set(contract.allowed_tools)
        if not contract.browser_verifications:
            exposed_tools.discard("run_playwright")
        tool_definitions = self.executor.registry.definitions(exposed_tools)
        turns = int(browser_budget.get("turns", 0))
        tool_call_count = int(browser_budget.get("tool_calls", 0))
        stop_blocks = 0
        budget_warning_emitted = False

        async def persist_browser_budget() -> None:
            if browser_budget_store is not None:
                browser_budget.update(turns=turns, tool_calls=tool_call_count)
                await browser_budget_store.save(run_id, browser_budget)

        await self.event_sink.emit(
            run_id,
            "agent.started",
            {
                "task_id": contract.task_id,
                "workspace": str(resolved_workspace),
                "allowed_tools": sorted(contract.allowed_tools),
                "skills": [
                    {"name": item.name, "sha256": item.sha256}
                    for item in (skill_catalog.summaries if skill_catalog is not None else ())
                ],
            },
        )

        while turns < contract.budget.max_turns:
            if background_manager is not None:
                await self._inject_background_notifications(
                    background_manager,
                    messages,
                    run_id=run_id,
                    tool_context=tool_context,
                )
            if control is not None:
                control_result = await self._apply_control(
                    control,
                    messages,
                    run_id=run_id,
                    plan_manager=plan_manager,
                    task_graph_manager=task_graph_manager,
                    turns=turns,
                    tool_calls=tool_call_count,
                    tool_context=tool_context,
                )
                if control_result is not None:
                    return control_result
            if time.monotonic() - started_at > contract.budget.max_elapsed_seconds:
                return await self._budget_result(
                    run_id, turns, tool_call_count, "Maximum elapsed time reached"
                )
            turns += 1
            if shared_budget is not None:
                from aicoding_agent.regression.budget import RegressionBudgetExceeded

                try:
                    await shared_budget.charge_turn()
                except RegressionBudgetExceeded as exc:
                    return await self._budget_result(run_id, turns, tool_call_count, str(exc))
            await persist_browser_budget()
            await self.event_sink.emit(run_id, "agent.turn_started", {"turn": turns})
            remaining_turns = contract.budget.max_turns - turns + 1
            if remaining_turns <= 3 and not budget_warning_emitted:
                budget_warning_emitted = True
                warning = (
                    f"<budget_warning>{remaining_turns} model rounds remain, including this one. "
                    "Stop broad exploration. Make only necessary changes, run every configured "
                    "verification command after the final mutation, then submit a factual result. "
                    "If runtime verification is unavailable, disclose that limitation instead of "
                    "claiming verified completion.</budget_warning>"
                )
                messages.append({"role": "user", "content": warning})
                await self.event_sink.emit(
                    run_id,
                    "agent.budget_warning",
                    {"turn": turns, "remaining_turns": remaining_turns},
                )
            before_model = await self.hook_bus.trigger(
                HookEvent.BEFORE_MODEL_CALL,
                HookContext(
                    run_id=run_id,
                    workspace=resolved_workspace,
                    contract=contract,
                    turn=turns,
                ),
            )
            if before_model.block_reason:
                return AgentResult(
                    status=AgentStatus.FAILED,
                    run_id=run_id,
                    turns=turns,
                    tool_calls=tool_call_count,
                    reason=f"BeforeModelCall hook blocked the run: {before_model.block_reason}",
                )
            messages.extend(
                {"role": "user", "content": message} for message in before_model.inject_messages
            )
            await persist_new_messages(f"round-{turns}-input")
            try:
                raw_context_chars = self._messages_size(messages)
                prepared_context = (
                    await self.context_compactor.prepare(messages, run_id=run_id)
                    if self.context_compactor is not None
                    else None
                )
                model_messages = (
                    prepared_context.messages
                    if prepared_context is not None
                    else (
                        await self.context_archive.compact(
                            messages,
                            run_id=run_id,
                            max_chars=self.context_max_chars,
                        )
                        if self.context_archive is not None
                        else compact_messages(messages, max_chars=self.context_max_chars)
                    )
                )
                model_context_chars = self._messages_size(model_messages)
                compaction_record = (
                    prepared_context.record if prepared_context is not None else None
                )
                budget_status = (
                    self.context_compactor.budget.status(messages)
                    if self.context_compactor is not None
                    else None
                )
                await self.event_sink.emit(
                    run_id,
                    "context.prepared",
                    {
                        "turn": turns,
                        "raw_chars": raw_context_chars,
                        "model_chars": model_context_chars,
                        "compacted": (
                            compaction_record.compacted
                            if compaction_record is not None
                            else model_context_chars < raw_context_chars
                        ),
                        "archive_compacted": (
                            compaction_record.archive_compacted
                            if compaction_record is not None
                            else False
                        ),
                        "semantic_compacted": (
                            compaction_record.semantic_compacted
                            if compaction_record is not None
                            else False
                        ),
                        "message_count": len(model_messages),
                        "raw_tokens": (
                            compaction_record.raw_tokens if compaction_record is not None else None
                        ),
                        "archived_tokens": (
                            compaction_record.archived_tokens
                            if compaction_record is not None
                            else None
                        ),
                        "model_tokens": (
                            compaction_record.model_tokens
                            if compaction_record is not None
                            else None
                        ),
                        "compaction_strategy": (
                            compaction_record.strategy if compaction_record is not None else None
                        ),
                        "summary_version": (
                            compaction_record.summary_version
                            if compaction_record is not None
                            else None
                        ),
                        "notes_version": (
                            compaction_record.summary_version
                            if compaction_record is not None
                            else None
                        ),
                        "history_window_id": (
                            compaction_record.history_window_id
                            if compaction_record is not None
                            else None
                        ),
                        "compaction_error": (
                            compaction_record.error if compaction_record is not None else None
                        ),
                        "context_warning": (
                            budget_status.warning if budget_status is not None else False
                        ),
                        "warning_threshold_tokens": (
                            budget_status.warning_threshold_tokens
                            if budget_status is not None
                            else None
                        ),
                        "compact_threshold_tokens": (
                            budget_status.compact_threshold_tokens
                            if budget_status is not None
                            else None
                        ),
                    },
                )
                model_started_at = time.monotonic()
                timeout = (
                    max(0, tool_context.deadline_at - time.monotonic())
                    if (contract.browser_verifications or shared_budget is not None)
                    and tool_context.deadline_at is not None
                    else None
                )
                async with asyncio.timeout(timeout):
                    response = await recovery.complete(
                        self.model,
                        model_messages,
                        tool_definitions,
                        emit=lambda event_type, data, turn=turns: self.event_sink.emit(
                            run_id,
                            event_type,
                            {"turn": turn, **data},
                        ),
                    )
            except TimeoutError:
                return await self._budget_result(
                    run_id, turns, tool_call_count, "Task time budget exhausted during model call"
                )
            except ModelError as exc:
                await self.event_sink.emit(
                    run_id,
                    "agent.failed",
                    {"turn": turns, "error_type": type(exc).__name__, "message": str(exc)},
                )
                return AgentResult(
                    status=AgentStatus.FAILED,
                    run_id=run_id,
                    turns=turns,
                    tool_calls=tool_call_count,
                    reason=str(exc),
                )

            assistant_message = self._assistant_message(
                response.content,
                response.reasoning_content,
                response.tool_calls,
            )
            messages.append(assistant_message)
            await persist_new_messages(f"round-{turns}-assistant")
            if self.context_compactor is not None:
                self.context_compactor.budget.observe_usage(response.usage)
            await self.event_sink.emit(
                run_id,
                "model.response",
                {
                    "turn": turns,
                    "content": response.content,
                    "tool_names": [call.name for call in response.tool_calls],
                    "usage": response.usage,
                    "finish_reason": response.finish_reason,
                    "duration_ms": round((time.monotonic() - model_started_at) * 1000, 3),
                },
            )

            if self._has_current_control(control):
                if response.tool_calls:
                    tool_call_count += len(response.tool_calls)
                    await persist_browser_budget()
                    messages.extend(
                        self._tool_messages(
                            [
                                ToolResult(
                                    tool_call_id=call.id,
                                    tool_name=call.name,
                                    ok=False,
                                    output="Cancelled before execution by a user control message",
                                )
                                for call in response.tool_calls
                            ]
                        )
                    )
                    await persist_new_messages(f"round-{turns}-cancelled-tools")
                control_result = await self._apply_control(
                    control,
                    messages,
                    run_id=run_id,
                    plan_manager=plan_manager,
                    task_graph_manager=task_graph_manager,
                    turns=turns,
                    tool_calls=tool_call_count,
                    tool_context=tool_context,
                )
                if control_result is not None:
                    return control_result
                continue

            if response.tool_calls:
                if shared_budget is not None:
                    try:
                        await shared_budget.charge_tools(len(response.tool_calls))
                    except RegressionBudgetExceeded as exc:
                        return await self._budget_result(run_id, turns, tool_call_count, str(exc))
                if tool_call_count + len(response.tool_calls) > contract.budget.max_tool_calls:
                    return await self._budget_result(
                        run_id, turns, tool_call_count, "Maximum tool call budget reached"
                    )
                tool_call_count += len(response.tool_calls)
                await persist_browser_budget()
                try:
                    async with asyncio.timeout(
                        shared_budget.remaining_seconds if shared_budget is not None else None
                    ):
                        results, control_pending = await self._execute_tool_calls(
                            response.tool_calls,
                            tool_context,
                            control=control,
                        )
                except TimeoutError:
                    return await self._budget_result(
                        run_id,
                        turns,
                        tool_call_count,
                        "Task time budget exhausted during tools",
                    )
                messages.extend(self._tool_messages(results))
                await persist_new_messages(f"round-{turns}-tool-results")
                await self.hook_bus.trigger(
                    HookEvent.AFTER_TURN,
                    HookContext(
                        run_id=run_id,
                        workspace=resolved_workspace,
                        contract=contract,
                        turn=turns,
                    ),
                )
                if control_pending and control is not None:
                    control_result = await self._apply_control(
                        control,
                        messages,
                        run_id=run_id,
                        plan_manager=plan_manager,
                        task_graph_manager=task_graph_manager,
                        turns=turns,
                        tool_calls=tool_call_count,
                        tool_context=tool_context,
                    )
                    if control_result is not None:
                        return control_result
                    continue
                browser_terminal = tool_context.state.get("browser_terminal_status")
                if browser_terminal:
                    result = AgentResult(
                        status=AgentStatus(browser_terminal),
                        run_id=run_id,
                        turns=turns,
                        tool_calls=tool_call_count,
                        reason=tool_context.state.get("browser_terminal_reason"),
                    )
                    await self.event_sink.emit(
                        run_id, f"agent.{result.status.value}", result.model_dump(mode="json")
                    )
                    return result
                circuit = recovery.observe_tool_results(response.tool_calls, results)
                if circuit is not None:
                    await self.event_sink.emit(
                        run_id,
                        "tool.circuit_opened",
                        {"turn": turns, **circuit.model_dump(mode="json")},
                    )
                    result = AgentResult(
                        status=AgentStatus.FAILED,
                        run_id=run_id,
                        turns=turns,
                        tool_calls=tool_call_count,
                        reason=circuit.reason,
                    )
                    await self.event_sink.emit(
                        run_id,
                        "agent.failed",
                        result.model_dump(mode="json"),
                    )
                    return result
                question = tool_context.state.get("user_question")
                if isinstance(question, str) and question:
                    result = AgentResult(
                        status=AgentStatus.WAITING_FOR_USER,
                        run_id=run_id,
                        final_text=question,
                        turns=turns,
                        tool_calls=tool_call_count,
                        reason="The Agent requested user clarification",
                    )
                    await self.event_sink.emit(
                        run_id,
                        "agent.waiting_for_user",
                        result.model_dump(mode="json"),
                    )
                    return result
                # A submission requests host validation in this turn, even at the budget
                # boundary. Evaluate only after the whole batch has settled: later edits
                # can invalidate the submitted outcome and its verification evidence.
                if not any(result.ok and result.tool_name == "submit_result" for result in results):
                    continue

            stop_hooks = await self.hook_bus.trigger(
                HookEvent.BEFORE_STOP,
                HookContext(
                    run_id=run_id,
                    workspace=resolved_workspace,
                    contract=contract,
                    turn=turns,
                ),
            )
            if stop_hooks.continue_message:
                messages.append({"role": "user", "content": stop_hooks.continue_message})
                continue
            await self.completion_gate.refresh_browser_evidence(tool_context)
            decision = self.completion_gate.evaluate(contract, tool_context)
            await self.event_sink.emit(
                run_id,
                "agent.stop_evaluated",
                {"turn": turns, "action": decision.action, "reason": decision.reason},
            )
            if decision.action == GateAction.UNVERIFIED:
                outcome = tool_context.state.get("coding_outcome")
                result = AgentResult(
                    status=AgentStatus.SUBMITTED_UNVERIFIED,
                    run_id=run_id,
                    final_text=response.content or outcome.summary,
                    outcome=outcome,
                    turns=turns,
                    tool_calls=tool_call_count,
                    reason=decision.reason,
                )
                await self.event_sink.emit(
                    run_id,
                    "agent.submitted_unverified",
                    result.model_dump(mode="json"),
                )
                if not response.tool_calls:
                    await self.hook_bus.trigger(
                        HookEvent.AFTER_TURN,
                        HookContext(
                            run_id=run_id,
                            workspace=resolved_workspace,
                            contract=contract,
                            turn=turns,
                        ),
                    )
                return result

            if decision.action == GateAction.COMPLETE:
                if contract.browser_verifications and (
                    time.monotonic() - started_at >= contract.budget.max_elapsed_seconds
                ):
                    return await self._budget_result(
                        run_id,
                        turns,
                        tool_call_count,
                        "Task time budget exhausted before completion",
                    )
                outcome = tool_context.state.get("coding_outcome")
                deterministic_authoritative = (
                    bool(contract.verification_commands)
                    and (
                        int(tool_context.state.get("last_mutation_sequence", 0)) > 0
                        or bool(outcome.changed_files)
                    )
                ) or any(item.required for item in contract.browser_verifications)
                if self.goal_evaluator is not None:
                    goal_started_at = time.monotonic()
                    try:
                        goal_timeout = (
                            max(0, tool_context.deadline_at - time.monotonic())
                            if contract.browser_verifications
                            and tool_context.deadline_at is not None
                            else None
                        )
                        async with asyncio.timeout(goal_timeout):
                            goal_result = await self.goal_evaluator.evaluate(
                                self._goal_input(contract, tool_context)
                            )
                    except TimeoutError:
                        return await self._budget_result(
                            run_id,
                            turns,
                            tool_call_count,
                            "Task time budget exhausted during goal evaluation",
                        )
                    except GoalEvaluationError as exc:
                        if deterministic_authoritative:
                            await self.event_sink.emit(
                                run_id,
                                "goal.evaluation_failed",
                                {
                                    "turn": turns,
                                    "authoritative": False,
                                    "overridden_by": "deterministic_verification",
                                    "error_type": type(exc).__name__,
                                    "message": str(exc),
                                },
                            )
                        else:
                            result = AgentResult(
                                status=AgentStatus.FAILED,
                                run_id=run_id,
                                turns=turns,
                                tool_calls=tool_call_count,
                                reason=f"Semantic goal evaluation failed: {exc}",
                            )
                            await self.event_sink.emit(
                                run_id,
                                "agent.failed",
                                result.model_dump(mode="json"),
                            )
                            return result
                    else:
                        evaluation = goal_result.evaluation
                        await self.event_sink.emit(
                            run_id,
                            "goal.evaluated",
                            {
                                "turn": turns,
                                "status": evaluation.status,
                                "reason": evaluation.reason,
                                "missing": evaluation.missing,
                                "evidence_refs": evaluation.evidence_refs,
                                "attempts": goal_result.attempts,
                                "usage": goal_result.usage,
                                "authoritative": not deterministic_authoritative,
                                "overridden_by": (
                                    "deterministic_verification"
                                    if deterministic_authoritative
                                    and evaluation.status != GoalStatus.COMPLETE
                                    else None
                                ),
                                "duration_ms": round(
                                    (time.monotonic() - goal_started_at) * 1000,
                                    3,
                                ),
                            },
                        )
                        if not deterministic_authoritative:
                            if evaluation.status == GoalStatus.NEEDS_USER:
                                result = AgentResult(
                                    status=AgentStatus.WAITING_FOR_USER,
                                    run_id=run_id,
                                    final_text=evaluation.reason,
                                    turns=turns,
                                    tool_calls=tool_call_count,
                                    reason="Semantic goal evaluation requires user input",
                                )
                                await self.event_sink.emit(
                                    run_id,
                                    "agent.waiting_for_user",
                                    result.model_dump(mode="json"),
                                )
                                return result
                            if evaluation.status == GoalStatus.IMPOSSIBLE:
                                result = AgentResult(
                                    status=AgentStatus.FAILED,
                                    run_id=run_id,
                                    turns=turns,
                                    tool_calls=tool_call_count,
                                    reason=f"Semantic goal is impossible: {evaluation.reason}",
                                )
                                await self.event_sink.emit(
                                    run_id,
                                    "agent.failed",
                                    result.model_dump(mode="json"),
                                )
                                return result
                            if evaluation.status == GoalStatus.INCOMPLETE:
                                stop_blocks += 1
                                if stop_blocks > contract.budget.max_stop_blocks:
                                    return await self._budget_result(
                                        run_id,
                                        turns,
                                        tool_call_count,
                                        "Semantic acceptance remained incomplete after the "
                                        "retry limit",
                                    )
                                messages.append(
                                    {
                                        "role": "user",
                                        "content": (
                                            "<goal_evaluation_blocked>"
                                            f"{evaluation.reason} Missing: "
                                            f"{'; '.join(evaluation.missing)}. "
                                            "Continue working; deterministic host gates remain "
                                            "authoritative."
                                            "</goal_evaluation_blocked>"
                                        ),
                                    }
                                )
                                continue
                # The semantic evaluator may take time: check browser input again before
                # accepting the outcome, including edits made outside model tools.
                await self.completion_gate.refresh_browser_evidence(tool_context)
                final_decision = self.completion_gate.evaluate(contract, tool_context)
                if final_decision.action != GateAction.COMPLETE:
                    messages.append({"role": "user", "content": final_decision.reason})
                    continue
                result = AgentResult(
                    status=AgentStatus.COMPLETED,
                    run_id=run_id,
                    final_text=response.content or outcome.summary,
                    outcome=outcome,
                    turns=turns,
                    tool_calls=tool_call_count,
                )
                await self.event_sink.emit(
                    run_id, "agent.completed", result.model_dump(mode="json")
                )
                if not response.tool_calls:
                    await self.hook_bus.trigger(
                        HookEvent.AFTER_TURN,
                        HookContext(
                            run_id=run_id,
                            workspace=resolved_workspace,
                            contract=contract,
                            turn=turns,
                        ),
                    )
                return result

            stop_blocks += 1
            if stop_blocks > contract.budget.max_stop_blocks:
                return await self._budget_result(
                    run_id,
                    turns,
                    tool_call_count,
                    "Completion remained unproven after the Stop Gate retry limit",
                )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "<completion_gate_blocked>"
                        + decision.reason
                        + " Continue working and provide concrete tool evidence."
                        + "</completion_gate_blocked>"
                    ),
                }
            )

        return await self._budget_result(
            run_id, turns, tool_call_count, "Maximum model turn budget reached"
        )

    async def _apply_control(
        self,
        control: SteeringInbox,
        messages: list[dict[str, Any]],
        *,
        run_id: str,
        plan_manager: PlanManager | None,
        task_graph_manager: TaskGraphManager | None,
        turns: int,
        tool_calls: int,
        tool_context: ToolContext,
    ) -> AgentResult | None:
        controls = await control.consume_current()
        for item in controls:
            if item.intent == ControlIntent.INTERRUPT:
                result = AgentResult(
                    status=AgentStatus.INTERRUPTED,
                    run_id=run_id,
                    turns=turns,
                    tool_calls=tool_calls,
                    reason="Interrupted by the user at a safe Agent boundary",
                )
                await self.event_sink.emit(
                    run_id, "agent.interrupted", {"control_sequence": item.sequence}
                )
                return result
            if item.intent == ControlIntent.STEER:
                if plan_manager is not None:
                    await plan_manager.supersede(item.content)
                if task_graph_manager is not None:
                    await task_graph_manager.supersede(item.content)
                tool_context.state["coding_outcome"] = None
                tool_context.state["user_question"] = None
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"<user_steering>{item.content}</user_steering>\n"
                            "Revise the visible plan and task graph before continuing. The frozen "
                            "task contract and host permissions remain authoritative."
                        ),
                    }
                )
                await self.event_sink.emit(
                    run_id,
                    "agent.steered",
                    {"control_sequence": item.sequence, "message": item.content},
                )
        return None

    async def _execute_tool_calls(
        self,
        calls: list[ToolCall],
        context: ToolContext,
        *,
        control: SteeringInbox | None,
    ) -> tuple[list[ToolResult], bool]:
        """Preserve safe read concurrency while checking control after each mutation."""
        results: list[ToolResult] = []
        index = 0
        while index < len(calls):
            tool = self.executor.registry.get(calls[index].name)
            if tool is not None and tool.concurrency_safe:
                batch_end = index + 1
                while batch_end < len(calls):
                    candidate = self.executor.registry.get(calls[batch_end].name)
                    if candidate is None or not candidate.concurrency_safe:
                        break
                    batch_end += 1
                results.extend(await self.executor.execute_many(calls[index:batch_end], context))
                index = batch_end
            else:
                results.append(await self.executor.execute(calls[index], context))
                index += 1

            if self._has_current_control(control):
                results.extend(
                    ToolResult(
                        tool_call_id=call.id,
                        tool_name=call.name,
                        ok=False,
                        output="Cancelled before execution by a user control message",
                    )
                    for call in calls[index:]
                )
                return results, True
        return results, False

    @staticmethod
    def _has_current_control(control: SteeringInbox | None) -> bool:
        return control is not None and any(
            item.intent in {ControlIntent.STEER, ControlIntent.INTERRUPT}
            for item in control.pending
        )

    async def _inject_background_notifications(
        self,
        manager: BackgroundTaskManager,
        messages: list[dict[str, Any]],
        *,
        run_id: str,
        tool_context: ToolContext,
    ) -> None:
        for job in await manager.collect_completed():
            sequence = await tool_context.next_operation()
            start_sequence = tool_context.state.get("background_verifications", {}).pop(
                job.job_id, None
            )
            if (
                job.status == BackgroundStatus.COMPLETED
                and job.exit_code == 0
                and job.verification_id
                and start_sequence is not None
                and tool_context.state.get("verification_attempt", {}).get(job.verification_id)
                == start_sequence
            ):
                tool_context.state["verification_success"][job.verification_id] = start_sequence
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "<task_notification>"
                        f"<task_id>{job.job_id}</task_id>"
                        f"<status>{job.status}</status>"
                        f"<exit_code>{job.exit_code}</exit_code>"
                        f"<summary>{job.output}</summary>"
                        "</task_notification>"
                    ),
                }
            )
            await self.event_sink.emit(
                run_id,
                "background.notification",
                {
                    "job_id": job.job_id,
                    "status": job.status,
                    "exit_code": job.exit_code,
                    "verification_id": job.verification_id,
                    "operation_sequence": sequence,
                },
            )

    async def _budget_result(
        self,
        run_id: str,
        turns: int,
        tool_calls: int,
        reason: str,
    ) -> AgentResult:
        result = AgentResult(
            status=AgentStatus.BUDGET_EXHAUSTED,
            run_id=run_id,
            turns=turns,
            tool_calls=tool_calls,
            reason=reason,
        )
        await self.event_sink.emit(run_id, "agent.budget_exhausted", result.model_dump(mode="json"))
        return result

    @staticmethod
    def _goal_input(contract: TaskContract, context: ToolContext) -> GoalEvaluationInput:
        outcome = context.state.get("coding_outcome")
        if outcome is None:
            raise GoalEvaluationError("Deterministic gate did not provide a coding outcome")
        evidence: dict[str, str] = {
            "outcome": "Agent self-report; schema validated, not independent correctness evidence"
        }
        host_evidence_ids: list[str] = []
        last_mutation = int(context.state.get("last_mutation_sequence", 0))
        verification_success = context.state.get("verification_success", {})
        for command_id, sequence in list(verification_success.items())[:200]:
            if sequence <= last_mutation:
                continue
            evidence[f"verification:{command_id}"] = (
                f"Verification succeeded at operation sequence {sequence}"
            )
            host_evidence_ids.append(f"verification:{command_id}")
        browser_results = context.state.get("browser_verification_results", {})
        for verification_id, result in list(browser_results.items())[:200]:
            if verification_success.get(verification_id, 0) <= last_mutation:
                continue
            if result.status != "passed":
                continue
            evidence_id = f"browser:{verification_id}"
            evidence[evidence_id] = json.dumps(
                {
                    "status": result.status,
                    "attempt_id": result.attempt_id,
                    "executed": result.executed,
                    "passed": result.passed,
                    "failed": result.failed,
                    "input_fingerprint": result.input_fingerprint,
                    "suite_fingerprint": result.suite_fingerprint,
                    "report_hash": result.report_hash,
                    "evidence_refs": [str(ref)[:500] for ref in result.evidence_refs[:5]],
                },
                ensure_ascii=False,
            )
            host_evidence_ids.append(evidence_id)
        for evidence_id, excerpt in context.state.get("file_observations", {}).items():
            evidence[evidence_id] = excerpt
            host_evidence_ids.append(evidence_id)
        if context.plan_manager is not None and context.plan_manager.current is not None:
            plan = context.plan_manager.current
            for item_index, item in enumerate(plan.items):
                for evidence_index, value in enumerate(item.evidence):
                    if len(evidence) >= 500:
                        break
                    evidence[f"plan:{plan.version}:{item_index}:{evidence_index}"] = (
                        "Agent plan self-report: " + value[:1000]
                    )
        if (
            context.task_graph_manager is not None
            and context.task_graph_manager.current is not None
        ):
            graph = context.task_graph_manager.current
            for task in graph.tasks:
                for evidence_index, value in enumerate(task.evidence):
                    if len(evidence) >= 500:
                        break
                    evidence[f"task:{task.task_id}:{evidence_index}"] = (
                        "Agent task self-report: " + value[:1000]
                    )
        return GoalEvaluationInput(
            objective=contract.objective,
            acceptance_criteria=contract.acceptance_criteria,
            outcome_summary=outcome.summary,
            changed_files=outcome.changed_files,
            risk_notes=outcome.risk_notes,
            available_evidence=evidence,
            host_evidence_ids=host_evidence_ids,
            has_mutations=last_mutation > 0,
        )

    @staticmethod
    def _messages_size(messages: list[dict[str, Any]]) -> int:
        return sum(
            len(json.dumps(message, ensure_ascii=False, default=str)) for message in messages
        )

    @staticmethod
    def _assistant_message(
        content: str,
        reasoning_content: str | None,
        calls: list[ToolCall],
    ) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": content or None}
        if reasoning_content is not None:
            message["reasoning_content"] = reasoning_content
        if calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in calls
            ]
        return message

    @staticmethod
    def _tool_messages(results: list[ToolResult]) -> list[dict[str, Any]]:
        return [
            {
                "role": "tool",
                "tool_call_id": result.tool_call_id,
                "content": json.dumps(
                    {
                        "ok": result.ok,
                        "output": result.output,
                        "data": result.data,
                        "policy_denied": result.policy_denied,
                    },
                    ensure_ascii=False,
                ),
            }
            for result in results
        ]
