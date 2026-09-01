from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from aicoding_agent.agent.gate import CompletionGate, GateAction
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
    ) -> None:
        self.model = model
        self.executor = executor
        self.context_builder = context_builder or ContextBuilder()
        self.completion_gate = completion_gate or CompletionGate()
        self.event_sink = event_sink or NullEventSink()
        self.context_max_chars = context_max_chars

    async def run(
        self,
        contract: TaskContract,
        workspace: Path,
        *,
        run_id: str | None = None,
        conversation_history: list[dict[str, str]] | None = None,
    ) -> AgentResult:
        resolved_workspace = workspace.resolve()
        if not resolved_workspace.is_dir():
            raise ValueError(f"Workspace does not exist: {resolved_workspace}")
        run_id = run_id or uuid4().hex
        started_at = time.monotonic()
        tool_context = ToolContext(
            workspace=resolved_workspace,
            contract=contract,
            emit=lambda event_type, data: self.event_sink.emit(run_id, event_type, data),
        )
        messages = self.context_builder.build(
            contract,
            resolved_workspace,
            conversation_history=conversation_history,
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
            if time.monotonic() - started_at > contract.budget.max_elapsed_seconds:
                return await self._budget_result(
                    run_id, turns, tool_call_count, "Maximum elapsed time reached"
                )
            turns += 1
            await self.event_sink.emit(run_id, "agent.turn_started", {"turn": turns})
            try:
                response = await self.model.complete(
                    compact_messages(messages, max_chars=self.context_max_chars),
                    tool_definitions,
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

            if response.tool_calls:
                if tool_call_count + len(response.tool_calls) > contract.budget.max_tool_calls:
                    return await self._budget_result(
                        run_id, turns, tool_call_count, "Maximum tool call budget reached"
                    )
                tool_call_count += len(response.tool_calls)
                results = await self.executor.execute_many(response.tool_calls, tool_context)
                messages.extend(self._tool_messages(results))
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
