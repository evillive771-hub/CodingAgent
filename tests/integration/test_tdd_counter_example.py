import os
import shutil
from pathlib import Path

import pytest
from conftest import ScriptedModel

from aicoding_agent.adapters.browser_tests import BrowserTestRunner
from aicoding_agent.agent.loop import AgentLoop
from aicoding_agent.domain.models import ModelResponse, TaskContract, ToolCall
from aicoding_agent.regression.config import build_controller
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor


async def test_example_real_loop_and_host_both_require_red_then_green(tmp_path):
    if not os.environ.get("CODINGAGENT_PLAYWRIGHT_TEST_PACKAGE"):
        pytest.skip("Real browser example requires an existing installed runtime")
    root = Path(__file__).parents[2]
    sample = root / "examples" / "tdd_counter"
    workspace = tmp_path / "workspace"
    shutil.copytree(root / "examples" / "browser_feedback" / "workspace", workspace)
    task = TaskContract.model_validate_json((sample / "task.json").read_text(encoding="utf-8"))
    runner = await BrowserTestRunner.from_config(
        sample / "browser-suites.json", tmp_path / "browser",
    )
    controller = await build_controller(sample / "regression.json", tmp_path / "runtime", runner)

    def call(name, **kwargs):
        return ModelResponse(tool_calls=[ToolCall(id=name, name=name, arguments=kwargs)])

    command = task.verification_commands[0]
    class Model(ScriptedModel):
        async def complete(self, messages, tools):
            if not self.responses:
                raise AssertionError(str(messages[-7:]))
            return await super().complete(messages, tools)

    model = Model([
        call("read_file", path="counter.js"),
        call("apply_text_patch", path="counter.js", old_text="return value - 1;",
             new_text="return Math.max(0, value - 1);"),
        call("run_process", program=command.program, args=command.args, cwd=command.cwd),
        call("run_playwright", verification_id="counter-e2e"),
        call("submit_result", summary="Both requirement suites pass", changed_files=["counter.js"]),
    ])
    agent = AgentLoop(model=model, executor=ToolExecutor(build_builtin_registry()),
                      browser_runner=runner)
    result = await controller.run(task, workspace, run_id="counter", agent=agent, adopt=True)
    assert result.status == "accepted", result.reason
    assert result.tdd_evidence.red_report.status == "assertion_failed"
    assert len(result.report.tests) == 2 and all(t.status == "passed" for t in result.report.tests)
    assert result.agent_result.turns == 5
    assert "Math.max(0, value - 1)" in (workspace / "counter.js").read_text()
    assert await controller.validate_checkpoint("counter")
