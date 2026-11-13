from pathlib import Path

import pytest
from pydantic import BaseModel

from aicoding_agent.adapters.fakes import (
    FakeBrowserVerifier,
    FakeChangeInspector,
    FakeTestEnvironmentPublisher,
    FakeWorkspaceManager,
)
from aicoding_agent.domain.models import AgentResult, AgentStatus, CodingOutcome, TaskContract
from aicoding_agent.persistence.jsonl import AtomicCheckpointStore
from aicoding_agent.workflow.engine import DeliveryWorkflow
from aicoding_agent.workflow.models import ApprovalRecord


class Report(BaseModel):
    evidence_refs: list[str]
    candidate_fingerprint: str = "a" * 64


class Outcome(BaseModel):
    status: str
    agent_result: AgentResult
    workspace: Path
    report: Report | None
    baseline_revision: str | None = None


class CompletedAgent:
    def __init__(self):
        self.calls = 0

    async def run(self, contract, workspace, *, run_id=None):
        self.calls += 1
        return AgentResult(
            status=AgentStatus.COMPLETED,
            run_id=run_id,
            outcome=CodingOutcome(summary="Fixed", changed_files=["app.py"]),
        )


class HostCrash(BaseException):
    pass


class Controller:
    """Exercise the workflow boundary, including idempotent controller recovery."""

    def __init__(self, candidate, *, accepted=True, crash_once=False):
        self.candidate = candidate
        self.accepted = accepted
        self.crash_once = crash_once
        self.sources = []
        self.outcome = None

    async def run(self, contract, source_workspace, *, run_id, agent):
        self.sources.append(source_workspace)
        if self.outcome is None:
            result = await agent.run(contract, self.candidate, run_id=run_id)
            self.outcome = Outcome(
                status="accepted" if self.accepted else "blocked",
                agent_result=result,
                workspace=self.candidate,
                report=Report(evidence_refs=["fake://regression/report"]),
                baseline_revision="b" * 64 if self.accepted else None,
            )
        if self.crash_once:
            self.crash_once = False
            raise HostCrash("crash after persisted regression result")
        return self.outcome

    async def validate_checkpoint(self, run_id):
        return self.outcome is not None and self.outcome.status == "accepted"


def setup(tmp_path, *, accepted=True, crash_once=False):
    source = tmp_path / "source"
    source.mkdir()
    (source / "uncommitted.py").write_text("print('source change')", encoding="utf-8")
    candidate = tmp_path / "retained-candidate"
    candidate.mkdir()
    (candidate / "app.py").write_text("print('fixed')", encoding="utf-8")
    controller = Controller(candidate, accepted=accepted, crash_once=crash_once)
    publisher = FakeTestEnvironmentPublisher()
    agent = CompletedAgent()
    manager = FakeWorkspaceManager(tmp_path / "manager-worktrees")
    workflow = DeliveryWorkflow(
        agent=agent,
        change_inspector=FakeChangeInspector("a" * 64),
        test_publisher=publisher,
        browser_verifier=FakeBrowserVerifier(),
        checkpoints=AtomicCheckpointStore(tmp_path / "checkpoints"),
        workspace_manager=manager,
        finalize_external_changes=False,
        regression_controller=controller,
    )
    contract = TaskContract(
        task_id="regression-task",
        objective="Fix without breaking old features",
        acceptance_criteria=["All command and browser regressions pass"],
        target_environment="test",
    )
    return workflow, contract, source, controller, publisher, manager, agent


@pytest.mark.asyncio
async def test_regression_acceptance_uses_candidate_and_binds_report(tmp_path):
    workflow, contract, source, controller, publisher, manager, agent = setup(tmp_path)
    result = await workflow.execute(contract, source, run_id="accepted")
    assert result.stage == "awaiting_approval"
    assert agent.calls == 1
    assert controller.sources == [source.resolve()]
    checkpoint = await workflow.checkpoints.load("accepted")
    assert checkpoint["workspace"] == str(controller.candidate)
    assert checkpoint["approval_package"]["subject"]["regression_report"] == (
        checkpoint["regression_report"]
    )
    assert checkpoint["approval_package"]["subject"]["accepted_baseline_revision"] == "b" * 64
    assert "fake://regression/report" in result.evidence_refs
    assert len(publisher.published) == 1
    completed = await workflow.decide(
        "accepted",
        ApprovalRecord(
            decision="approved", subject_hash=result.approval_subject_hash, approver="owner"
        ),
    )
    assert completed.stage == "completed"
    assert manager.released == [manager.prepared[0].lease_id]
    assert controller.candidate.is_dir()


@pytest.mark.asyncio
async def test_regression_blocked_never_deploys_and_remains_terminal(tmp_path):
    workflow, contract, source, controller, publisher, manager, agent = setup(
        tmp_path, accepted=False
    )
    result = await workflow.execute(contract, source, run_id="blocked")
    assert result.stage == "blocked"
    assert not publisher.published
    assert not manager.released
    assert controller.candidate.is_dir()
    assert "fake://regression/report" in result.evidence_refs
    resumed = await workflow.resume("blocked")
    assert resumed.stage == "blocked"
    assert agent.calls == 1
    assert len(controller.sources) == 1


@pytest.mark.asyncio
async def test_regression_resume_reuses_controller_result_without_repeating_agent(tmp_path):
    workflow, contract, source, controller, publisher, _, agent = setup(tmp_path, crash_once=True)
    with pytest.raises(HostCrash):
        await workflow.execute(contract, source, run_id="crash")
    checkpoint = await workflow.checkpoints.load("crash")
    assert checkpoint["stage"] == "regression_verifying"
    assert not publisher.published
    resumed = await workflow.resume("crash")
    assert resumed.stage == "awaiting_approval"
    assert agent.calls == 1
    assert len(publisher.published) == 1
    assert controller.sources == [source.resolve(), source.resolve()]


@pytest.mark.asyncio
async def test_stale_regression_after_inspection_blocks_deployment(tmp_path):
    workflow, contract, source, controller, publisher, _, _ = setup(tmp_path)

    class InvalidatingInspector(FakeChangeInspector):
        async def snapshot(self, workspace):
            snapshot = await super().snapshot(workspace)
            controller.outcome.status = "blocked"
            return snapshot

    workflow.change_inspector = InvalidatingInspector("a" * 64)
    result = await workflow.execute(contract, source, run_id="stale")
    assert result.stage == "blocked"
    assert not publisher.published
    assert "stale" in result.failure_reason


@pytest.mark.asyncio
async def test_blocked_without_report_is_preserved(tmp_path):
    workflow, contract, source, controller, publisher, _, _ = setup(tmp_path, accepted=False)
    original = controller.run

    async def no_report(*args, **kwargs):
        outcome = await original(*args, **kwargs)
        outcome.report = None
        return outcome

    controller.run = no_report
    result = await workflow.execute(contract, source, run_id="no-report")
    assert result.stage == "blocked"
    assert not publisher.published
