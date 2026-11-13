import asyncio
import json
import sys
from pathlib import Path

import pytest

from aicoding_agent.domain.models import AgentResult, AgentStatus, TaskContract, VerificationCommand
from aicoding_agent.regression.controller import RegressionController
from aicoding_agent.regression.gate import CommandSuite, RegressionGate


class EditingAgent:
    def __init__(self, changes):
        self.changes = changes
        self.workspaces = []
        self.initial_values = []

    async def run(self, contract, workspace, *, run_id, shared_budget, **kwargs):
        await shared_budget.charge_turn()
        self.workspaces.append(workspace)
        self.initial_values.append(json.loads((workspace / "state.json").read_text()))
        values = dict(self.initial_values[-1])
        change = self.changes[min(len(self.workspaces) - 1, len(self.changes) - 1)]
        for name, value in change.items():
            if name in {"a", "b"}:
                (workspace / f"{name}.flag").write_text(json.dumps(value), encoding="utf-8")
            else:
                values[name] = value
        (workspace / "state.json").write_text(json.dumps(values), encoding="utf-8")
        return AgentResult(status=AgentStatus.COMPLETED, run_id=run_id)


def task(name, verification):
    return TaskContract(task_id=name, objective=f"Implement {name}",
                        acceptance_criteria=["Historical behavior remains valid"],
                        verification_commands=[VerificationCommand(
                            id=verification, program="python", args=["host-test.py"]
                        )])


@pytest.fixture
def laboratory(tmp_path):
    source, host = tmp_path / "source", tmp_path / "host-tests"
    source.mkdir()
    host.mkdir()
    (source / "state.json").write_text(json.dumps({"old": True, "a": False, "b": False}),
                                      encoding="utf-8")
    for flag in ("a", "b"):
        (source / f"{flag}.flag").write_text("false", encoding="utf-8")
    old = host / "old.py"
    old.write_text("import os,json,pathlib\n"
                   "s=json.loads((pathlib.Path(os.environ['REGRESSION_WORKSPACE'])/'state.json')"
                   ".read_text())\nassert s['old']\n"
                   "root=pathlib.Path(os.environ['REGRESSION_WORKSPACE'])\n"
                   "assert not (json.loads((root/'a.flag').read_text()) and "
                   "json.loads((root/'b.flag').read_text()))\n",
                   encoding="utf-8")
    current = host / "current.py"
    current.write_text("import os,json,pathlib\n"
                       "s=json.loads((pathlib.Path(os.environ['REGRESSION_WORKSPACE'])/"
                       "'state.json').read_text())\nassert 'old' in s\n", encoding="utf-8")
    suites = [CommandSuite(id=name, command=[sys.executable, str(script)], cwd=host,
                           test_files=[script])
              for name, script in (("old", old), ("current", current))]
    gate = RegressionGate(tmp_path / "evidence", command_suites=suites)
    controller = RegressionController(tmp_path / "runtime", gate)
    return source, host, controller


async def test_historical_failure_retries_once_from_baseline_then_blocks(laboratory):
    source, _, controller = laboratory
    accepted = await controller.run(task("A", "old"), source, run_id="a",
                                    agent=EditingAgent([{}]))
    assert accepted.status == "accepted"
    bad = EditingAgent([{"old": False}])
    blocked = await controller.run(task("B", "current"), source, run_id="b", agent=bad)
    assert blocked.status == "blocked"
    assert len(bad.workspaces) == 2
    assert bad.workspaces[0] != bad.workspaces[1]
    assert all(values["old"] for values in bad.initial_values)
    assert (await controller.store.current()).revision == 1
    assert blocked.agent_result.turns == 2
    assert {test.id for test in blocked.report.tests} == {"old", "current"}


async def test_baseline_retry_can_succeed_and_promote_once(laboratory):
    source, _, controller = laboratory
    await controller.run(task("A", "old"), source, run_id="a", agent=EditingAgent([{}]))
    agent = EditingAgent([{"old": False}, {"a": True}])
    outcome = await controller.run(task("B", "current"), source, run_id="b", agent=agent)
    assert outcome.status == "accepted"
    assert outcome.baseline_revision == 2
    assert len(agent.workspaces) == 2
    assert agent.initial_values[1]["old"]
    snapshot = Path((await controller.store.current()).snapshot_path)
    assert json.loads((snapshot / "a.flag").read_text())


async def test_individually_valid_candidates_fail_combined_regression(laboratory):
    source, _, controller = laboratory
    await controller.run(task("seed", "old"), source, run_id="seed", agent=EditingAgent([{}]))
    left = await controller.run(task("left", "current"), source, run_id="left",
                                agent=EditingAgent([{"a": True}]), accept=False)
    right = await controller.run(task("right", "current"), source, run_id="right",
                                 agent=EditingAgent([{"b": True}]), accept=False)
    assert left.status == right.status == "validated"
    combined = await controller.integrate(left)
    assert combined.status == "accepted"
    rejected = await controller.integrate(right)
    assert rejected.status == "blocked"
    assert rejected.report.status == "assertion_failed"
    assert (await controller.store.current()).revision == 2


