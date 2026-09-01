from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aicoding_agent.domain.models import TaskContract

CASES_ROOT = Path(__file__).resolve().parents[2] / "examples" / "chat_cases"
CASES = (
    ("01-counter-clamp", "counter.test.js"),
    ("02-search-normalization", "search.test.js"),
    ("03-accessible-dialog", "dialog.test.js"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("case_name", "test_file"), CASES)
async def test_demo_case_starts_with_a_reproducible_failure(
    case_name: str,
    test_file: str,
) -> None:
    workspace = CASES_ROOT / case_name
    profile = TaskContract.model_validate_json(
        (workspace / ".aicoding-agent.json").read_text(encoding="utf-8")
    )
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
