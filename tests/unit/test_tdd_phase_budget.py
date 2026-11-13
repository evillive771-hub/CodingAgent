from aicoding_agent.domain.models import Budget
from aicoding_agent.regression.budget import RegressionBudget


async def test_red_and_green_share_totals_but_keep_persisted_phase_counts(tmp_path):
    budget = await RegressionBudget.initialize(tmp_path, "task", Budget())
    budget.phase = "red_verify"
    await budget.reserve_command("unit")
    budget.phase = "green_verify"
    await budget.reserve_command("unit")
    recovered = await RegressionBudget.initialize(tmp_path, "task", Budget())
    state = await recovered.refresh()
    assert state.tool_calls == 2 and state.command_runs["unit"] == 2
    assert state.phase_counts["red_verify:command:unit"] == 1
    assert state.phase_counts["green_verify:command:unit"] == 1
