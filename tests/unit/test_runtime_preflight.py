from pathlib import Path

import pytest

from aicoding_agent.agent.preflight import inspect_runtime, render_runtime
from aicoding_agent.context.builder import compact_messages
from aicoding_agent.domain.models import TaskContract, VerificationCommand


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [PermissionError("denied"), RuntimeError("symlink loop")])
async def test_unreadable_paths_are_unknown_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def inaccessible(*args, **kwargs):
        raise error

    contract = TaskContract(
        task_id="inaccessible",
        objective="Review",
        acceptance_criteria=["Preserve behavior"],
        verification_commands=[VerificationCommand(id="tests", program="python")],
    )
    monkeypatch.setattr(Path, "exists", inaccessible)
    monkeypatch.setattr(Path, "is_dir", inaccessible)
    facts = await inspect_runtime(tmp_path, contract, registered_tools={"run_process"})
    assert facts["workspace_root_git_metadata"] is None
    assert facts["workspace_root_node_modules"] is None
    assert "cwd_inspection_error" in facts["verification_preflight"][0]["blockers"]


@pytest.mark.asyncio
async def test_copied_workspace_does_not_claim_evaluator_dependencies(tmp_path: Path) -> None:
    contract = TaskContract(
        task_id="copy", objective="Review", acceptance_criteria=["Preserve API"]
    )
    facts = await inspect_runtime(tmp_path, contract, registered_tools={"submit_result"})
    assert facts["verification_command_count"] == 0
    assert facts["workspace_root_git_metadata"] is False
    assert facts["workspace_root_node_modules"] is False
    assert facts["process_tools"] == []


@pytest.mark.asyncio
async def test_preflight_reports_independent_configuration_blockers(tmp_path: Path) -> None:
    contract = TaskContract(
        task_id="invalid-runtime",
        objective="Verify",
        acceptance_criteria=["Tests pass"],
        allowed_tools={"run_process"},
        allowed_commands={"python"},
        verification_commands=[
            VerificationCommand(id="missing", program="aica-nonexistent-test-runner", cwd="gone"),
            VerificationCommand(id="escape", program="python", cwd="../outside"),
        ],
    )
    facts = await inspect_runtime(tmp_path, contract, registered_tools=set())
    checks = {item["id"]: item["blockers"] for item in facts["verification_preflight"]}
    assert set(checks["missing"]) == {
        "no_process_tool",
        "executable_not_allowed",
        "executable_not_on_path",
        "cwd_missing",
    }
    assert "cwd_not_allowed" in checks["escape"]


@pytest.mark.asyncio
async def test_preflight_is_bounded_and_does_not_execute_commands(tmp_path: Path) -> None:
    contract = TaskContract(
        task_id="bounded-runtime",
        objective="Verify",
        acceptance_criteria=["Tests pass"],
        verification_commands=[
            VerificationCommand(
                id=f"test-{index}",
                program="python",
                args=["-c", "from pathlib import Path; Path('should-not-exist').touch()"],
            )
            for index in range(40)
        ],
    )
    facts = await inspect_runtime(tmp_path, contract, registered_tools={"run_process"})
    assert facts["verification_command_count"] == 40
    assert len(facts["verification_preflight"]) == 32
    assert facts["verification_preflight_truncated"] is True
    assert not (tmp_path / "should-not-exist").exists()


def test_compaction_preserves_latest_runtime_observations() -> None:
    latest = render_runtime({"verification_command_count": 1})
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "# Task contract\nkeep old requirements"},
        {"role": "user", "content": render_runtime({"verification_command_count": 0})},
        {"role": "assistant", "content": "x" * 10_000},
        {"role": "user", "content": latest},
    ]
    compacted = compact_messages(messages, max_chars=1600)
    contents = [item["content"] for item in compacted]
    assert latest in contents
    assert messages[1]["content"] in contents
    assert messages[2]["content"] not in contents
