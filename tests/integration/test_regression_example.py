import asyncio
import shutil
from pathlib import Path

import pytest

from aicoding_agent.domain.models import TaskContract
from aicoding_agent.regression.config import build_controller


async def test_counter_example_command_rejects_bug_and_accepts_actual_fix(tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node is required for the frontend example")
    sample = Path(__file__).parents[2] / "examples" / "browser_feedback"
    contract = TaskContract.model_validate_json(
        (sample / "task-regression.json").read_text(encoding="utf-8"),
    )
    controller = await build_controller(sample / "regression.json", tmp_path / "runtime")
    assert controller.gate.require_both is True
    assert contract.verification_commands
    assert any(v.required for v in contract.browser_verifications)
    source = (sample / "workspace" / "counter.js").read_text(encoding="utf-8")
    target = tmp_path / "counter.js"
    command = contract.verification_commands[0]

    async def execute():
        process = await asyncio.create_subprocess_exec(
            *command.argv, cwd=tmp_path, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, error = await process.communicate()
        return process.returncode, output, error

    target.write_text(source, encoding="utf-8")
    failed, _, error = await execute()
    assert failed != 0 and b"AssertionError" in error
    target.write_text(source.replace("return value - 1;", "return Math.max(0, value - 1);"),
                      encoding="utf-8")
    passed, _, error = await execute()
    assert passed == 0, error.decode()
