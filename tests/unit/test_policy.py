from pathlib import Path

import pytest

from aicoding_agent.domain.models import TaskContract, ToolCall, VerificationCommand
from aicoding_agent.tools.base import ToolContext, ToolError
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor
from aicoding_agent.tools.paths import resolve_workspace_path


def test_path_escape_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ToolError):
        resolve_workspace_path(
            tmp_path,
            "../outside.txt",
            allowed_prefixes=["."],
            for_write=True,
        )


@pytest.mark.asyncio
async def test_unapproved_process_is_denied(tmp_path: Path) -> None:
    contract = TaskContract(
        task_id="policy-test",
        objective="Validate process policy",
        acceptance_criteria=["Unsafe process does not run"],
        verification_commands=[
            VerificationCommand(id="tests", program="python", args=["-m", "pytest"])
        ],
    )
    context = ToolContext(workspace=tmp_path, contract=contract)
    executor = ToolExecutor(build_builtin_registry())

    result = await executor.execute(
        ToolCall(
            id="call-1",
            name="run_process",
            arguments={"program": "python", "args": ["-c", "print('unsafe')"]},
        ),
        context,
    )

    assert not result.ok
    assert result.policy_denied
    assert "not an exact verification command" in result.output


@pytest.mark.asyncio
async def test_secret_bearing_file_is_not_readable(tmp_path: Path) -> None:
    (tmp_path / "env.txt").write_text("MIMO_API_KEY=secret", encoding="utf-8")
    contract = TaskContract(
        task_id="secret-policy",
        objective="Do not expose secrets",
        acceptance_criteria=["Secret file is blocked"],
    )
    context = ToolContext(workspace=tmp_path, contract=contract)
    executor = ToolExecutor(build_builtin_registry())

    result = await executor.execute(
        ToolCall(id="call-secret", name="read_file", arguments={"path": "env.txt"}),
        context,
    )

    assert not result.ok
    assert result.policy_denied
    assert "protected or secret-bearing" in result.output
