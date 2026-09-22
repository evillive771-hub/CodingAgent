from pathlib import Path

import pytest

from aicoding_agent.adapters.fakes import (
    FakeArtifactBuilder,
    FakeBrowserVerifier,
    FakeChangeInspector,
    FakeChangePublisher,
    FakeTeambitionAdapter,
    FakeTestEnvironmentPublisher,
    FakeWorkspaceManager,
)
from aicoding_agent.adapters.ports import StageExecutionError, VerificationReport
from aicoding_agent.domain.models import AgentResult, AgentStatus, CodingOutcome, TaskContract
from aicoding_agent.persistence.jsonl import AtomicCheckpointStore
from aicoding_agent.workflow.engine import DeliveryWorkflow, WorkflowError
from aicoding_agent.workflow.models import ApprovalRecord


class CompletedAgent:
    def __init__(self) -> None:
        self.workspaces: list[Path] = []

    async def run(self, contract, workspace, *, run_id=None):
        self.workspaces.append(workspace)
        return AgentResult(
            status=AgentStatus.COMPLETED,
            run_id=run_id or "run",
            outcome=CodingOutcome(summary="Fixed", changed_files=["app.py"]),
        )


class PlannedCrash(BaseException):
    pass


class CrashOnceVerifier:
    def __init__(self) -> None:
        self.calls = 0

    async def verify(self, receipt, contract):
        self.calls += 1
        if self.calls == 1:
            raise PlannedCrash("simulated host crash")
        return VerificationReport(
            ok=True,
            summary="Recovered verification passed",
            evidence_refs=["fake://evidence/recovered"],
        )


class CrashOnceTeambition(FakeTeambitionAdapter):
    async def transition_to_testing(self, *args, **kwargs):
        raise PlannedCrash("simulated host crash after Git publication")


class InternallyRolledBackPublisher(FakeTestEnvironmentPublisher):
    async def publish(self, *args, **kwargs):
        del args, kwargs
        raise StageExecutionError(
            "activation health check failed",
            evidence_refs=["fake://deployment/failure", "fake://rollback/automatic"],
            rollback_status="succeeded",
        )


def build_workflow(
    tmp_path: Path,
    *,
    browser_ok: bool = True,
    agent: CompletedAgent | None = None,
    workspace_manager: FakeWorkspaceManager | None = None,
):
    publisher = FakeTestEnvironmentPublisher()
    teambition = FakeTeambitionAdapter()
    change_publisher = FakeChangePublisher()
    workflow = DeliveryWorkflow(
        agent=agent or CompletedAgent(),
        change_inspector=FakeChangeInspector("a" * 64),
        test_publisher=publisher,
        browser_verifier=FakeBrowserVerifier(ok=browser_ok),
        change_publisher=change_publisher,
        teambition=teambition,
        checkpoints=AtomicCheckpointStore(tmp_path / "checkpoints"),
        workspace_manager=workspace_manager,
    )
    return workflow, publisher, teambition, change_publisher