@pytest.mark.parametrize("tamper", ["code", "report"])
async def test_resume_rejects_stale_code_or_missing_report(laboratory, tamper):
    source, _, controller = laboratory
    agent = EditingAgent([{}])
    outcome = await controller.run(task("A", "old"), source, run_id="a", agent=agent)
    if tamper == "code":
        (outcome.workspace / "state.json").write_text("{}", encoding="utf-8")
    else:
        Path(outcome.report.report_path).unlink()
    resumed = await controller.run(task("A", "old"), source, run_id="a", agent=agent)
    assert resumed.status == "blocked"
    assert len(agent.workspaces) == 1
    assert (await controller.store.current()).revision == 1


@pytest.mark.parametrize("recovers", [True, False])
async def test_environment_retry_reuses_candidate_without_model_repair(laboratory, recovers):
    source, host, controller = laboratory
    script = host / "flaky.py"
    script.write_text(
        "import pathlib,sys\n"
        "marker=pathlib.Path('first-attempt')\n"
        f"if not marker.exists() or {not recovers!r}:\n"
        "    marker.write_text('done')\n"
        "    sys.exit(0)\n"
        "output=pathlib.Path(next(a.split('=',1)[1] for a in sys.argv if "
        "a.startswith('--junitxml=')))\n"
        "output.write_text('<testsuite><testcase classname=\"Case\" name=\"check\"/>'"
        "+'</testsuite>')\n", encoding="utf-8",
    )
    suite = CommandSuite(id="flaky", command=[sys.executable, str(script)], cwd=host,
                         test_files=[script], reporter="pytest_junit",
                         expected_tests=["Case::check"])
    controller.gate.command_suites["flaky"] = suite
    agent = EditingAgent([{}])
    outcome = await controller.run(task("A", "flaky"), source, run_id="a", agent=agent)
    assert outcome.status == ("accepted" if recovers else "blocked")
    assert len(agent.workspaces) == 1
    state = await controller.checkpoints.load("a")
    assert state["infra_retries_used"] == 1
    if not recovers:
        assert (await controller.store.current()).revision == 0
        assert outcome.report.status == "infra_error"


async def test_interrupted_coding_reuses_candidate_and_persisted_total_budget(laboratory):
    source, _, controller = laboratory

    class InterruptedAgent:
        async def run(self, contract, workspace, *, shared_budget, **kwargs):
            await shared_budget.charge_turn()
            (workspace / "a.flag").write_text("true", encoding="utf-8")
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await controller.run(task("A", "old"), source, run_id="a", agent=InterruptedAgent())
    saved = await controller.checkpoints.load("a")
    budget_before = await (await controller.budget_for("a", task("A", "old"))).refresh()
    restarted = RegressionController(controller.root, controller.gate)
    agent = EditingAgent([{}])
    outcome = await restarted.run(task("A", "old"), source, run_id="a", agent=agent)
    budget_after = await (await restarted.budget_for("a", task("A", "old"))).refresh()
    assert outcome.status == "accepted"
    assert agent.workspaces == [Path(saved["workspace"])]
    assert outcome.agent_result.turns == 2
    assert budget_before.deadline_epoch == budget_after.deadline_epoch
    assert json.loads((source / "a.flag").read_text()) is False


async def test_publish_then_checkpoint_crash_resumes_without_double_promotion(
    laboratory, monkeypatch,
):
    source, _, controller = laboratory
    original_finish = controller._finish

    async def crash_after_publication(run_id, state, status, *args, **kwargs):
        if status == "accepted":
            raise RuntimeError("simulated crash after atomic baseline publication")
        return await original_finish(run_id, state, status, *args, **kwargs)

    monkeypatch.setattr(controller, "_finish", crash_after_publication)
    agent = EditingAgent([{"a": True}])
    with pytest.raises(RuntimeError, match="simulated crash"):
        await controller.run(task("A", "old"), source, run_id="a", agent=agent)
    assert (await controller.store.current()).revision == 1
    restarted = RegressionController(controller.root, controller.gate)
    outcome = await restarted.run(task("A", "old"), source, run_id="a", agent=agent)
    assert outcome.status == "accepted"
    assert outcome.baseline_revision == (await restarted.store.current()).revision == 1
    assert len(agent.workspaces) == 1


async def test_weakened_passing_historical_tests_cannot_adopt_candidate(laboratory):
    source, host, controller = laboratory
    await controller.run(task("A", "old"), source, run_id="a",
                         agent=EditingAgent([{}]), adopt=True)
    # The changed host script now exits successfully despite a broken requirement.
    (host / "old.py").write_text("assert True\n", encoding="utf-8")
    outcome = await controller.run(task("B", "current"), source, run_id="b",
                                   agent=EditingAgent([{"old": False}]), adopt=True)
    assert outcome.report.status == "passed"
    assert outcome.status == "blocked"
    assert "fingerprint changed" in outcome.reason
    assert json.loads((source / "state.json").read_text())["old"]
    assert (await controller.store.current()).revision == 1
