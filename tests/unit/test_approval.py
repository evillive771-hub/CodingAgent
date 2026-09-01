from __future__ import annotations

from copy import deepcopy

import pytest

from aicoding_agent.domain.models import TaskContract
from aicoding_agent.workflow.approval import ApprovalPackageBuilder, ApprovalPackageError
from aicoding_agent.workflow.models import WorkflowRun


def workflow_run() -> WorkflowRun:
    contract = TaskContract(
        task_id="approval-1",
        objective="Build a stable approval package",
        acceptance_criteria=["Evidence and changes are hash-bound"],
        target_environment="test",
    )
    return WorkflowRun(
        run_id="approval-run",
        task_id=contract.task_id,
        workspace="D:/worktree",
        contract=contract.model_dump(mode="json"),
        agent_result={
            "status": "completed",
            "run_id": "approval-run",
            "final_text": "Done",
            "outcome": {
                "summary": "Fixed the issue",
                "changed_files": ["app.ts"],
                "risk_notes": [],
            },
            "turns": 3,
            "tool_calls": 7,
            "reason": None,
        },
        change_snapshot={
            "change_hash": "a" * 64,
            "base_revision": "b" * 40,
            "changed_files": ["app.ts"],
        },
        deployment_receipt={
            "deployment_id": "deploy-1",
            "environment": "test",
            "artifact_hash": "c" * 64,
            "location": "https://test.example.invalid/deploy-1",
        },
        verification_report={
            "ok": True,
            "summary": "Acceptance scenario passed",
            "evidence_refs": ["evidence://browser/report.json"],
        },
    )


def test_approval_package_hash_is_stable_and_excludes_final_model_text() -> None:
    builder = ApprovalPackageBuilder()
    run = workflow_run()

    first = builder.build(run)
    run.agent_result["final_text"] = "Different prose that is not approval evidence"
    second = builder.build(run)

    assert first.subject_hash == second.subject_hash
    assert first.subject == second.subject
    assert "final_text" not in first.subject["agent"]


def test_approval_package_hash_changes_with_contract_or_evidence() -> None:
    builder = ApprovalPackageBuilder()
    original = workflow_run()
    changed_contract = original.model_copy(deep=True)
    changed_contract.contract["objective"] = "A different objective"
    changed_evidence = original.model_copy(deep=True)
    changed_evidence.verification_report["evidence_refs"] = ["evidence://different"]

    original_hash = builder.build(original).subject_hash

    assert builder.build(changed_contract).subject_hash != original_hash
    assert builder.build(changed_evidence).subject_hash != original_hash


def test_approval_package_requires_successful_evidence() -> None:
    run = workflow_run()
    report = deepcopy(run.verification_report)
    report["evidence_refs"] = []
    run.verification_report = report

    with pytest.raises(ApprovalPackageError, match="requires verification evidence"):
        ApprovalPackageBuilder().build(run)
