from conftest import ScriptedModel

from aicoding_agent.agent.loop import AgentLoop
from aicoding_agent.domain.models import TaskContract
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor


async def test_direct_loop_cannot_bypass_host_red_gate(tmp_path):
    model = ScriptedModel([])
    contract = TaskContract(task_id="strict", objective="Fix", acceptance_criteria=["Pass"],
                            tdd={"red_cases": {"ui": ["zero"]}})
    result = await AgentLoop(model=model, executor=ToolExecutor(build_builtin_registry())).run(
        contract, tmp_path, run_id="unauthorized",
    )
    assert result.status == "blocked"
    assert "RED" in result.reason
    assert not model.requests
