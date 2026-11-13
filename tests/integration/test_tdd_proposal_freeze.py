import asyncio
import sys

import pytest

from aicoding_agent.domain.models import AgentResult, AgentStatus, TaskContract, VerificationCommand
from aicoding_agent.regression.controller import RegressionController
from aicoding_agent.regression.gate import CommandSuite, RegressionGate
from aicoding_agent.regression.tdd import TddError
from aicoding_agent.tools.tdd import TddProposal


def laboratory(tmp_path):
    source, host = tmp_path / "source", tmp_path / "host"
    source.mkdir()
    host.mkdir()
    (source / "value.txt").write_text("0", encoding="utf-8")
    test = host / "tests" / "test_counter.py"
    suite = CommandSuite(
        id="unit", command=[sys.executable, "-m", "pytest", str(test), "-q"], cwd=host,
        test_files=[test], reporter="pytest_junit",
        expected_tests=["tests.test_counter::test_clamp"],
    )
    contract = TaskContract(
        task_id="clamp", objective="Implement clamp", acceptance_criteria=["Clamp"],
        verification_commands=[VerificationCommand(id="unit", program="python", args=["-V"])],
        tdd={"propose_tests": True},
    )
    proposal = TddProposal.model_validate({
        "files": [{"path": "tests/test_counter.py", "content": (
            "import os\nfrom pathlib import Path\n\n"
            "def test_clamp():\n"
            "    value = Path(os.environ['REGRESSION_WORKSPACE']) / 'value.txt'\n"
            "    assert value.read_text() == '1'\n"
        )}],
        "red_cases": {"unit": ["tests.test_counter::test_clamp"]},
        "requirement_links": {"tests.test_counter::test_clamp": 0},
    })
    gate = RegressionGate(tmp_path / "evidence", command_suites=[suite])
    return source, test, contract, proposal, RegressionController(tmp_path / "runtime", gate)


class Agent:
    def __init__(self, controller, proposal):
        self.controller, self.proposal = controller, proposal
        self.drafts, self.implementations = 0, 0

    async def propose_tests(self, contract, workspace, *, run_id, shared_budget):
        self.drafts += 1
        assert "tests/test_counter.py" in contract.objective
        state = await self.controller.checkpoints.load("task")
        assert state["phase"] == "test_design"
        assert (workspace / "value.txt").read_text() == "0"
        assert not (workspace / "tests").exists()
        await shared_budget.charge_turn()
        return self.proposal

    async def run(self, contract, workspace, *, run_id, shared_budget, **kwargs):
        self.implementations += 1
        state = await self.controller.checkpoints.load("task")
        assert state["tdd_evidence"]["red_report"]["status"] == "assertion_failed"
        await shared_budget.charge_turn()
        (workspace / "value.txt").write_text("1", encoding="utf-8")
        return AgentResult(status=AgentStatus.COMPLETED, run_id=run_id)


async def test_new_model_test_frozen_externally_then_red_then_green(tmp_path):
    source, test, contract, proposal, controller = laboratory(tmp_path)
    agent = Agent(controller, proposal)
    result = await controller.run(contract, source, run_id="task", agent=agent, adopt=True)
    assert result.status == "accepted", result.reason
    assert result.tdd_evidence.red_report.tests[0].cases == {
        "tests.test_counter::test_clamp": "assertion_failed"
    }
    assert agent.drafts == agent.implementations == 1
    assert test.read_text() == proposal.files[0].content
    assert not (source / "tests").exists()
    assert (source / "value.txt").read_text() == "1"


async def test_resume_after_red_saved_before_main_checkpoint_does_not_redraft(tmp_path):
    import asyncio

    source, _, contract, proposal, controller = laboratory(tmp_path)
    agent = Agent(controller, proposal)
    original_prepare = controller.prepare_tdd

    async def interrupted(*args, **kwargs):
        await original_prepare(*args, **kwargs)
        raise asyncio.CancelledError

    controller.prepare_tdd = interrupted
    with pytest.raises(asyncio.CancelledError):
        await controller.run(contract, source, run_id="task", agent=agent)
    restarted = RegressionController(controller.root, controller.gate)
    outcome = await restarted.run(contract, source, run_id="task", agent=agent)
    assert outcome.status == "accepted", outcome.reason
    assert agent.drafts == agent.implementations == 1


async def test_model_cannot_replace_existing_host_test_before_red(tmp_path):
    source, test, contract, proposal, controller = laboratory(tmp_path)
    test.parent.mkdir()
    test.write_text("def test_clamp():\n    assert False\n", encoding="utf-8")
    original = test.read_bytes()
    agent = Agent(controller, proposal)
    result = await controller.run(contract, source, run_id="task", agent=agent)
    assert result.status == "blocked"
    assert "overwrite" in result.reason
    assert test.read_bytes() == original
    assert agent.implementations == 0
    assert (await controller.store.current()).revision == 0


async def test_proposed_file_without_host_slot_rejected(tmp_path):
    source, _, contract, proposal, controller = laboratory(tmp_path)
    data = proposal.model_dump()
    data["files"][0]["path"] = "tests/test_other.py"
    proposal = TddProposal.model_validate(data)
    await controller.store.initialize(source)
    with pytest.raises(TddError, match="host-declared"):
        await controller._freeze_proposal(contract, proposal)


@pytest.mark.parametrize("interruption", ["red_verify", "coding"])
async def test_interrupted_proposal_resumes_frozen_tests_without_new_draft(tmp_path, interruption):
    source, test, contract, proposal, controller = laboratory(tmp_path)
    agent = Agent(controller, proposal)
    original_verify, original_run = controller.gate.verify, agent.run
    intercepted = False

    async def paused_verify(*args, **kwargs):
        nonlocal intercepted
        if not intercepted:
            intercepted = True
            raise asyncio.CancelledError()
        return await original_verify(*args, **kwargs)

    async def paused_coding(*args, **kwargs):
        nonlocal intercepted
        if not intercepted:
            intercepted = True
            raise asyncio.CancelledError()
        return await original_run(*args, **kwargs)

    if interruption == "red_verify":
        controller.gate.verify = paused_verify
    else:
        agent.run = paused_coding
    with pytest.raises(asyncio.CancelledError):
        await controller.run(contract, source, run_id="task", agent=agent, adopt=True)
    assert test.read_text() == proposal.files[0].content
    assert (source / "value.txt").read_text() == "0"
    assert agent.drafts == 1
    result = await controller.run(contract, source, run_id="task", agent=agent, adopt=True)
    assert result.status == "accepted", result.reason
    assert agent.drafts == 1
    assert agent.implementations == 1
    assert (source / "value.txt").read_text() == "1"
