from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from aicoding_agent.domain.models import ToolCall, ToolResult
from aicoding_agent.tools.base import ToolContext, ToolError, ToolRegistry
from aicoding_agent.tools.policy import PolicyBehavior, PolicyEngine

ToolHook = Callable[[ToolCall, ToolResult | None], Awaitable[str | None]]


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        policy: PolicyEngine | None = None,
        pre_hooks: list[ToolHook] | None = None,
        post_hooks: list[ToolHook] | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy or PolicyEngine()
        self.pre_hooks = pre_hooks or []
        self.post_hooks = post_hooks or []
        self._mutation_lock = asyncio.Lock()

    async def execute_many(
        self,
        calls: list[ToolCall],
        context: ToolContext,
    ) -> list[ToolResult]:
        results: list[ToolResult | None] = [None] * len(calls)
        read_batch: list[tuple[int, ToolCall]] = []

        async def flush_read_batch() -> None:
            if not read_batch:
                return
            completed = await asyncio.gather(
                *(self.execute(call, context) for _, call in read_batch)
            )
            for (index, _), result in zip(read_batch, completed, strict=True):
                results[index] = result
            read_batch.clear()

        for index, call in enumerate(calls):
            tool = self.registry.get(call.name)
            if tool is not None and tool.concurrency_safe:
                read_batch.append((index, call))
                continue
            await flush_read_batch()
            results[index] = await self.execute(call, context)
        await flush_read_batch()
        return [result for result in results if result is not None]

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        tool = self.registry.get(call.name)
        if tool is None:
            return ToolResult(
                tool_call_id=call.id,
                tool_name=call.name,
                ok=False,
                output=f"Unknown tool: {call.name}",
            )
        try:
            arguments = tool.input_model.model_validate(call.arguments)
        except ValidationError as exc:
            return ToolResult(
                tool_call_id=call.id,
                tool_name=call.name,
                ok=False,
                output=f"Invalid tool arguments: {exc}",
            )

        decision = self.policy.evaluate(
            tool,
            arguments,
            workspace=context.workspace,
            contract=context.contract,
        )
        if decision.behavior != PolicyBehavior.ALLOW:
            result = ToolResult(
                tool_call_id=call.id,
                tool_name=call.name,
                ok=False,
                output=f"Policy {decision.behavior}: {decision.reason}",
                policy_denied=True,
            )
            await self._emit(context, "tool.policy_denied", result.model_dump())
            return result

        for hook in self.pre_hooks:
            blocked = await hook(call, None)
            if blocked:
                return ToolResult(
                    tool_call_id=call.id,
                    tool_name=call.name,
                    ok=False,
                    output=f"PreToolUse hook blocked the call: {blocked}",
                    policy_denied=True,
                )

        operation_sequence = await context.next_operation()
        await self._emit(
            context,
            "tool.started",
            {"tool_call_id": call.id, "tool_name": call.name, "sequence": operation_sequence},
        )
        try:
            if tool.concurrency_safe:
                execution = await tool.run(arguments, context)
            else:
                async with self._mutation_lock:
                    execution = await tool.run(arguments, context)
            result = ToolResult(
                tool_call_id=call.id,
                tool_name=call.name,
                ok=True,
                output=execution.output,
                data={**execution.data, "operation_sequence": operation_sequence},
                is_mutation=execution.is_mutation,
            )
            if execution.is_mutation:
                context.state["last_mutation_sequence"] = operation_sequence
                context.state["coding_outcome"] = None
            verification_id = execution.data.get("verification_id")
            if verification_id and execution.data.get("exit_code") == 0:
                context.state["verification_success"][verification_id] = operation_sequence
        except ToolError as exc:
            result = ToolResult(
                tool_call_id=call.id,
                tool_name=call.name,
                ok=False,
                output=str(exc),
                data={"operation_sequence": operation_sequence},
            )
        except Exception as exc:  # Defensive boundary: never leak internals to the model.
            result = ToolResult(
                tool_call_id=call.id,
                tool_name=call.name,
                ok=False,
                output=f"Tool failed internally: {type(exc).__name__}",
                data={"operation_sequence": operation_sequence},
            )

        for hook in self.post_hooks:
            await hook(call, result)
        await self._emit(context, "tool.finished", result.model_dump())
        return result

    @staticmethod
    async def _emit(context: ToolContext, event_type: str, data: dict[str, Any]) -> None:
        if context.emit is not None:
            await context.emit(event_type, data)
