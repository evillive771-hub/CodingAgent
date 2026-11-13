from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from aicoding_agent.adapters.ports import (
    ArtifactBuilder,
    BrowserVerifier,
    BuildReceipt,
    ChangeInspector,
    ChangePublisher,
    DeploymentReceipt,
    PublishReceipt,
    RollbackReceipt,
    StageExecutionError,
    TeambitionPort,
    TestEnvironmentPublisher,
    WorkspaceLease,
    WorkspaceManager,
)
from aicoding_agent.agent.loop import AgentLoop
from aicoding_agent.domain.models import AgentResult, AgentStatus, TargetEnvironment, TaskContract
from aicoding_agent.persistence.base import EventSink, NullEventSink
from aicoding_agent.persistence.jsonl import AtomicCheckpointStore
from aicoding_agent.workflow.approval import ApprovalPackageBuilder, ApprovalPackageError
from aicoding_agent.workflow.models import (
    ApprovalRecord,
    DeliveryStage,
    WorkflowResult,
    WorkflowRun,
)

if TYPE_CHECKING:
    from aicoding_agent.regression.controller import RegressionController


class WorkflowError(RuntimeError):
    pass


ALLOWED_TRANSITIONS: dict[DeliveryStage, set[DeliveryStage]] = {
    DeliveryStage.CREATED: {DeliveryStage.PREPARING_WORKSPACE},
    DeliveryStage.PREPARING_WORKSPACE: {DeliveryStage.CODING, DeliveryStage.FAILED},
    DeliveryStage.CODING: {
        DeliveryStage.REGRESSION_VERIFYING,
        DeliveryStage.INSPECTING_CHANGES,
        DeliveryStage.FAILED,
    },
    DeliveryStage.REGRESSION_VERIFYING: {
        DeliveryStage.INSPECTING_CHANGES,
        DeliveryStage.BLOCKED,
        DeliveryStage.FAILED,
    },
    DeliveryStage.INSPECTING_CHANGES: {
        DeliveryStage.BLOCKED,
        DeliveryStage.BUILDING,
        DeliveryStage.TEST_DEPLOYING,
        DeliveryStage.FAILED,
    },
    DeliveryStage.BUILDING: {
        DeliveryStage.TEST_DEPLOYING, DeliveryStage.BLOCKED, DeliveryStage.FAILED,
    },
    DeliveryStage.TEST_DEPLOYING: {
        DeliveryStage.BLOCKED,
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
        DeliveryStage.COMPLETED,
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
    DeliveryStage.BLOCKED: set(),
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
        checkpoints: AtomicCheckpointStore,
        change_publisher: ChangePublisher | None = None,
        teambition: TeambitionPort | None = None,
        event_sink: EventSink | None = None,
        workspace_manager: WorkspaceManager | None = None,
        approval_builder: ApprovalPackageBuilder | None = None,
        artifact_builder: ArtifactBuilder | None = None,
        finalize_external_changes: bool = True,
        regression_controller: RegressionController | None = None,
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
        self.artifact_builder = artifact_builder
        self.finalize_external_changes = finalize_external_changes
        self.regression_controller = regression_controller

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
        return await self._advance_to_approval(run)

    async def resume(self, run_id: str) -> WorkflowResult:
        payload = await self.checkpoints.load(run_id)
        if payload is None:
            raise WorkflowError(f"Unknown workflow run: {run_id}")
        run = WorkflowRun.model_validate(payload)
        contract = TaskContract.model_validate(run.contract)
        if contract.target_environment != TargetEnvironment.TEST:
            raise WorkflowError("DeliveryWorkflow only permits the test target environment")
        if run.stage in {
            DeliveryStage.AWAITING_APPROVAL,
            DeliveryStage.COMPLETED,
            DeliveryStage.FAILED,
            DeliveryStage.BLOCKED,
            DeliveryStage.ROLLED_BACK,
        }:
            return self._result(run)
        await self._save(run, "workflow.resume_started")
        if run.stage == DeliveryStage.FINALIZING:
            return await self._finalize(run)
        if run.stage == DeliveryStage.ROLLING_BACK:
            receipt = DeploymentReceipt.model_validate(run.deployment_receipt)
            await self._rollback(
                run,
                receipt,
                idempotency_key=run.rollback_idempotency_key or f"{run_id}:resume-rollback",
            )
            return self._result(run)
        return await self._advance_to_approval(run)

    async def _advance_to_approval(self, run: WorkflowRun) -> WorkflowResult:
        contract = TaskContract.model_validate(run.contract)
        source_workspace = Path(run.source_workspace or run.workspace).resolve()
        try:
            if run.stage == DeliveryStage.CREATED:
                await self._transition(run, DeliveryStage.PREPARING_WORKSPACE)
            if run.stage == DeliveryStage.PREPARING_WORKSPACE:
                if self.workspace_manager is not None:
                    lease = await self.workspace_manager.prepare(
                        source_workspace,
                        contract,
                        run_id=run.run_id,
                    )
                    run.workspace = lease.path
                    run.workspace_lease = lease.model_dump(mode="json")
                    await self._save(run, "workflow.workspace_prepared")
                await self._transition(run, DeliveryStage.CODING)
            active_workspace = Path(run.workspace)

            if run.stage == DeliveryStage.CODING:
                if self.regression_controller is not None:
                    await self._transition(run, DeliveryStage.REGRESSION_VERIFYING)
                else:
                    await self._complete_legacy_coding(run, contract, active_workspace)

            if run.stage == DeliveryStage.REGRESSION_VERIFYING:
                if self.regression_controller is None:
                    raise WorkflowError("Cannot resume regression without a regression controller")
                # The controller snapshots the actual source files and owns its candidates.
                # A workspace manager's lease continues to refer to its original worktree.
                outcome = await self.regression_controller.run(
                    contract,
                    source_workspace,
                    run_id=run.run_id,
                    agent=self.agent,
                )
                run.regression_outcome = outcome.model_dump(mode="json")
                run.regression_report = (
                    outcome.report.model_dump(mode="json") if outcome.report is not None else None
                )
                run.agent_result = outcome.agent_result.model_dump(mode="json")
                if outcome.workspace is not None:
                    run.workspace = str(outcome.workspace)
                active_workspace = Path(run.workspace)
                await self._save(run, "workflow.regression_verified")
                if outcome.status != "accepted":
                    run.failure_reason = (
                        outcome.agent_result.reason
                        or "Candidate did not pass regression acceptance"
                    )
                    await self._transition(run, DeliveryStage.BLOCKED)
                    return self._result(run)
                if outcome.agent_result.status != AgentStatus.COMPLETED:
                    raise WorkflowError(
                        "Accepted regression candidate has no completed Agent result"
                    )
                await self._transition(run, DeliveryStage.INSPECTING_CHANGES)

            if run.stage in {
                DeliveryStage.INSPECTING_CHANGES,
                DeliveryStage.BUILDING,
                DeliveryStage.TEST_DEPLOYING,
            } and not await self._validate_regression(run):
                return self._result(run)

            if run.stage == DeliveryStage.INSPECTING_CHANGES:
                if run.change_snapshot is None:
                    change_snapshot = await self.change_inspector.snapshot(active_workspace)
                    run.change_snapshot = change_snapshot.model_dump(mode="json")
                    await self._save(run, "workflow.changes_inspected")
                await self._transition(
                    run,
                    DeliveryStage.BUILDING
                    if self.artifact_builder is not None
                    else DeliveryStage.TEST_DEPLOYING,
                )

            if run.stage == DeliveryStage.BUILDING:
                if not await self._validate_regression(run):
                    return self._result(run)
                if self.artifact_builder is None:
                    raise WorkflowError("Cannot resume build without an artifact builder")
                if run.build_receipt is None:
                    change_hash = str((run.change_snapshot or {}).get("change_hash", ""))
                    build = await self.artifact_builder.build(
                        active_workspace,
                        contract,
                        change_hash=change_hash,
                        idempotency_key=f"{run.run_id}:build",
                    )
                    run.build_receipt = build.model_dump(mode="json")
                    await self._save(run, "workflow.build_completed")
                await self._transition(run, DeliveryStage.TEST_DEPLOYING)

            if run.stage == DeliveryStage.TEST_DEPLOYING:
                if not await self._validate_regression(run):
                    return self._result(run)
                if run.deployment_receipt is None:
                    change_hash = str((run.change_snapshot or {}).get("change_hash", ""))
                    deployment = await self.test_publisher.publish(
                        active_workspace,
                        contract,
                        change_hash=change_hash,
                        build_receipt=(
                            BuildReceipt.model_validate(run.build_receipt)
                            if run.build_receipt is not None
                            else None
                        ),
                        idempotency_key=f"{run.run_id}:test-deploy",
                    )
                    run.deployment_receipt = deployment.model_dump(mode="json")
                    await self._save(run, "workflow.test_deployed")
                await self._transition(run, DeliveryStage.TEST_VERIFYING)

            if run.stage == DeliveryStage.TEST_VERIFYING:
                deployment = DeploymentReceipt.model_validate(run.deployment_receipt)
                if run.verification_report is None:
                    report = await self.browser_verifier.verify(deployment, contract)
                    run.verification_report = report.model_dump(mode="json")
                    await self._save(run, "workflow.test_verified")
                if not bool((run.verification_report or {}).get("ok")):
                    summary = str((run.verification_report or {}).get("summary", "unknown"))
                    raise WorkflowError(f"Test environment verification failed: {summary}")
                if run.approval_package is None:
                    approval_package = self.approval_builder.build(run)
                    run.approval_package = approval_package.model_dump(mode="json")
                    run.approval_subject_hash = approval_package.subject_hash
                    await self._save(run, "workflow.approval_package_built")
                await self._transition(run, DeliveryStage.AWAITING_APPROVAL)
            return self._result(run)
        except Exception as exc:
            run.failure_reason = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, StageExecutionError):
                run.failure_evidence_refs = list(exc.evidence_refs)
                run.rollback_status = exc.rollback_status
            if run.deployment_receipt is not None:
                deployment = DeploymentReceipt.model_validate(run.deployment_receipt)
                await self._rollback(
                    run,
                    deployment,
                    idempotency_key=f"{run.run_id}:rollback",
                )
            else:
                await self._transition(run, DeliveryStage.FAILED)
            return self._result(run)

    async def _validate_regression(self, run: WorkflowRun) -> bool:
        if run.regression_outcome is None:
            return True
        if self.regression_controller is not None and await (
            self.regression_controller.validate_checkpoint(run.run_id)
        ):
            return True
        run.failure_reason = "Regression evidence became stale before delivery"
        await self._transition(run, DeliveryStage.BLOCKED)
        return False

    async def _complete_legacy_coding(
        self, run: WorkflowRun, contract: TaskContract, workspace: Path
    ) -> None:
        if run.agent_result is None:
            agent_result = await self.agent.run(contract, workspace, run_id=run.run_id)
            run.agent_result = agent_result.model_dump(mode="json")
            await self._save(run, "workflow.coding_completed")
        else:
            agent_result = AgentResult.model_validate(run.agent_result)
        if agent_result.status != AgentStatus.COMPLETED:
            raise WorkflowError(agent_result.reason or "Coding Agent did not complete")
        await self._transition(run, DeliveryStage.INSPECTING_CHANGES)

    async def decide(self, run_id: str, approval: ApprovalRecord) -> WorkflowResult:
        payload = await self.checkpoints.load(run_id)
        if payload is None:
            raise WorkflowError(f"Unknown workflow run: {run_id}")
        run = WorkflowRun.model_validate(payload)
        if run.stage != DeliveryStage.AWAITING_APPROVAL:
            raise WorkflowError(f"Run is not awaiting approval: {run.stage}")
        if approval.subject_hash != run.approval_subject_hash:
            raise WorkflowError("Approval hash does not match the current change and evidence")
        if approval.decision == "approved" and (
            run.contract.get("tdd") is not None
            or (run.regression_outcome or {}).get("tdd_evidence") is not None
        ):
            try:
                current_package = self.approval_builder.build(run)
            except ApprovalPackageError as exc:
                raise WorkflowError("Approval subject has invalid TDD evidence") from exc
            if current_package.subject_hash != run.approval_subject_hash:
                raise WorkflowError("Approval subject no longer matches the TDD evidence")
            if self.regression_controller is None or not await (
                self.regression_controller.validate_checkpoint(run.run_id)
            ):
                raise WorkflowError("TDD evidence became stale before approval")
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

        if not self.finalize_external_changes:
            await self._transition(run, DeliveryStage.COMPLETED)
            await self._save(run, "workflow.manual_approval_completed")
            await self._release_workspace(run, suppress_errors=True)
            return self._result(run)

        await self._transition(run, DeliveryStage.FINALIZING)
        return await self._finalize(run)

    async def _finalize(self, run: WorkflowRun) -> WorkflowResult:
        try:
            contract = TaskContract.model_validate(run.contract)
            current = await self.change_inspector.snapshot(Path(run.workspace))
            expected = str((run.change_snapshot or {})["change_hash"])
            if current.change_hash != expected:
                run.failure_reason = "Workspace changed after approval was requested"
                receipt = DeploymentReceipt.model_validate(run.deployment_receipt)
                await self._rollback(
                    run,
                    receipt,
                    idempotency_key=f"{run.run_id}:changed-after-approval-rollback",
                )
                return self._result(run)
            if run.publish_receipt is None:
                if self.change_publisher is None:
                    raise WorkflowError("No Git change publisher is configured")
                published = await self.change_publisher.publish(
                    Path(run.workspace),
                    contract,
                    expected_change_hash=expected,
                    idempotency_key=f"{run.run_id}:git-publish",
                )
                run.publish_receipt = published.model_dump(mode="json")
                await self._save(run, "workflow.git_published")
            else:
                published = PublishReceipt.model_validate(run.publish_receipt)
            if contract.source_ticket_id:
                if self.teambition is None:
                    raise WorkflowError("No Teambition mutation adapter is configured")
                evidence = list((run.verification_report or {}).get("evidence_refs", []))
                await self.teambition.transition_to_testing(
                    contract.source_ticket_id,
                    revision=published.revision,
                    evidence_refs=evidence,
                    idempotency_key=f"{run.run_id}:tb-testing",
                )
                await self._save(run, "workflow.teambition_updated")
            await self._transition(run, DeliveryStage.COMPLETED)
            await self._release_workspace(run, suppress_errors=True)
        except Exception as exc:
            run.failure_reason = f"Finalization failed: {type(exc).__name__}: {exc}"
            receipt = DeploymentReceipt.model_validate(run.deployment_receipt)
            await self._rollback(
                run,
                receipt,
                idempotency_key=f"{run.run_id}:finalization-rollback",
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
            DeliveryStage.BLOCKED,
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
            run.rollback_idempotency_key = idempotency_key
            if run.stage != DeliveryStage.ROLLING_BACK:
                await self._transition(run, DeliveryStage.ROLLING_BACK)
            else:
                await self._save(run, "workflow.rollback_resumed")
            rollback = await self.test_publisher.rollback(
                receipt, idempotency_key=idempotency_key
            )
            if rollback is not None:
                validated = RollbackReceipt.model_validate(rollback)
                run.rollback_receipt = validated.model_dump(mode="json")
            run.rollback_status = "succeeded"
            await self._transition(run, DeliveryStage.ROLLED_BACK)
        except Exception as exc:
            run.rollback_status = "failed"
            if isinstance(exc, StageExecutionError):
                run.failure_evidence_refs.extend(exc.evidence_refs)
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
        evidence_refs: list[str] = []
        evidence_refs.extend(run.failure_evidence_refs)
        if run.regression_report is not None:
            report_path = run.regression_report.get("report_path")
            if isinstance(report_path, str) and report_path:
                evidence_refs.append(report_path)
            for test in run.regression_report.get("tests", []):
                evidence_refs.extend(
                    value for value in test.get("evidence_refs", []) if isinstance(value, str)
                )
        tdd_evidence = (run.regression_outcome or {}).get("tdd_evidence")
        if isinstance(tdd_evidence, dict):
            red_report = tdd_evidence.get("red_report", {})
            report_path = red_report.get("report_path")
            if isinstance(report_path, str) and report_path:
                evidence_refs.append(report_path)
            for test in red_report.get("tests", []):
                evidence_refs.extend(
                    value for value in test.get("evidence_refs", []) if isinstance(value, str)
                )
        if run.build_receipt is not None:
            manifest = run.build_receipt.get("manifest_ref")
            if isinstance(manifest, str):
                evidence_refs.append(manifest)
            evidence_refs.extend(
                value
                for value in run.build_receipt.get("command_evidence_refs", [])
                if isinstance(value, str)
            )
        for payload in (run.regression_report, run.deployment_receipt, run.verification_report):
            if payload is not None:
                evidence_refs.extend(
                    value for value in payload.get("evidence_refs", []) if isinstance(value, str)
                )
        if run.rollback_receipt is not None:
            evidence_refs.extend(
                value
                for value in run.rollback_receipt.get("evidence_refs", [])
                if isinstance(value, str)
            )
        return WorkflowResult(
            run_id=run.run_id,
            stage=run.stage,
            approval_subject_hash=run.approval_subject_hash,
            failure_reason=run.failure_reason,
            workspace_released=run.workspace_released,
            workspace_cleanup_error=run.workspace_cleanup_error,
            evidence_refs=list(dict.fromkeys(evidence_refs)),
            rollback_status=run.rollback_status,
        )
