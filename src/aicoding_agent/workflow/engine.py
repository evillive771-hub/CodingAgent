from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from aicoding_agent.adapters.ports import (
    BrowserVerifier,
    ChangeInspector,
    ChangePublisher,
    DeploymentReceipt,
    TeambitionPort,
    TestEnvironmentPublisher,
    WorkspaceLease,
    WorkspaceManager,
)
from aicoding_agent.agent.loop import AgentLoop
from aicoding_agent.domain.models import AgentStatus, TargetEnvironment, TaskContract
from aicoding_agent.persistence.base import EventSink, NullEventSink
from aicoding_agent.persistence.jsonl import AtomicCheckpointStore
from aicoding_agent.workflow.approval import ApprovalPackageBuilder
from aicoding_agent.workflow.models import (
    ApprovalRecord,
    DeliveryStage,
    WorkflowResult,
    WorkflowRun,
)


class WorkflowError(RuntimeError):
    pass


ALLOWED_TRANSITIONS: dict[DeliveryStage, set[DeliveryStage]] = {
    DeliveryStage.CREATED: {DeliveryStage.PREPARING_WORKSPACE},
    DeliveryStage.PREPARING_WORKSPACE: {DeliveryStage.CODING, DeliveryStage.FAILED},
    DeliveryStage.CODING: {DeliveryStage.INSPECTING_CHANGES, DeliveryStage.FAILED},
    DeliveryStage.INSPECTING_CHANGES: {DeliveryStage.TEST_DEPLOYING, DeliveryStage.FAILED},
    DeliveryStage.TEST_DEPLOYING: {
        DeliveryStage.TEST_VERIFYING,
        DeliveryStage.FAILED,
        DeliveryStage.ROLLING_BACK,
    },
    DeliveryStage.TEST_VERIFYING: {
        DeliveryStage.AWAITING_APPROVAL,
        DeliveryStage.ROLLING_BACK,
    },
    DeliveryStage.AWAITING_APPROVAL: {
        DeliveryStage.FINALIZING,
        DeliveryStage.ROLLING_BACK,
    },
    DeliveryStage.FINALIZING: {
        DeliveryStage.COMPLETED,
        DeliveryStage.FAILED,
        DeliveryStage.ROLLING_BACK,
    },
    DeliveryStage.ROLLING_BACK: {DeliveryStage.ROLLED_BACK, DeliveryStage.FAILED},
    DeliveryStage.COMPLETED: set(),
    DeliveryStage.FAILED: set(),
    DeliveryStage.ROLLED_BACK: set(),
}


