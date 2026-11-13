from pathlib import Path

import pytest

from aicoding_agent.agent.loop import AgentLoop
from aicoding_agent.domain.models import Budget, ModelResponse, TaskContract, ToolCall
from aicoding_agent.regression.budget import RegressionBudget
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor


class Model:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        return ModelResponse(tool_calls=[ToolCall(
            id="submit", name="submit_result", arguments={"summary": "done", "changed_files": []},
        )])


@pytest.mark.asyncio
async def test_model_budget_does_not_reset_for_new_candidate(tmp_path: Path):
    contract = TaskContract(
        task_id="retry", objective="Check retry", acceptance_criteria=["Limited"],
        allowed_tools={"submit_result"}, budget=Budget(max_turns=1),
    )
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    budget = await RegressionBudget.initialize(tmp_path / "budget", "shared", contract.budget)
    model = Model()
    loop = AgentLoop(model=model, executor=ToolExecutor(build_builtin_registry()))
    result = await loop.run(contract, first, run_id="first", shared_budget=budget)
    assert result.status == "completed"
    resumed = await RegressionBudget.initialize(tmp_path / "budget", "shared", contract.budget)
    result = await loop.run(contract, second, run_id="second", shared_budget=resumed)
    assert result.status == "budget_exhausted"
    assert model.calls == 1
