import asyncio
import os
import sys

import pytest
from pydantic import ValidationError

from aicoding_agent.domain.models import TaskContract, canonical_task_contract_payload


def test_tdd_contract_keeps_legacy_defaults_and_declares_cases():
    values = dict(task_id="counter", objective="Fix zero clamp", acceptance_criteria=["Zero clamp"])
    assert TaskContract(**values).tdd is None
    task = TaskContract(**values, tdd={"red_cases": {"counter": ["zero decrement"]}})
    assert task.tdd.red_cases["counter"] == ["zero decrement"]
    with pytest.raises(ValidationError):
        TaskContract(**values, tdd={"red_cases": {"counter": []}})
    with pytest.raises(ValidationError):
        TaskContract(**values, tdd={"red_cases": {"counter": ["zero", "zero"]}})
    assert TaskContract(**values, tdd={"propose_tests": True}).tdd.propose_tests


def test_legacy_contract_fingerprint_does_not_gain_a_null_tdd_field():
    contract = TaskContract(task_id="old", objective="Fix", acceptance_criteria=["Pass"])
    assert "tdd" not in canonical_task_contract_payload(contract)


async def test_resume_contract_fingerprint_is_stable_across_python_hash_seeds():
    script = (
        "from aicoding_agent.domain.models import TaskContract;"
        "from aicoding_agent.regression.controller import _contract_hash;"
        "print(_contract_hash(TaskContract(task_id='stable',objective='Fix',"
        "acceptance_criteria=['Pass'])))"
    )
    outputs = []
    for seed in ("1", "2"):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script, env={**os.environ, "PYTHONHASHSEED": seed},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        output, error = await process.communicate()
        assert process.returncode == 0, error.decode()
        outputs.append(output.strip())
    assert outputs[0] == outputs[1]
