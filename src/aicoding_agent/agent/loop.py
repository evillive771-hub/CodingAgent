from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from aicoding_agent.agent.background import BackgroundStatus, BackgroundTaskManager
from aicoding_agent.agent.control import ControlIntent, SteeringInbox
from aicoding_agent.agent.gate import CompletionGate, GateAction
from aicoding_agent.agent.hooks import AsyncHookBus, HookContext, HookEvent
from aicoding_agent.agent.plan import PlanManager
from aicoding_agent.context.archive import ContextEvidenceArchive
from aicoding_agent.context.builder import ContextBuilder, compact_messages
from aicoding_agent.domain.models import (
    AgentResult,
    AgentStatus,
    TaskContract,
    ToolCall,
    ToolResult,
)
from aicoding_agent.model.base import ChatModel, ModelError
from aicoding_agent.persistence.base import EventSink, NullEventSink
from aicoding_agent.tools.base import ToolContext
from aicoding_agent.tools.executor import ToolExecutor


class AgentLoop:
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

    async def run(
        self,
        contract: TaskContract,
        workspace: Path,
        *,
        run_id: str | None = None,
        conversation_history: list[dict[str, str]] | None = None,
        plan_manager: PlanManager | None = None,
        control: SteeringInbox | None = None,
        background_manager: BackgroundTaskManager | None = None,
    ) -> AgentResult:
        resolved_workspace = workspace.resolve()
        if not resolved_workspace.is_dir():
            raise ValueError(f"Workspace does not exist: {resolved_workspace}")
        run_id = run_id or uuid4().hex
        background_manager = background_manager or self.background_manager
        started_at = time.monotonic()
        tool_context = ToolContext(
            workspace=resolved_workspace,
            contract=contract,
            emit=lambda event_type, data: self.event_sink.emit(run_id, event_type, data),
            plan_manager=plan_manager,
            run_id=run_id,
            background_manager=background_manager,
        )
        if plan_manager is not None:
            await plan_manager.initialize()
        if control is not None:
            await control.initialize()
        if background_manager is not None:
            await background_manager.initialize()
        messages = self.context_builder.build(
            contract,
            resolved_workspace,
            conversation_history=conversation_history,
            active_plan=plan_manager.render() if plan_manager is not None else None,
        )
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
        tool_definitions = self.executor.registry.definitions(contract.allowed_tools)
        turns = 0
        tool_call_count = 0
        stop_blocks = 0

        await self.event_sink.emit(
            run_id,
            "agent.started",
            {
                "task_id": contract.task_id,
                "workspace": str(resolved_workspace),
                "allowed_tools": sorted(contract.allowed_tools),
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
            await self.event_sink.emit(run_id, "agent.turn_started", {"turn": turns})
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
            try:
                model_messages = (
                    await self.context_archive.compact(
                        messages,
                        run_id=run_id,
                        max_chars=self.context_max_chars,
                    )
                    if self.context_archive is not None
                    else compact_messages(messages, max_chars=self.context_max_chars)
                )
                response = await self.model.complete(model_messages, tool_definitions)
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
            await self.event_sink.emit(
                run_id,
                "model.response",
                {
                    "turn": turns,
                    "content": response.content,
                    "tool_names": [call.name for call in response.tool_calls],
                    "usage": response.usage,
                    "finish_reason": response.finish_reason,
                },
            )

            if self._has_current_control(control):
                if response.tool_calls:
                    tool_call_count += len(response.tool_calls)
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
                control_result = await self._apply_control(
                    control,
                    messages,
                    run_id=run_id,
                    plan_manager=plan_manager,
                    turns=turns,
                    tool_calls=tool_call_count,
                    tool_context=tool_context,
                )
                if control_result is not None:
                    return control_result
                continue

            if response.tool_calls:
                if tool_call_count + len(response.tool_calls) > contract.budget.max_tool_calls:
                    return await self._budget_result(
                        run_id, turns, tool_call_count, "Maximum tool call budget reached"
                    )
                tool_call_count += len(response.tool_calls)
                results, control_pending = await self._execute_tool_calls(
                    response.tool_calls,
                    tool_context,
                    control=control,
                )
                messages.extend(self._tool_messages(results))
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
                        turns=turns,
                        tool_calls=tool_call_count,
                        tool_context=tool_context,
                    )
                    if control_result is not None:
                        return control_result
                    continue
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
            decision = self.completion_gate.evaluate(contract, tool_context)
            await self.event_sink.emit(
                run_id,
                "agent.stop_evaluated",
                {"turn": turns, "action": decision.action, "reason": decision.reason},
            )
            if decision.action == GateAction.COMPLETE:
                outcome = tool_context.state.get("coding_outcome")
                result = AgentResult(
                    status=AgentStatus.COMPLETED,
                    run_id=run_id,
                    final_text=response.content,
                    outcome=outcome,
                    turns=turns,
                    tool_calls=tool_call_count,
                )
                await self.event_sink.emit(
                    run_id, "agent.completed", result.model_dump(mode="json")
                )
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
                tool_context.state["coding_outcome"] = None
                tool_context.state["user_question"] = None
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"<user_steering>{item.content}</user_steering>\n"
                            "Revise the visible plan before continuing. The frozen task contract "
                            "and host permissions remain authoritative."
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
            if (
                job.status == BackgroundStatus.COMPLETED
                and job.exit_code == 0
                and job.verification_id
            ):
                tool_context.state["verification_success"][job.verification_id] = sequence
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
