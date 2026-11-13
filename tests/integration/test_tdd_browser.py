"""Installed Chromium smoke for host RED proof, coding and complete GREEN acceptance."""
import json
from pathlib import Path

import pytest
from test_regression_browser import EditingAgent
from test_regression_browser import chromium_lab as chromium_lab

from aicoding_agent.domain.models import TddSpec
from aicoding_agent.persistence.jsonl import AtomicCheckpointStore


async def recorded_environment_retries(controller, run_id):
    red = await AtomicCheckpointStore(controller.root / "tdd").load(run_id)
    green = await controller.checkpoints.load(run_id)
    assert 0 <= red["infra_retries_used"] <= controller.infra_retries
    assert 0 <= green["infra_retries_used"] <= controller.infra_retries
    return red["infra_retries_used"] + green["infra_retries_used"]


class TddEditingAgent(EditingAgent):
    def __init__(self, controller, fixes):
        super().__init__(fixes)
        self.controller = controller
        self.red_attempts = []

    async def run(self, contract, workspace, *, run_id, shared_budget, **kwargs):
        outer_run = run_id.rsplit("-a", 1)[0]
        checkpoint = await self.controller.checkpoints.load(outer_run)
        proof = checkpoint["tdd_evidence"]
        assert proof["red_report"]["status"] == "assertion_failed"
        assert [item["status"] for item in proof["red_report"]["tests"]] == [
            "passed", "assertion_failed",
        ]
        assert proof["red_report"]["tests"][1]["cases"] == {
            "counter cannot decrement below zero": "assertion_failed",
            "increment and decrement preserve positive values": "passed",
        }
        self.red_attempts.append(proof["red_report"]["attempt_id"])
        return await super().run(
            contract, workspace, run_id=run_id, shared_budget=shared_budget, **kwargs,
        )


def configure(chromium_lab):
    source, contract, create_controller = chromium_lab
    contract.tdd = TddSpec(red_cases={"ui": ["counter cannot decrement below zero"]})
    controller = create_controller()
    controller.gate.require_both = True
    controller.require_tdd = True
    return source, contract, controller, create_controller


@pytest.mark.asyncio
async def test_chromium_red_precedes_coding_and_green_accepts_with_restored_proof(chromium_lab):
    source, contract, controller, create_controller = configure(chromium_lab)
    agent = TddEditingAgent(controller, [True])
    outcome = await controller.run(contract, source, run_id="tdd-real-pass", agent=agent)
    assert outcome.status == "accepted", outcome.reason
    assert agent.calls == 1
    assert outcome.tdd_evidence is not None
    assert outcome.tdd_evidence.red_report.status == "assertion_failed"
    assert [test.status for test in outcome.report.tests] == ["passed", "passed"]
    assert outcome.report.tests[1].cases == {
        "counter cannot decrement below zero": "passed",
        "increment and decrement preserve positive values": "passed",
    }
    assert outcome.baseline_revision == 1
    assert await controller.validate_checkpoint("tdd-real-pass")
    budget = await (await controller.budget_for("tdd-real-pass", contract)).refresh()
    executions = 2 + await recorded_environment_retries(controller, "tdd-real-pass")
    assert budget.browser_runs["ui"] == executions
    assert budget.command_runs["unit"] == executions
    assert budget.turns == 1
    restored = create_controller()
    restored.gate.require_both = True
    restored.require_tdd = True
    resumed = await restored.run(contract, source, run_id="tdd-real-pass", agent=agent)
    assert resumed.status == "accepted"
    assert await restored.validate_checkpoint("tdd-real-pass")
    assert resumed.tdd_evidence == outcome.tdd_evidence
    assert agent.calls == 1
    after = await (await restored.budget_for("tdd-real-pass", contract)).refresh()
    assert after == budget


@pytest.mark.asyncio
async def test_chromium_two_failed_green_attempts_keep_baseline_and_shared_budget(chromium_lab):
    source, contract, controller, _ = configure(chromium_lab)
    agent = TddEditingAgent(controller, [False])
    outcome = await controller.run(contract, source, run_id="tdd-real-block", agent=agent)
    assert outcome.status == "blocked", outcome.reason
    assert agent.calls == 2
    assert len(set(agent.red_attempts)) == 1
    assert all("return value - 1;" in code for code in agent.initial_code)
    assert outcome.tdd_evidence is not None
    assert [test.status for test in outcome.report.tests] == ["passed", "assertion_failed"]
    baseline = await controller.store.current()
    assert baseline.revision == 0
    assert "return value - 1;" in (
        Path(baseline.snapshot_path) / "counter.js"
    ).read_text(encoding="utf-8")
    budget = await (await controller.budget_for("tdd-real-block", contract)).refresh()
    executions = 3 + await recorded_environment_retries(controller, "tdd-real-block")
    assert budget.browser_runs["ui"] == executions
    assert budget.command_runs["unit"] == executions
    assert budget.turns == 2
    events = [
        json.loads(line) for line in (controller.root / "events.jsonl").read_text().splitlines()
    ]
    assert sum(event["event"] == "tdd.red_verified" for event in events) == 1
