import sys

import pytest

from aicoding_agent.domain.models import AgentResult, AgentStatus, TaskContract, VerificationCommand
from aicoding_agent.regression.controller import RegressionController
from aicoding_agent.regression.gate import CommandSuite, RegressionGate


def lab(tmp_path, *, initial="0", broken=False):
    source, host = tmp_path / "source", tmp_path / "host"
    source.mkdir()
    host.mkdir()
    (source / "value.txt").write_text(initial, encoding="utf-8")
    test = host / "check.py"
    test.write_text("import os,sys,pathlib\n"
                    "p=pathlib.Path(os.environ['REGRESSION_WORKSPACE'])/'value.txt'\n"
                    "ok=p.read_text()=='1'\n"
                    "report=pathlib.Path(next(a.split('=',1)[1] for a in sys.argv "
                    "if a.startswith('--junitxml=')))\n"
                    "case='<testcase classname=\"counter\" name=\"clamp\">'\n"
                    "case+=('' if ok else '<failure message=\"needs clamp\"/>')+'</testcase>'\n"
                    "report.write_text('<testsuite>'+case+'</testsuite>')\n"
                    "sys.exit(0 if ok else 1)\n", encoding="utf-8")
    if broken:
        test.write_text("raise ImportError('dependency missing')", encoding="utf-8")
    suite = CommandSuite(id="unit", command=[sys.executable, str(test)], cwd=host,
                         test_files=[test], reporter="pytest_junit",
                         expected_tests=["counter::clamp"])
    gate = RegressionGate(tmp_path / "evidence", command_suites=[suite])
    contract = TaskContract(task_id="clamp", objective="Implement clamp",
                            acceptance_criteria=["Clamp"],
                            verification_commands=[VerificationCommand(id="unit", program="python",
                                                                       args=["-V"])],
                            tdd={"red_cases": {"unit": ["counter::clamp"]}})
    return source, test, contract, RegressionController(tmp_path / "runtime", gate)


class Agent:
    def __init__(self, controller, test=None):
        self.controller, self.calls, self.test = controller, 0, test

    async def run(self, contract, workspace, *, run_id, shared_budget, **kwargs):
        self.calls += 1
        checkpoint = await self.controller.checkpoints.load("task")
        assert checkpoint["tdd_evidence"]["red_report"]["status"] == "assertion_failed"
        await shared_budget.charge_turn()
        (workspace / "value.txt").write_text("1", encoding="utf-8")
        if self.test:
            self.test.write_text(self.test.read_text().replace("ok=p.read_text()=='1'", "ok=True"),
                                 encoding="utf-8")
        return AgentResult(status=AgentStatus.COMPLETED, run_id=run_id)


async def test_red_precedes_model_and_green_acceptance_has_paired_evidence(tmp_path):
    source, _, contract, controller = lab(tmp_path)
    agent = Agent(controller)
    outcome = await controller.run(contract, source, run_id="task", agent=agent, adopt=True)
    assert outcome.status == "accepted"
    assert outcome.tdd_evidence.red_report.status == "assertion_failed"
    assert outcome.report.status == "passed"
    assert (source / "value.txt").read_text() == "1"
    assert await controller.validate_checkpoint("task")


@pytest.mark.parametrize("initial,broken", [("1", False), ("0", True)])
async def test_invalid_red_never_calls_model_or_advances_baseline(tmp_path, initial, broken):
    source, _, contract, controller = lab(tmp_path, initial=initial, broken=broken)
    agent = Agent(controller)
    outcome = await controller.run(contract, source, run_id="task", agent=agent)
    assert outcome.status == "blocked"
    assert agent.calls == 0
    assert (await controller.store.current()).revision == 0


async def test_test_weakening_after_red_blocks_adoption(tmp_path):
    source, test, contract, controller = lab(tmp_path)
    outcome = await controller.run(contract, source, run_id="task", agent=Agent(controller, test),
                                   adopt=True)
    assert outcome.status == "blocked"
    assert (source / "value.txt").read_text() == "0"
    assert (await controller.store.current()).revision == 0


async def test_resume_after_red_reuses_proof_and_does_not_reset_budget(tmp_path):
    import asyncio

    source, _, contract, controller = lab(tmp_path)

    class InterruptedAgent:
        async def run(self, contract, workspace, *, shared_budget, **kwargs):
            await shared_budget.charge_turn()
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await controller.run(contract, source, run_id="task", agent=InterruptedAgent())
    state = await controller.checkpoints.load("task")
    budget = await controller.budget_for("task", contract)
    before = await budget.refresh()
    restarted = RegressionController(controller.root, controller.gate)
    outcome = await restarted.run(contract, source, run_id="task", agent=Agent(restarted))
    after = await (await restarted.budget_for("task", contract)).refresh()
    assert outcome.status == "accepted"
    assert outcome.tdd_evidence.red_report.attempt_id == (
        state["tdd_evidence"]["red_report"]["attempt_id"]
    )
    assert before.deadline_epoch == after.deadline_epoch
    assert before.command_runs["unit"] == 1
    assert after.command_runs["unit"] == 2 and after.turns == 2
