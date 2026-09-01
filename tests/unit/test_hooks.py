from pathlib import Path

import pytest

from aicoding_agent.agent.hooks import (
    AsyncHookBus,
    HookContext,
    HookEvent,
    HookResponse,
)
from aicoding_agent.domain.models import TaskContract, ToolCall
from aicoding_agent.tools.base import ToolContext
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor


def contract() -> TaskContract:
    return TaskContract(
        task_id="hook-test",
        objective="Exercise hooks",
        acceptance_criteria=["Hooks run in order"],
    )


@pytest.mark.asyncio
async def test_async_hook_bus_combines_injections_in_registration_order(tmp_path: Path) -> None:
    bus = AsyncHookBus()

    async def first(context: HookContext) -> HookResponse:
        return HookResponse(inject_messages=(f"first:{context.turn}",))

    async def second(context: HookContext) -> HookResponse:
        return HookResponse(inject_messages=("second",), continue_message="continue")

    bus.register(HookEvent.BEFORE_MODEL_CALL, first)
    bus.register(HookEvent.BEFORE_MODEL_CALL, second)

    result = await bus.trigger(
        HookEvent.BEFORE_MODEL_CALL,
        HookContext(run_id="run", workspace=tmp_path, contract=contract(), turn=2),
    )

    assert result.inject_messages == ("first:2", "second")
    assert result.continue_message == "continue"


@pytest.mark.asyncio
async def test_host_policy_denial_happens_before_pre_tool_hook(tmp_path: Path) -> None:
    bus = AsyncHookBus()
    hook_calls = 0

    async def hook(context: HookContext) -> None:
        nonlocal hook_calls
        hook_calls += 1

    bus.register(HookEvent.PRE_TOOL_USE, hook)
    task = contract().model_copy(update={"allowed_tools": {"submit_result"}})
    executor = ToolExecutor(build_builtin_registry(), hook_bus=bus)

    result = await executor.execute(
        ToolCall(id="read", name="read_file", arguments={"path": "README.md"}),
        ToolContext(workspace=tmp_path, contract=task),
    )

    assert result.policy_denied
    assert hook_calls == 0


@pytest.mark.asyncio
async def test_pre_tool_hook_can_reduce_but_not_expand_permission(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("safe", encoding="utf-8")
    bus = AsyncHookBus()

    async def block(context: HookContext) -> HookResponse:
        return HookResponse(block_reason="maintenance window")

    bus.register(HookEvent.PRE_TOOL_USE, block)
    executor = ToolExecutor(build_builtin_registry(), hook_bus=bus)

    result = await executor.execute(
        ToolCall(id="read", name="read_file", arguments={"path": "README.md"}),
        ToolContext(workspace=tmp_path, contract=contract()),
    )

    assert result.policy_denied
    assert "maintenance window" in result.output
