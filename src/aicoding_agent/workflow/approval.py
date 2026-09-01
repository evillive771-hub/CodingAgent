from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field

from aicoding_agent.workflow.models import WorkflowModel, WorkflowRun


class ApprovalPackageError(RuntimeError):
    pass


class ApprovalPackage(WorkflowModel):
    schema_version: Literal["1"] = "1"
    subject_hash: str = Field(min_length=64, max_length=64)
    subject: dict[str, Any]
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ApprovalPackageBuilder:
    """Build a stable, human-inspectable approval subject from completed test evidence."""

    def build(self, run: WorkflowRun) -> ApprovalPackage:
        contract = self._required_mapping("contract", run.contract)
        agent_result = self._required_mapping("agent_result", run.agent_result)
        change_snapshot = self._required_mapping("change_snapshot", run.change_snapshot)
        deployment = self._required_mapping("deployment_receipt", run.deployment_receipt)
        verification = self._required_mapping("verification_report", run.verification_report)

        if contract.get("task_id") != run.task_id:
            raise ApprovalPackageError("Workflow task id does not match its frozen contract")
        if agent_result.get("status") != "completed" or not agent_result.get("outcome"):
            raise ApprovalPackageError("Approval package requires a completed Agent outcome")
        change_hash = str(change_snapshot.get("change_hash", ""))
        if not re.fullmatch(r"[a-fA-F0-9]{64}", change_hash):
            raise ApprovalPackageError("Approval package requires a SHA-256 change hash")
        if deployment.get("environment") != "test":
            raise ApprovalPackageError("Approval package only permits a test deployment")
        if verification.get("ok") is not True:
            raise ApprovalPackageError("Approval package requires successful verification")
        evidence_refs = verification.get("evidence_refs")
        if not isinstance(evidence_refs, list) or not evidence_refs:
            raise ApprovalPackageError("Approval package requires verification evidence")

        contract_hash = self._hash(contract)
        if run.workspace_lease is not None:
            lease_contract_hash = run.workspace_lease.get("contract_hash")
            if lease_contract_hash != contract_hash:
                raise ApprovalPackageError("Workspace lease does not match the frozen contract")

        subject = {
            "task": {
                "task_id": run.task_id,
                "source": contract.get("source"),
                "source_ticket_id": contract.get("source_ticket_id"),
                "objective": contract.get("objective"),
                "acceptance_criteria": contract.get("acceptance_criteria"),
                "target_environment": contract.get("target_environment"),
                "base_revision": contract.get("base_revision"),
                "contract_hash": contract_hash,
            },
            "agent": {
                "status": agent_result.get("status"),
                "outcome": agent_result.get("outcome"),
                "turns": agent_result.get("turns"),
                "tool_calls": agent_result.get("tool_calls"),
            },
            "change_snapshot": change_snapshot,
            "deployment_receipt": deployment,
            "verification_report": verification,
        }
        canonical_subject = self._canonical_copy(subject)
        return ApprovalPackage(
            subject_hash=self._hash(canonical_subject),
            subject=canonical_subject,
        )

    @staticmethod
    def _required_mapping(name: str, value: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ApprovalPackageError(f"Approval package is missing {name}")
        return value

    @staticmethod
    def _canonical_copy(value: dict[str, Any]) -> dict[str, Any]:
        return json.loads(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

    @staticmethod
    def _hash(value: dict[str, Any]) -> str:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()
