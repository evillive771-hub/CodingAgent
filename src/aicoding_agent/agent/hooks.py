from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from aicoding_agent.domain.models import TaskContract, ToolCall, ToolResult


class HookEvent(StrEnum):
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    BEFORE_MODEL_CALL = "before_model_call"
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    BEFORE_STOP = "before_stop"
    AFTER_TURN = "after_turn"


@dataclass(frozen=True, slots=True)
class HookContext:
    run_id: str
    workspace: Path
    contract: TaskContract
    turn: int = 0
    user_message: str | None = None
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HookResponse:
    block_reason: str | None = None
    inject_messages: tuple[str, ...] = ()
    continue_message: str | None = None


AsyncHook = Callable[[HookContext], Awaitable[HookResponse | None]]


class AsyncHookBus:
    """Typed async extension points. Host policy remains authoritative."""

    def __init__(self) -> None:
        self._hooks: dict[HookEvent, list[AsyncHook]] = {event: [] for event in HookEvent}

    def register(self, event: HookEvent, callback: AsyncHook) -> None:
        self._hooks[event].append(callback)

    async def trigger(self, event: HookEvent, context: HookContext) -> HookResponse:
        injected: list[str] = []
        continuation: str | None = None
        for callback in self._hooks[event]:
            response = await callback(context)
            if response is None:
                continue
            injected.extend(response.inject_messages)
            continuation = response.continue_message or continuation
            if response.block_reason:
                return HookResponse(
                    block_reason=response.block_reason,
                    inject_messages=tuple(injected),
                    continue_message=continuation,
                )
        return HookResponse(
            inject_messages=tuple(injected),
            continue_message=continuation,
        )
