import asyncio
from pathlib import Path

import pytest

from aicoding_agent.domain.models import TaskContract, ToolCall, ToolRisk
from aicoding_agent.tools.base import (
    BaseTool,
    ToolContext,
    ToolExecution,
    ToolInput,
    ToolRegistry,
)
from aicoding_agent.tools.executor import ToolExecutor


class ProbeTool(BaseTool[ToolInput]):
    input_model = ToolInput
    risk = ToolRisk.READ

    def __init__(self, name: str, *, concurrency_safe: bool) -> None:
        self.name = name
        self.description = "Probe executor scheduling"
        self.concurrency_safe = concurrency_safe
        self.active = 0
        self.max_active = 0
        self.two_running = asyncio.Event()

    async def run(self, arguments: ToolInput, context: ToolContext) -> ToolExecution:
        del arguments, context
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.active == 2:
            self.two_running.set()
        if self.concurrency_safe:
            await asyncio.wait_for(self.two_running.wait(), timeout=1)
        else:
            await asyncio.sleep(0.01)
        self.active -= 1
        return ToolExecution(output="done")


def context(tmp_path: Path, *allowed_tools: str) -> ToolContext:
    return ToolContext(
        workspace=tmp_path,
        contract=TaskContract(
            task_id="executor-concurrency",
            objective="Verify scheduling",
            acceptance_criteria=["Read tools overlap and mutations do not"],
            allowed_tools=set(allowed_tools),
        ),
    )


@pytest.mark.asyncio
async def test_concurrency_safe_tools_execute_in_parallel(tmp_path: Path) -> None:
    registry = ToolRegistry()
    probe = ProbeTool("read_probe", concurrency_safe=True)
    registry.register(probe)

    results = await ToolExecutor(registry).execute_many(
        [
            ToolCall(id="read-1", name="read_probe", arguments={}),
            ToolCall(id="read-2", name="read_probe", arguments={}),
        ],
        context(tmp_path, "read_probe"),
    )

    assert all(result.ok for result in results)
    assert probe.max_active == 2


@pytest.mark.asyncio
async def test_mutating_tools_remain_serialized(tmp_path: Path) -> None:
    registry = ToolRegistry()
    probe = ProbeTool("write_probe", concurrency_safe=False)
    probe.risk = ToolRisk.WRITE
    registry.register(probe)

    results = await ToolExecutor(registry).execute_many(
        [
            ToolCall(id="write-1", name="write_probe", arguments={}),
            ToolCall(id="write-2", name="write_probe", arguments={}),
        ],
        context(tmp_path, "write_probe"),
    )

    assert all(result.ok for result in results)
    assert probe.max_active == 1
