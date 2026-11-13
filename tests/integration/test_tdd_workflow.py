import sys

import pytest

from aicoding_agent.adapters.fakes import (
    FakeArtifactBuilder,
    FakeBrowserVerifier,
    FakeChangeInspector,
    FakeTestEnvironmentPublisher,
)
from aicoding_agent.domain.models import (
    AgentResult,
    AgentStatus,
    CodingOutcome,
    TaskContract,
    TddSpec,
    VerificationCommand,
)
from aicoding_agent.persistence.jsonl import AtomicCheckpointStore
from aicoding_agent.regression.controller import RegressionController
from aicoding_agent.regression.gate import CommandSuite, RegressionGate
from aicoding_agent.workflow.approval import ApprovalPackageBuilder
from aicoding_agent.workflow.engine import DeliveryWorkflow, WorkflowError
from aicoding_agent.workflow.models import ApprovalRecord, WorkflowRun


def laboratory(tmp_path, mode="valid"):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.txt").write_text("fixed" if mode == "already_satisfied" else "bug")
    host = tmp_path / "tests"
    host.mkdir()
    fixed = host / "test_requirement.py"
    fixed.write_text(
        "import os\nfrom pathlib import Path\n"
        "def test_fix():\n"
        "    assert (Path(os.environ['REGRESSION_WORKSPACE'])/'app.txt').read_text() == 'fixed'\n",
    )
    if mode == "infra_error":
        fixed.write_text("raise RuntimeError('No test environment')\n")
    suite = CommandSuite(
        id="requirement", command=[sys.executable, "-m", "pytest", str(fixed), "-q"],
        cwd=host, test_files=[fixed], expected_tests=["test_requirement::test_fix"],
        reporter="pytest_junit",
    )
    controller = RegressionController(
        tmp_path / "regression", RegressionGate(tmp_path / "evidence", command_suites=[suite]),
    )

    class Agent:
        calls = 0

        async def run(self, contract, workspace, *, run_id, shared_budget, **kwargs):
            self.calls += 1
            await shared_budget.charge_turn()
            (workspace / "app.txt").write_text("fixed")
            return AgentResult(
                run_id=run_id, status=AgentStatus.COMPLETED,
                outcome=CodingOutcome(summary="Fixed", changed_files=["app.txt"]),
            )

    agent, builder, publisher = Agent(), FakeArtifactBuilder(), FakeTestEnvironmentPublisher()
    workflow = DeliveryWorkflow(
        agent=agent, change_inspector=FakeChangeInspector("a" * 64),
        artifact_builder=builder, test_publisher=publisher,
        browser_verifier=FakeBrowserVerifier(),
        checkpoints=AtomicCheckpointStore(tmp_path / "checkpoints"),
        regression_controller=controller, finalize_external_changes=False,
    )
    contract = TaskContract(
        task_id="tdd-workflow", objective="Fix the observed bug", acceptance_criteria=["Fixed"],
        target_environment="test",
        verification_commands=[VerificationCommand(id="requirement", program="python")],
        tdd=TddSpec(red_cases={"requirement": ["test_requirement::test_fix"]}),
    )
    return workflow, contract, source, agent, builder, publisher


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["valid", "already_satisfied", "infra_error"])
async def test_delivery_requires_valid_red_before_coding_build_or_deployment(tmp_path, mode):
    workflow, contract, source, agent, builder, publisher = laboratory(tmp_path, mode)
    result = await workflow.execute(contract, source, run_id="delivery-tdd")
    if mode != "valid":
        assert result.stage == "blocked"
        assert agent.calls == 0
        assert builder.receipts == []
        assert publisher.published == []
        return
    assert result.stage == "awaiting_approval", result.failure_reason
    assert agent.calls == 1
    assert len(builder.receipts) == len(publisher.published) == 1
    checkpoint = await workflow.checkpoints.load("delivery-tdd")
    proof = checkpoint["regression_outcome"]["tdd_evidence"]
    assert proof["red_report"]["status"] == "assertion_failed"
    assert checkpoint["regression_report"]["status"] == "passed"
    assert checkpoint["approval_package"]["subject"]["tdd_evidence"] == proof
    assert proof["red_report"]["report_path"] in result.evidence_refs


@pytest.mark.asyncio
async def test_approval_subject_changes_when_red_proof_changes(tmp_path):
    workflow, contract, source, _, _, _ = laboratory(tmp_path)
    result = await workflow.execute(contract, source, run_id="approval-tdd")
    assert result.stage == "awaiting_approval", result.failure_reason
    run = WorkflowRun.model_validate(await workflow.checkpoints.load("approval-tdd"))
    original = ApprovalPackageBuilder().build(run)
    run.regression_outcome["tdd_evidence"]["baseline_fingerprint"] = "b" * 64
    changed = ApprovalPackageBuilder().build(run)
    assert changed.subject_hash != original.subject_hash


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["mutate", "remove"])
async def test_delivery_refuses_old_approval_after_red_proof_changed(tmp_path, change):
    workflow, contract, source, _, _, _ = laboratory(tmp_path)
    result = await workflow.execute(contract, source, run_id="tampered-proof")
    checkpoint = await workflow.checkpoints.load("tampered-proof")
    if change == "mutate":
        checkpoint["regression_outcome"]["tdd_evidence"]["baseline_fingerprint"] = "b" * 64
    else:
        checkpoint["regression_outcome"]["tdd_evidence"] = None
    await workflow.checkpoints.save("tampered-proof", checkpoint)
    with pytest.raises(WorkflowError, match="evidence|subject"):
        await workflow.decide(
            "tampered-proof", ApprovalRecord(decision="approved", approver="owner",
                                             subject_hash=result.approval_subject_hash),
        )


@pytest.mark.asyncio
async def test_rejection_can_roll_back_even_when_tdd_proof_is_stale(tmp_path):
    workflow, contract, source, _, _, publisher = laboratory(tmp_path)
    result = await workflow.execute(contract, source, run_id="reject-stale")
    checkpoint = await workflow.checkpoints.load("reject-stale")
    checkpoint["regression_outcome"]["tdd_evidence"]["baseline_fingerprint"] = "b" * 64
    await workflow.checkpoints.save("reject-stale", checkpoint)
    rejected = await workflow.decide(
        "reject-stale", ApprovalRecord(decision="rejected", approver="owner",
                                        subject_hash=result.approval_subject_hash),
    )
    assert rejected.stage == "rolled_back"
    assert publisher.rolled_back == [checkpoint["deployment_receipt"]["deployment_id"]]
