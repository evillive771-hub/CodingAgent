import asyncio

import pytest

from aicoding_agent.domain.models import Budget
from aicoding_agent.regression.budget import RegressionBudget, RegressionBudgetExceeded


async def test_two_restored_workers_cannot_overspend_shared_budget(tmp_path):
    limits = Budget(max_turns=1, max_tool_calls=2)
    left = await RegressionBudget.initialize(tmp_path, "run", limits)
    right = await RegressionBudget.initialize(tmp_path, "run", limits)
    outcomes = await asyncio.gather(left.charge_turn(), right.charge_turn(),
                                    return_exceptions=True)
    assert sum(isinstance(item, RegressionBudgetExceeded) for item in outcomes) == 1
    await left.charge_tools()
    await right.charge_tools()
    resumed = await RegressionBudget.initialize(tmp_path, "run", limits)
    with pytest.raises(RegressionBudgetExceeded):
        await resumed.charge_tools()
    assert (await resumed.refresh()).tool_calls == 2
    with pytest.raises(ValueError, match="cannot replace"):
        await RegressionBudget.initialize(tmp_path, "run", Budget())


async def test_browser_counts_and_original_deadline_survive_restart(tmp_path):
    budget = await RegressionBudget.initialize(tmp_path, "run", Budget())
    await budget.reserve_browser("history", 1)
    resumed = await RegressionBudget.initialize(tmp_path, "run", Budget())
    with pytest.raises(RegressionBudgetExceeded, match="Browser execution"):
        await resumed.reserve_browser("history", 1)
    payload = await budget.store.load("run")
    payload["deadline_epoch"] = 0
    await budget.store.save("run", payload)
    expired = await RegressionBudget.initialize(tmp_path, "run", Budget())
    with pytest.raises(RegressionBudgetExceeded, match="time budget"):
        await expired.reserve_command("old")
    assert (await expired.refresh()).command_runs == {}
