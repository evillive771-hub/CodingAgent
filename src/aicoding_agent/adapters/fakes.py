from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from aicoding_agent.adapters.ports import (
    ChangeSnapshot,
    DeploymentReceipt,
    PublishReceipt,
    TicketSnapshot,
    VerificationReport,
    WorkspaceLease,
)
from aicoding_agent.domain.models import TaskContract, canonical_task_contract_payload


class FakeTeambitionAdapter:
    def __init__(self, tickets: dict[str, TicketSnapshot] | None = None) -> None:
        self.tickets = tickets or {}
        self.transitions: list[dict[str, object]] = []

    async def fetch_ticket(self, ticket_id: str) -> TicketSnapshot:
        try:
            return self.tickets[ticket_id]
        except KeyError as exc:
            raise KeyError(f"Unknown fake Teambition ticket: {ticket_id}") from exc

    async def transition_to_testing(
        self,
        ticket_id: str,
        *,
        revision: str,
        evidence_refs: list[str],
        idempotency_key: str,
    ) -> None:
        self.transitions.append(
            {
                "ticket_id": ticket_id,
                "revision": revision,
                "evidence_refs": list(evidence_refs),
                "idempotency_key": idempotency_key,
            }
        )


class FakeChangeInspector:
    def __init__(self, change_hash: str = "fake-change-hash") -> None:
        self.change_hash = change_hash

    async def snapshot(self, workspace: Path) -> ChangeSnapshot:
        files = sorted(
            path.relative_to(workspace).as_posix()
            for path in workspace.rglob("*")
            if path.is_file() and ".git" not in path.parts
        )
        return ChangeSnapshot(change_hash=self.change_hash, changed_files=files)


class FakeWorkspaceManager:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.prepared: list[WorkspaceLease] = []
        self.released: list[str] = []

    async def prepare(
        self,
        repository: Path,
        contract: TaskContract,
        *,
        run_id: str,
    ) -> WorkspaceLease:
        target = self.root / run_id
        target.mkdir(parents=True, exist_ok=True)
        lease = WorkspaceLease(
            lease_id=f"fake-{run_id}",
            run_id=run_id,
            task_id=contract.task_id,
            repository_path=str(repository.resolve()),
            path=str(target),
            base_revision=contract.base_revision or "0" * 40,
            contract_hash=hashlib.sha256(
                json.dumps(
                    canonical_task_contract_payload(contract),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        )
        self.prepared.append(lease)
        return lease

    async def release(self, lease: WorkspaceLease) -> None:
        self.released.append(lease.lease_id)


class FakeTestEnvironmentPublisher:
    def __init__(self, *, fail_publish: bool = False, fail_rollback: bool = False) -> None:
        self.fail_publish = fail_publish
        self.fail_rollback = fail_rollback
        self.published: list[DeploymentReceipt] = []
        self.rolled_back: list[str] = []

    async def publish(
        self,
        workspace: Path,
        contract: TaskContract,
        *,
        change_hash: str,
        idempotency_key: str,
    ) -> DeploymentReceipt:
        if self.fail_publish:
            raise RuntimeError("Fake test deployment failed")
        receipt = DeploymentReceipt(
            deployment_id=uuid4().hex,
            artifact_hash=hashlib.sha256(change_hash.encode()).hexdigest(),
            location=f"fake://test/{contract.task_id}",
        )
        self.published.append(receipt)
        return receipt

    async def rollback(
        self,
        receipt: DeploymentReceipt,
        *,
        idempotency_key: str,
    ) -> None:
        if self.fail_rollback:
            raise RuntimeError("Fake rollback failed")
        self.rolled_back.append(receipt.deployment_id)


class FakeBrowserVerifier:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok

    async def verify(
        self,
        receipt: DeploymentReceipt,
        contract: TaskContract,
    ) -> VerificationReport:
        return VerificationReport(
            ok=self.ok,
            summary="Fake browser verification passed" if self.ok else "Fake browser failure",
            evidence_refs=[f"fake://evidence/{receipt.deployment_id}"],
        )


class FakeChangePublisher:
    def __init__(self) -> None:
        self.published: list[dict[str, str]] = []

    async def publish(
        self,
        workspace: Path,
        contract: TaskContract,
        *,
        expected_change_hash: str,
        idempotency_key: str,
    ) -> PublishReceipt:
        revision = hashlib.sha256(
            f"{contract.task_id}:{expected_change_hash}".encode()
        ).hexdigest()[:40]
        self.published.append(
            {
                "task_id": contract.task_id,
                "change_hash": expected_change_hash,
                "idempotency_key": idempotency_key,
            }
        )
        return PublishReceipt(revision=revision, remote="fake://git/origin")