@pytest.mark.asyncio
async def test_workflow_waits_for_hash_bound_approval_then_finalizes(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    contract = TaskContract(
        task_id="TB:123",
        source="teambition",
        source_ticket_id="123",
        objective="Fix ticket",
        acceptance_criteria=["Browser verification passes"],
        target_environment="test",
    )
    workflow, publisher, teambition, change_publisher = build_workflow(tmp_path)

    first = await workflow.execute(contract, tmp_path, run_id="workflow-run")

    assert first.stage == "awaiting_approval"
    assert first.approval_subject_hash is not None
    assert len(publisher.published) == 1
    assert not teambition.transitions
    checkpoint = await workflow.checkpoints.load("workflow-run")
    assert checkpoint is not None
    assert checkpoint["approval_package"]["subject_hash"] == first.approval_subject_hash
    assert checkpoint["approval_package"]["subject"]["task"]["contract_hash"]

    final = await workflow.decide(
        "workflow-run",
        ApprovalRecord(
            decision="approved",
            subject_hash=first.approval_subject_hash,
            approver="frontend-owner",
        ),
    )

    assert final.stage == "completed"
    assert len(change_publisher.published) == 1
    assert teambition.transitions[0]["ticket_id"] == "123"


@pytest.mark.asyncio
async def test_failed_test_verification_rolls_back(tmp_path: Path) -> None:
    contract = TaskContract(
        task_id="rollback-test",
        objective="Exercise rollback",
        acceptance_criteria=["Failure rolls back"],
        target_environment="test",
    )
    workflow, publisher, _, _ = build_workflow(tmp_path, browser_ok=False)

    result = await workflow.execute(contract, tmp_path, run_id="rollback-run")

    assert result.stage == "rolled_back"
    assert publisher.rolled_back == [publisher.published[0].deployment_id]
    assert "verification failed" in (result.failure_reason or "").lower()
    assert result.rollback_status == "succeeded"
    assert f"fake://rollback/{publisher.published[0].deployment_id}" in result.evidence_refs


@pytest.mark.asyncio
async def test_deployment_failure_preserves_internal_rollback_evidence(tmp_path: Path) -> None:
    contract = TaskContract(
        task_id="internal-rollback",
        objective="Preserve deployment failure evidence",
        acceptance_criteria=["The workflow reports the automatic rollback"],
        target_environment="test",
    )
    workflow, _, _, _ = build_workflow(tmp_path)
    workflow.test_publisher = InternallyRolledBackPublisher()

    result = await workflow.execute(contract, tmp_path, run_id="internal-rollback-run")

    assert result.stage == "failed"
    assert result.rollback_status == "succeeded"
    assert result.evidence_refs == [
        "fake://deployment/failure",
        "fake://rollback/automatic",
    ]


@pytest.mark.asyncio
async def test_phase_one_builds_before_deploy_and_approval_does_not_publish(
    tmp_path: Path,
) -> None:
    contract = TaskContract(
        task_id="phase-one",
        objective="Complete the test-only delivery flow",
        acceptance_criteria=["Approval is bound to the artifact manifest"],
        target_environment="test",
    )
    builder = FakeArtifactBuilder()
    workflow, publisher, teambition, change_publisher = build_workflow(tmp_path)
    workflow.artifact_builder = builder
    workflow.finalize_external_changes = False

    waiting = await workflow.execute(contract, tmp_path, run_id="phase-one-run")

    assert waiting.stage == "awaiting_approval"
    checkpoint = await workflow.checkpoints.load("phase-one-run")
    assert checkpoint is not None
    assert checkpoint["build_receipt"]["artifact_hash"]
    assert checkpoint["approval_package"]["subject"]["build_receipt"]["manifest_ref"]
    assert publisher.published[0].artifact_hash == builder.receipts[0].artifact_hash

    completed = await workflow.decide(
        "phase-one-run",
        ApprovalRecord(
            decision="approved",
            subject_hash=waiting.approval_subject_hash or "",
            approver="frontend-owner",
        ),
    )

    assert completed.stage == "completed"
    assert change_publisher.published == []
    assert teambition.transitions == []


@pytest.mark.asyncio
async def test_workflow_uses_and_releases_managed_workspace_after_completion(
    tmp_path: Path,
) -> None:
    source_repository = tmp_path / "source"
    source_repository.mkdir()
    agent = CompletedAgent()
    manager = FakeWorkspaceManager(tmp_path / "worktrees")
    workflow, _, _, _ = build_workflow(
        tmp_path,
        agent=agent,
        workspace_manager=manager,
    )
    contract = TaskContract(
        task_id="managed-workspace",
        objective="Run inside an isolated workspace",
        acceptance_criteria=["Only the managed workspace is modified"],
        target_environment="test",
    )

    first = await workflow.execute(contract, source_repository, run_id="managed-run")

    assert first.stage == "awaiting_approval"
    assert agent.workspaces == [Path(manager.prepared[0].path)]
    assert manager.released == []

    final = await workflow.decide(
        "managed-run",
        ApprovalRecord(
            decision="approved",
            subject_hash=first.approval_subject_hash or "",
            approver="frontend-owner",
        ),
    )

    assert final.stage == "completed"
    assert final.workspace_released is True
    assert manager.released == [manager.prepared[0].lease_id]


@pytest.mark.asyncio
async def test_workflow_refuses_to_overwrite_an_existing_run(tmp_path: Path) -> None:
    contract = TaskContract(
        task_id="duplicate-run",
        objective="Preserve the first workflow checkpoint",
        acceptance_criteria=["A duplicate run id is rejected"],
        target_environment="test",
    )
    workflow, _, _, _ = build_workflow(tmp_path)
    first = await workflow.execute(contract, tmp_path, run_id="same-run")

    with pytest.raises(WorkflowError, match="already exists"):
        await workflow.execute(contract, tmp_path, run_id="same-run")

    assert first.stage == "awaiting_approval"


@pytest.mark.asyncio
async def test_workflow_resumes_from_test_verification_checkpoint(tmp_path: Path) -> None:
    contract = TaskContract(
        task_id="resume-verification",
        objective="Resume after a host crash",
        acceptance_criteria=["The deployment is not repeated"],
        target_environment="test",
    )
    workflow, publisher, _, _ = build_workflow(tmp_path)
    verifier = CrashOnceVerifier()
    workflow.browser_verifier = verifier

    with pytest.raises(PlannedCrash):
        await workflow.execute(contract, tmp_path, run_id="resume-run")

    checkpoint = await workflow.checkpoints.load("resume-run")
    assert checkpoint is not None
    assert checkpoint["stage"] == "test_verifying"
    assert checkpoint["deployment_receipt"] is not None

    result = await workflow.resume("resume-run")

    assert result.stage == "awaiting_approval"
    assert verifier.calls == 2
    assert len(publisher.published) == 1


@pytest.mark.asyncio
async def test_workflow_resumes_finalization_without_republishing_git(tmp_path: Path) -> None:
    contract = TaskContract(
        task_id="resume-finalization",
        source="teambition",
        source_ticket_id="TB-9",
        objective="Resume finalization",
        acceptance_criteria=["Git publication is idempotent"],
        target_environment="test",
    )
    workflow, _, _, change_publisher = build_workflow(tmp_path)
    waiting = await workflow.execute(contract, tmp_path, run_id="finalize-resume")
    workflow.teambition = CrashOnceTeambition()

    with pytest.raises(PlannedCrash):
        await workflow.decide(
            "finalize-resume",
            ApprovalRecord(
                decision="approved",
                subject_hash=waiting.approval_subject_hash or "",
                approver="frontend-owner",
            ),
        )

    checkpoint = await workflow.checkpoints.load("finalize-resume")
    assert checkpoint is not None
    assert checkpoint["stage"] == "finalizing"
    assert checkpoint["publish_receipt"] is not None
    recovered_teambition = FakeTeambitionAdapter()
    workflow.teambition = recovered_teambition

    result = await workflow.resume("finalize-resume")

    assert result.stage == "completed"
    assert len(change_publisher.published) == 1
    assert recovered_teambition.transitions[0]["ticket_id"] == "TB-9"
