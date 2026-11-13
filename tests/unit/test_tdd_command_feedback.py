import json
import sys
from pathlib import Path
from types import SimpleNamespace

from aicoding_agent.domain.models import TaskContract, ToolCall, VerificationCommand
from aicoding_agent.tools.base import ToolContext
from aicoding_agent.tools.builtin import (
    RunProcessInput,
    RunProcessTool,
    StartBackgroundProcessInput,
    StartBackgroundProcessTool,
    build_builtin_registry,
)
from aicoding_agent.tools.executor import ToolExecutor


async def test_frozen_host_command_reports_red_and_green_in_candidate(tmp_path, monkeypatch):
    candidate, host = tmp_path / "candidate", tmp_path / "host"
    candidate.mkdir()
    host.mkdir()
    (candidate / "value.txt").write_text("0", encoding="utf-8")
    test = host / "check.py"
    test.write_text(
        "import os\nfrom pathlib import Path\n"
        "assert 'MODEL_API_KEY' not in os.environ\n"
        "assert os.environ['EVAL_PROJECT_ROOT'] == os.environ['REGRESSION_WORKSPACE']\n"
        "value = Path(os.environ['REGRESSION_WORKSPACE'])/'value.txt'\n"
        "assert value.read_text() == '1', 'needs clamp'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODEL_API_KEY", "not-a-real-key")
    monkeypatch.setenv("REGRESSION_WORKSPACE", "wrong-inherited-workspace")
    command = VerificationCommand(id="unit", program="python", args=[str(test)])
    contract = TaskContract(
        task_id="clamp", objective="Fix clamp", acceptance_criteria=["Clamp"],
        verification_commands=[command], allowed_tools={"run_process"}, allowed_commands={"python"},
    )
    context = ToolContext(workspace=candidate, contract=contract)
    executor = ToolExecutor(build_builtin_registry())
    call = ToolCall(
        id="check", name="run_process", arguments={"program": "python", "args": [str(test)]}
    )
    red = await executor.execute(call, context)
    assert red.ok
    assert red.data["exit_code"] == 1
    assert "AssertionError: needs clamp" in red.output
    assert "KeyError" not in red.output
    (candidate / "value.txt").write_text("1", encoding="utf-8")
    green = await executor.execute(call, context)
    assert green.ok and green.data["exit_code"] == 0


async def test_nonverification_process_does_not_receive_workspace_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("REGRESSION_WORKSPACE", "wrong")
    monkeypatch.setenv("EVAL_PROJECT_ROOT", "wrong")
    contract = TaskContract(task_id="read", objective="Read", acceptance_criteria=["Read"])
    context = ToolContext(workspace=tmp_path, contract=contract)
    result = await RunProcessTool().run(RunProcessInput(
        program="python", args=["-c", (
            "import os,json; print(json.dumps({k:v for k,v in os.environ.items() "
            "if k in ['REGRESSION_WORKSPACE','EVAL_PROJECT_ROOT']}))"
        )],
    ), context)
    assert json.loads(result.output.split("\n", 1)[1]) == {}


async def test_background_verification_receives_same_frozen_workspace_env(tmp_path, monkeypatch):
    observed = {}

    class Background:
        async def start(self, **kwargs):
            observed.update(kwargs)
            return SimpleNamespace(job_id="test", status="running")

    monkeypatch.setenv("MODEL_API_KEY", "not-a-real-key")
    command = VerificationCommand(id="unit", program="python", args=["test.py"])
    contract = TaskContract(
        task_id="clamp", objective="Fix", acceptance_criteria=["Pass"],
        verification_commands=[command],
    )
    context = ToolContext(workspace=tmp_path, contract=contract, background_manager=Background())
    await StartBackgroundProcessTool().run(
        StartBackgroundProcessInput(program="python", args=["test.py"]), context,
    )
    assert observed["env"]["REGRESSION_WORKSPACE"] == str(tmp_path.resolve())
    assert observed["env"]["EVAL_PROJECT_ROOT"] == str(tmp_path.resolve())
    assert "MODEL_API_KEY" not in observed["env"]


async def test_verification_python_uses_agent_virtual_environment(tmp_path):
    command = VerificationCommand(id="runtime", program="python",
                                  args=["-c", "import sys; print(sys.executable)"])
    task = TaskContract(task_id="runtime", objective="Test", acceptance_criteria=["Active Python"],
                        verification_commands=[command])
    result = await RunProcessTool().run(
        RunProcessInput(program=command.program, args=command.args),
        ToolContext(workspace=tmp_path, contract=task),
    )
    reported = result.output.split("\n", 1)[1].strip()
    assert Path(reported).resolve() == Path(sys.executable).resolve()