class DeliveryWorkflow:
    """Deterministic outer workflow. No release operation is exposed to the model."""

    def __init__(
        self,
        *,
        agent: AgentLoop,
        change_inspector: ChangeInspector,
        test_publisher: TestEnvironmentPublisher,
        browser_verifier: BrowserVerifier,
        change_publisher: ChangePublisher,
        teambition: TeambitionPort,
        checkpoints: AtomicCheckpointStore,
        event_sink: EventSink | None = None,
        workspace_manager: WorkspaceManager | None = None,
        approval_builder: ApprovalPackageBuilder | None = None,
    ) -> None:
        self.agent = agent
        self.change_inspector = change_inspector
        self.test_publisher = test_publisher
        self.browser_verifier = browser_verifier
        self.change_publisher = change_publisher
        self.teambition = teambition
        self.checkpoints = checkpoints
        self.event_sink = event_sink or NullEventSink()
        self.workspace_manager = workspace_manager
        self.approval_builder = approval_builder or ApprovalPackageBuilder()

    async def execute(
        self,
        contract: TaskContract,
        workspace: Path,
        *,
        run_id: str | None = None,
    ) -> WorkflowResult:
        if contract.target_environment != TargetEnvironment.TEST:
            raise WorkflowError("DeliveryWorkflow only permits the test target environment")
        run_id = run_id or uuid4().hex
        if await self.checkpoints.load(run_id) is not None:
            raise WorkflowError(f"Workflow run already exists: {run_id}")
        source_workspace = workspace.resolve()
        run = WorkflowRun(
            run_id=run_id,
            task_id=contract.task_id,
            source_workspace=str(source_workspace),
            workspace=str(source_workspace),
            contract=contract.model_dump(mode="json"),
        )
        await self._save(run, "workflow.created")
        deployment: DeploymentReceipt | None = None
        try:
            await self._transition(run, DeliveryStage.PREPARING_WORKSPACE)
            if self.workspace_manager is not None:
                lease = await self.workspace_manager.prepare(
                    source_workspace,
                    contract,
                    run_id=run_id,
                )
                run.workspace = lease.path
                run.workspace_lease = lease.model_dump(mode="json")
                await self._save(run, "workflow.workspace_prepared")
            active_workspace = Path(run.workspace)

            await self._transition(run, DeliveryStage.CODING)
            agent_result = await self.agent.run(contract, active_workspace, run_id=run_id)
            run.agent_result = agent_result.model_dump(mode="json")
            if agent_result.status != AgentStatus.COMPLETED:
                raise WorkflowError(agent_result.reason or "Coding Agent did not complete")

            await self._transition(run, DeliveryStage.INSPECTING_CHANGES)
            change_snapshot = await self.change_inspector.snapshot(active_workspace)
            run.change_snapshot = change_snapshot.model_dump(mode="json")

            await self._transition(run, DeliveryStage.TEST_DEPLOYING)
            deployment = await self.test_publisher.publish(
                active_workspace,
                contract,
                change_hash=change_snapshot.change_hash,
                idempotency_key=f"{run_id}:test-deploy",
            )
            run.deployment_receipt = deployment.model_dump(mode="json")

            await self._transition(run, DeliveryStage.TEST_VERIFYING)
            report = await self.browser_verifier.verify(deployment, contract)
            run.verification_report = report.model_dump(mode="json")
            if not report.ok:
                raise WorkflowError(f"Test environment verification failed: {report.summary}")

            approval_package = self.approval_builder.build(run)
            run.approval_package = approval_package.model_dump(mode="json")
            run.approval_subject_hash = approval_package.subject_hash
            await self._transition(run, DeliveryStage.AWAITING_APPROVAL)
            return self._result(run)
        except Exception as exc:
            run.failure_reason = f"{type(exc).__name__}: {exc}"
            if deployment is not None:
                try:
                    await self._transition(run, DeliveryStage.ROLLING_BACK)
                    await self.test_publisher.rollback(
                        deployment,
                        idempotency_key=f"{run_id}:rollback",
                    )
                    await self._transition(run, DeliveryStage.ROLLED_BACK)
                except Exception as rollback_exc:
                    run.failure_reason += (
                        f"; rollback failed: {type(rollback_exc).__name__}: {rollback_exc}"
                    )
                    await self._transition(run, DeliveryStage.FAILED)
            else:
                await self._transition(run, DeliveryStage.FAILED)
            return self._result(run)

    async def decide(self, run_id: str, approval: ApprovalRecord) -> WorkflowResult:
        payload = await self.checkpoints.load(run_id)
        if payload is None:
            raise WorkflowError(f"Unknown workflow run: {run_id}")
        run = WorkflowRun.model_validate(payload)
        if run.stage != DeliveryStage.AWAITING_APPROVAL:
            raise WorkflowError(f"Run is not awaiting approval: {run.stage}")
        if approval.subject_hash != run.approval_subject_hash:
            raise WorkflowError("Approval hash does not match the current change and evidence")
        run.approval = approval.model_dump(mode="json")
        if approval.decision == "rejected":
            run.failure_reason = f"Rejected by {approval.approver}: {approval.comment}"
            receipt = DeploymentReceipt.model_validate(run.deployment_receipt)
            await self._rollback(
                run,
                receipt,
                idempotency_key=f"{run_id}:approval-rejected-rollback",
            )
            return self._result(run)

        await self._transition(run, DeliveryStage.FINALIZING)
        try:
            contract = TaskContract.model_validate(run.contract)
            current = await self.change_inspector.snapshot(Path(run.workspace))
            expected = str(run.change_snapshot["change_hash"])
            if current.change_hash != expected:
                run.failure_reason = "Workspace changed after approval was requested"
                receipt = DeploymentReceipt.model_validate(run.deployment_receipt)
                await self._rollback(
                    run,
                    receipt,
                    idempotency_key=f"{run_id}:changed-after-approval-rollback",
                )
                return self._result(run)
            published = await self.change_publisher.publish(
                Path(run.workspace),
                contract,
                expected_change_hash=expected,
                idempotency_key=f"{run_id}:git-publish",
            )
            run.publish_receipt = published.model_dump(mode="json")
            if contract.source_ticket_id:
                evidence = list((run.verification_report or {}).get("evidence_refs", []))
                await self.teambition.transition_to_testing(
                    contract.source_ticket_id,
                    revision=published.revision,
                    evidence_refs=evidence,
                    idempotency_key=f"{run_id}:tb-testing",
                )
            await self._transition(run, DeliveryStage.COMPLETED)
            await self._release_workspace(run, suppress_errors=True)
        except Exception as exc:
            run.failure_reason = f"Finalization failed: {type(exc).__name__}: {exc}"
            receipt = DeploymentReceipt.model_validate(run.deployment_receipt)
            await self._rollback(
                run,
                receipt,
                idempotency_key=f"{run_id}:finalization-rollback",
            )
        return self._result(run)

    async def cleanup_workspace(self, run_id: str) -> WorkflowResult:
        payload = await self.checkpoints.load(run_id)
        if payload is None:
            raise WorkflowError(f"Unknown workflow run: {run_id}")
        run = WorkflowRun.model_validate(payload)
        if run.stage not in {
            DeliveryStage.COMPLETED,
            DeliveryStage.FAILED,
            DeliveryStage.ROLLED_BACK,
        }:
            raise WorkflowError(f"Cannot release a non-terminal workspace: {run.stage}")
        if run.workspace_lease is not None and self.workspace_manager is None:
            raise WorkflowError("The workflow has no workspace manager configured")
        await self._release_workspace(run, suppress_errors=False)
        return self._result(run)

    async def _transition(self, run: WorkflowRun, stage: DeliveryStage) -> None:
        if stage not in ALLOWED_TRANSITIONS[run.stage]:
            raise WorkflowError(f"Invalid workflow transition: {run.stage} -> {stage}")
        run.stage = stage
        run.updated_at = datetime.now(UTC)
        await self._save(run, "workflow.stage_changed")

    async def _save(self, run: WorkflowRun, event_type: str) -> None:
        payload = run.model_dump(mode="json")
        await self.checkpoints.save(run.run_id, payload)
        await self.event_sink.emit(
            run.run_id,
            event_type,
            {
                "stage": run.stage,
                "task_id": run.task_id,
                "approval_subject_hash": run.approval_subject_hash,
                "failure_reason": run.failure_reason,
                "workspace_cleanup_error": run.workspace_cleanup_error,
            },
        )

    async def _rollback(
        self,
        run: WorkflowRun,
        receipt: DeploymentReceipt,
        *,
        idempotency_key: str,
    ) -> None:
        try:
            await self._transition(run, DeliveryStage.ROLLING_BACK)
            await self.test_publisher.rollback(receipt, idempotency_key=idempotency_key)
            await self._transition(run, DeliveryStage.ROLLED_BACK)
        except Exception as exc:
            run.failure_reason = (
                f"{run.failure_reason or ''}; rollback failed: {type(exc).__name__}: {exc}"
            ).lstrip("; ")
            if run.stage == DeliveryStage.ROLLING_BACK:
                await self._transition(run, DeliveryStage.FAILED)
            else:
                await self._save(run, "workflow.rollback_failed")

    async def _release_workspace(
        self,
        run: WorkflowRun,
        *,
        suppress_errors: bool,
    ) -> None:
        if self.workspace_manager is None or run.workspace_lease is None or run.workspace_released:
            return
        try:
            lease = WorkspaceLease.model_validate(run.workspace_lease)
            await self.workspace_manager.release(lease)
            run.workspace_released = True
            run.workspace_cleanup_error = None
            await self._save(run, "workflow.workspace_released")
        except Exception as exc:
            run.workspace_cleanup_error = f"{type(exc).__name__}: {exc}"
            await self._save(run, "workflow.workspace_cleanup_failed")
            if not suppress_errors:
                raise WorkflowError(run.workspace_cleanup_error) from exc

    @staticmethod
    def _result(run: WorkflowRun) -> WorkflowResult:
        return WorkflowResult(
            run_id=run.run_id,
            stage=run.stage,
            approval_subject_hash=run.approval_subject_hash,
            failure_reason=run.failure_reason,
            workspace_released=run.workspace_released,
            workspace_cleanup_error=run.workspace_cleanup_error,
        )
