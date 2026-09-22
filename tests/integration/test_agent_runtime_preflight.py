from pathlib import Path

import pytest
from conftest import ScriptedModel

from aicoding_agent.agent.loop import AgentLoop
from aicoding_agent.domain.models import ModelResponse, TaskContract, ToolCall
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
async def test_runtime_observations_reach_model_and_audit_on_start_and_resume(
    tmp_path: Path, resume: bool
) -> None:
    events = []

    class Sink:
        async def emit(self, run_id, event_type, data):
            events.append((event_type, data))

    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit", name="submit_result", arguments={"summary": "Read-only review"}
                    )
                ]
            )
        ]
    )
    contract = TaskContract(
        task_id="runtime-context", objective="Review", acceptance_criteria=["Preserve behavior"]
    )
    result = await AgentLoop(
        model=model, executor=ToolExecutor(build_builtin_registry()), event_sink=Sink()
    ).run(
        contract,
        tmp_path,
        resume_messages=[{"role": "system", "content": "original"}] if resume else None,
    )
    assert result.status == "completed"
    assert any("<runtime_capabilities>" in str(msg) for msg in model.requests[0][0])
    facts = [data for kind, data in events if kind == "agent.runtime_preflight"]
    assert len(facts) == 1
    assert facts[0]["verification_command_count"] == 0
    assert facts[0]["workspace_root_node_modules"] is False
