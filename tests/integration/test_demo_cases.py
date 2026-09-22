from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aicoding_agent.domain.models import TaskContract

CASES_ROOT = Path(__file__).resolve().parents[2] / "examples" / "chat_cases"

# Cases expected to still fail (unsolved demos).
UNSOLVED_CASES = (
    ("02-search-normalization", "search.test.js"),
    ("03-accessible-dialog", "dialog.test.js"),
)

# Cases that have been fixed and should now pass.
SOLVED_CASES = (("01-counter-clamp", "counter.test.js"),)


def _load_config(workspace: Path) -> TaskContract | None:
    config_path = workspace / ".aicoding-agent.json"
    if not config_path.exists():
        return None
    return TaskContract.model_validate_json(config_path.read_text(encoding="utf-8"))


@pytest.mark.asyncio
@pytest.mark.parametrize(("case_name", "test_file"), UNSOLVED_CASES)
async def test_unsolved_demo_case_fails(
    case_name: str,
    test_file: str,
) -> None:
    workspace = CASES_ROOT / case_name
    profile = _load_config(workspace)
    if profile is None:
        pytest.skip(f"{case_name}: .aicoding-agent.json not found")
    process = await asyncio.create_subprocess_exec(
        "node",
        "--test",
        test_file,
        cwd=str(workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    assert profile.target_environment == "local"
    assert profile.verification_commands[0].args == ["--test", test_file]
    assert process.returncode != 0, (stdout + stderr).decode(errors="replace")


@pytest.mark.asyncio
@pytest.mark.parametrize(("case_name", "test_file"), SOLVED_CASES)
async def test_solved_demo_case_passes(
    case_name: str,
    test_file: str,
) -> None:
    workspace = CASES_ROOT / case_name
    profile = _load_config(workspace)
    if profile is None:
        pytest.skip(f"{case_name}: .aicoding-agent.json not found")
    process = await asyncio.create_subprocess_exec(
        "node",
        "--test",
        test_file,
        cwd=str(workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    assert profile.target_environment == "local"
    assert profile.verification_commands[0].args == ["--test", test_file]
    assert process.returncode == 0, (stdout + stderr).decode(errors="replace")
