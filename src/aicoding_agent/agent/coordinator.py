from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pydantic import Field

from aicoding_agent.adapters.git import GitCandidateIntegrator
from aicoding_agent.adapters.ports import ChangeSnapshot, WorkspaceLease, WorkspaceManager
from aicoding_agent.agent.task_graph import (
    TaskGraphManager,
    TaskGraphState,
    TaskNodeStatus,
)
from aicoding_agent.agent.team import (
    PlanApprovalStatus,
    TeamLaunchManager,
    TeamMessageBus,
    TeamMessageType,
    TeamPlanApprovalManager,
    TeamProtocolError,
)
from aicoding_agent.domain.models import (
    AgentResult,
    AgentStatus,
    StrictModel,
    TargetEnvironment,
    TaskContract,
    VerificationCommand,
)
from aicoding_agent.persistence.base import validate_run_id
from aicoding_agent.tools.paths import resolve_workspace_path


class CoordinatorError(RuntimeError):
    pass


class CandidateStatus(StrEnum):
    INTEGRATED = "integrated"
    BLOCKED = "blocked"


class WorkerLifecycle(StrEnum):
    IDLE = "idle"
    WORK = "work"
    STOPPING = "stopping"
    STOPPED = "stopped"


class CoordinatorWorkerState(StrictModel):
    owner: str = Field(min_length=1, max_length=128)
    lifecycle: WorkerLifecycle = WorkerLifecycle.IDLE
    assignments_completed: int = Field(default=0, ge=0)
    last_task_id: str | None = Field(default=None, max_length=64)


class CoordinatorMessage(StrictModel):
    message_id: str = Field(min_length=32, max_length=32)
    sequence: int = Field(ge=1)
    coordinator_id: str = Field(min_length=1, max_length=128)
    owner: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=64)
    event_type: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CoordinatorMailboxStore:
    def __init__(self, root: Path, *, max_payload_chars: int = 20_000) -> None:
        self.root = root.resolve()
        self.max_payload_chars = max_payload_chars
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def append(
        self,
        *,
        coordinator_id: str,
        owner: str,
        task_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> CoordinatorMessage:
        coordinator_id = validate_run_id(coordinator_id)
        owner = validate_run_id(owner)
        payload = payload or {}
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        if len(encoded) > self.max_payload_chars:
            raise CoordinatorError("Coordinator mailbox payload exceeds its bounded size")
        lock = self._locks.setdefault((coordinator_id, owner), asyncio.Lock())
        path = self._path(coordinator_id, owner)
        async with lock:
            existing = await asyncio.to_thread(self._load_sync, path)
            message = CoordinatorMessage(
                message_id=uuid4().hex,
                sequence=len(existing) + 1,
                coordinator_id=coordinator_id,
                owner=owner,
                task_id=task_id,
                event_type=event_type,
                payload=payload,
            )
            await asyncio.to_thread(self._append_sync, path, message)
            return message

    async def load(self, coordinator_id: str, owner: str) -> list[CoordinatorMessage]:
        coordinator_id = validate_run_id(coordinator_id)
        owner = validate_run_id(owner)
        lock = self._locks.setdefault((coordinator_id, owner), asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(self._load_sync, self._path(coordinator_id, owner))

    def _path(self, coordinator_id: str, owner: str) -> Path:
        return self.root / coordinator_id / "mailboxes" / f"{owner}.jsonl"

    @staticmethod
    def _load_sync(path: Path) -> list[CoordinatorMessage]:
        if not path.is_file():
            return []
        messages: list[CoordinatorMessage] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            message = CoordinatorMessage.model_validate_json(line)
            if message.sequence != len(messages) + 1:
                raise CoordinatorError(
                    f"Invalid coordinator mailbox sequence at line {line_number}"
                )
            messages.append(message)
        return messages

    @staticmethod
    def _append_sync(path: Path, message: CoordinatorMessage) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(message.model_dump_json())
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


class VerificationEvidence(StrictModel):
    verification_id: str
    exit_code: int
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_preview: str = Field(max_length=1000)


class CoordinatorCandidate(StrictModel):
    task_id: str
    owner: str
    run_id: str
    status: CandidateStatus
    workspace_lease: WorkspaceLease | None = None
    agent_result: AgentResult | None = None
    change_snapshot: ChangeSnapshot | None = None
    verification: list[VerificationEvidence] = Field(default_factory=list)
    integrated_revision: str | None = None
    failure_reason: str | None = Field(default=None, max_length=2000)


class CoordinatorResult(StrictModel):
    coordinator_id: str
    integration_lease: WorkspaceLease
    integration_revision: str
    candidates: list[CoordinatorCandidate]
    completed_tasks: list[str]
    blocked_tasks: list[str]
    pending_tasks: list[str] = Field(default_factory=list)
    workers: list[CoordinatorWorkerState] = Field(default_factory=list)
    source_unchanged: bool = True


class CoordinatorAgent(Protocol):
    async def run(
        self,
        contract: TaskContract,
        workspace: Path,
        *,
        run_id: str | None = None,
    ) -> AgentResult: ...


class ChangeInspector(Protocol):
    async def snapshot(self, workspace: Path) -> ChangeSnapshot: ...


class CoordinatorWorkerRuntime:
    """Keep one Agent instance and bounded cross-assignment context per worker."""

    def __init__(
        self,
        owner: str,
        agent: CoordinatorAgent,
        *,
        responsibility: str = "",
        max_history_items: int = 12,
    ) -> None:
        self.owner = owner
        self.agent = agent
        self.max_history_items = max_history_items
        self.history: list[dict[str, str]] = []
        if responsibility:
            self.history.append(
                {
                    "role": "user",
                    "content": (
                        f"<team_role name={owner!r}>"
                        f"{responsibility}</team_role>"
                    ),
                }
            )
        self.state = CoordinatorWorkerState(owner=owner)

    async def run_assignment(
        self,
        contract: TaskContract,
        workspace: Path,
        *,
        run_id: str,
    ) -> AgentResult:
        self.state.lifecycle = WorkerLifecycle.WORK
        self.state.last_task_id = contract.task_id.removeprefix("coord:")
        optional: dict[str, Any] = {"run_id": run_id}
        signature = inspect.signature(self.agent.run)
        accepts_keywords = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        if accepts_keywords or "conversation_history" in signature.parameters:
            optional["conversation_history"] = list(self.history)
        result = await self.agent.run(contract, workspace, **optional)
        assistant_text = result.final_text.strip()
        if not assistant_text and result.outcome is not None:
            assistant_text = result.outcome.summary.strip()
        if not assistant_text:
            assistant_text = result.reason or f"Assignment ended with {result.status}"
        self.history.extend(
            [
                {"role": "user", "content": contract.objective},
                {"role": "assistant", "content": assistant_text[:20_000]},
            ]
        )
        self.history = self.history[-self.max_history_items :]
        self.state.assignments_completed += 1
        self.state.lifecycle = WorkerLifecycle.IDLE
        return result

    def receive_direct_message(self, sender: str, content: str) -> None:
        self.history.append(
            {
                "role": "user",
                "content": f"<team_message sender={sender!r}>{content}</team_message>",
            }
        )
        self.history = self.history[-self.max_history_items :]


class CoordinatorMergeGate:
    def __init__(self, *, timeout_seconds: float = 300.0) -> None:
        self.timeout_seconds = timeout_seconds

    async def validate(
        self,
        *,
        workspace: Path,
        contract: TaskContract,
        result: AgentResult,
        inspector: ChangeInspector,
        expected_base_revision: str,
    ) -> tuple[ChangeSnapshot, list[VerificationEvidence]]:
        if result.status != AgentStatus.COMPLETED or result.outcome is None:
            raise CoordinatorError("Worker Agent did not produce a completed coding outcome")
        before = await inspector.snapshot(workspace)
        if before.base_revision != expected_base_revision:
            raise CoordinatorError("Candidate base revision changed during Agent execution")
        if not before.changed_files:
            raise CoordinatorError("Candidate contains no code changes")
        if set(result.outcome.changed_files) != set(before.changed_files):
            raise CoordinatorError("Agent changed_files does not match the Git candidate snapshot")
        if not contract.verification_commands:
            raise CoordinatorError(
                "Coordinator candidates require at least one verification command"
            )
        evidence: list[VerificationEvidence] = []
        for command in contract.verification_commands:
            evidence.append(await self._verify(workspace, contract, command))
        after = await inspector.snapshot(workspace)
        if after != before:
            raise CoordinatorError("Merge-gate verification changed the candidate workspace")
        return before, evidence

    async def _verify(
        self,
        workspace: Path,
        contract: TaskContract,
        command: VerificationCommand,
    ) -> VerificationEvidence:
        cwd = resolve_workspace_path(
            workspace,
            command.cwd,
            allowed_prefixes=contract.allowed_path_prefixes,
        )
        if not cwd.is_dir():
            raise CoordinatorError(f"Verification cwd does not exist: {command.cwd}")
        try:
            process = await asyncio.create_subprocess_exec(
                command.program,
                *command.args,
                cwd=str(cwd),
                env=_safe_process_environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise CoordinatorError(
                f"Could not start verification executable: {command.program}"
            ) from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                self.timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise CoordinatorError(f"Verification {command.id} timed out") from exc
        output = stdout + b"\n" + stderr
        if process.returncode != 0:
            raise CoordinatorError(
                f"Verification {command.id} failed with exit code {process.returncode}"
            )
        return VerificationEvidence(
            verification_id=command.id,
            exit_code=process.returncode,
            output_sha256=hashlib.sha256(output).hexdigest(),
            output_preview=f"<captured {len(output)} bytes; content redacted>",
        )


class MultiAgentCoordinator:
    def __init__(
        self,
        *,
        workspace_manager: WorkspaceManager,
        inspector: ChangeInspector,
        integrator: GitCandidateIntegrator,
        mailbox: CoordinatorMailboxStore,
        team_bus: TeamMessageBus | None = None,
        launch_manager: TeamLaunchManager | None = None,
        plan_approvals: TeamPlanApprovalManager | None = None,
        merge_gate: CoordinatorMergeGate | None = None,
        max_workers: int = 3,
        plan_approval_timeout_seconds: float = 300.0,
    ) -> None:
        if not 1 <= max_workers <= 8:
            raise ValueError("Coordinator max_workers must be between 1 and 8")
        if plan_approval_timeout_seconds <= 0:
            raise ValueError("Plan approval timeout must be positive")
        self.workspace_manager = workspace_manager
        self.inspector = inspector
        self.integrator = integrator
        self.mailbox = mailbox
        self.team_bus = team_bus
        self.launch_manager = launch_manager
        self.plan_approvals = plan_approvals
        self.merge_gate = merge_gate or CoordinatorMergeGate()
        self.max_workers = max_workers
        self.plan_approval_timeout_seconds = plan_approval_timeout_seconds

    async def run(
        self,
        *,
        coordinator_id: str,
        repository: Path,
        task_graph: TaskGraphManager,
        task_contracts: dict[str, TaskContract],
        agent_factory: Callable[[str], CoordinatorAgent],
        keep_alive: bool = False,
        proposal_id: str | None = None,
    ) -> CoordinatorResult:
        coordinator_id = validate_run_id(coordinator_id)
        repository = repository.resolve()
        await task_graph.initialize()
        graph = task_graph.current
        if graph is None or graph.state != TaskGraphState.ACTIVE:
            raise CoordinatorError("Coordinator requires an active Task DAG")
        graph_ids = {task.task_id for task in graph.tasks}
        if set(task_contracts) != graph_ids:
            raise CoordinatorError("Task contract IDs must exactly match the active Task DAG")
        if any(task.status != TaskNodeStatus.PENDING for task in graph.tasks):
            raise CoordinatorError("Coordinator requires a fresh all-pending Task DAG")
        if keep_alive and self.team_bus is None:
            raise CoordinatorError("Persistent Coordinator workers require a TeamMessageBus")
        approved_roles: list[tuple[str, str]] = []
        approved_proposal_id: str | None = None
        if self.launch_manager is not None:
            if proposal_id is None:
                raise CoordinatorError("Team startup requires an approved proposal ID")
            try:
                proposal = await self.launch_manager.require_approved(
                    coordinator_id,
                    proposal_id,
                )
            except TeamProtocolError as exc:
                raise CoordinatorError(str(exc)) from exc
            if len(proposal.roles) != self.max_workers:
                raise CoordinatorError(
                    "Approved team role count must match Coordinator max_workers"
                )
            approved_roles = [
                (role.name, role.responsibility)
                for role in proposal.roles
            ]
            approved_proposal_id = proposal_id
        source_before = await self.inspector.snapshot(repository)
        if source_before.base_revision is None:
            raise CoordinatorError("Could not freeze the source Git revision")
        if source_before.changed_files:
            raise CoordinatorError("Coordinator source repository must have a clean worktree")
        integration_contract = self._integration_contract(
            coordinator_id,
            next(iter(task_contracts.values())),
            source_before.base_revision,
        )
        integration_lease = await self.workspace_manager.prepare(
            repository,
            integration_contract,
            run_id=_safe_run_id(coordinator_id, "integration", 1),
        )
        integration_workspace = Path(integration_lease.path)
        integration_revision = integration_lease.base_revision
        if self.launch_manager is not None and approved_proposal_id is not None:
            await self.launch_manager.mark_started(coordinator_id, approved_proposal_id)
        integration_lock = asyncio.Lock()
        path_revisions: dict[str, str] = {}
        candidates: dict[str, CoordinatorCandidate] = {}
        candidates_lock = asyncio.Lock()
        changed = asyncio.Event()
        workers: dict[str, CoordinatorWorkerRuntime] = {}

        async def store_candidate(candidate: CoordinatorCandidate) -> None:
            async with candidates_lock:
                candidates[candidate.task_id] = candidate

        async def worker(index: int) -> None:
            nonlocal integration_revision
            if approved_roles:
                owner, responsibility = approved_roles[index - 1]
            else:
                owner, responsibility = f"worker-{index}", ""
            runtime = CoordinatorWorkerRuntime(
                owner,
                agent_factory(owner),
                responsibility=responsibility,
            )
            workers[owner] = runtime
            team_cursor = 0
            await self.mailbox.append(
                coordinator_id=coordinator_id,
                owner=owner,
                task_id="worker-lifecycle",
                event_type="worker.idle",
                payload={"lifecycle": WorkerLifecycle.IDLE},
            )
            if self.team_bus is not None:
                await self.team_bus.send(
                    team_id=coordinator_id,
                    sender=owner,
                    recipient="lead",
                    message_type=TeamMessageType.IDLE,
                    content="Worker is ready for an assignment.",
                )
            while True:
                shutdown_request_id: str | None = None
                if self.team_bus is not None:
                    incoming = await self.team_bus.read(
                        coordinator_id,
                        owner,
                        after_sequence=team_cursor,
                    )
                    for message in incoming:
                        team_cursor = max(team_cursor, message.sequence)
                        if message.message_type == TeamMessageType.DIRECT:
                            runtime.receive_direct_message(message.sender, message.content)
                        elif message.message_type == TeamMessageType.SHUTDOWN_REQUEST:
                            shutdown_request_id = message.request_id
                if shutdown_request_id is not None:
                    await self._stop_worker(
                        coordinator_id=coordinator_id,
                        runtime=runtime,
                        request_id=shutdown_request_id,
                    )
                    return
                claim = await task_graph.claim_next(owner)
                if claim is None:
                    await task_graph.block_unreachable()
                    current = task_graph.current
                    assert current is not None
                    if all(
                        task.status in {TaskNodeStatus.COMPLETED, TaskNodeStatus.BLOCKED}
                        for task in current.tasks
                    ):
                        if not keep_alive:
                            await self._stop_worker(
                                coordinator_id=coordinator_id,
                                runtime=runtime,
                            )
                            return
                    runtime.state.lifecycle = WorkerLifecycle.IDLE
                    changed.clear()
                    try:
                        await asyncio.wait_for(changed.wait(), timeout=0.1)
                    except TimeoutError:
                        pass
                    continue
                runtime.state.lifecycle = WorkerLifecycle.WORK
                runtime.state.last_task_id = claim.task_id
                await self.mailbox.append(
                    coordinator_id=coordinator_id,
                    owner=owner,
                    task_id=claim.task_id,
                    event_type="task.claimed",
                    payload={
                        "attempt": claim.attempts,
                        "lifecycle": WorkerLifecycle.WORK,
                    },
                )
                lease: WorkspaceLease | None = None
                result: AgentResult | None = None
                snapshot: ChangeSnapshot | None = None
                verification: list[VerificationEvidence] = []
                integrated = False
                run_id = _safe_run_id(coordinator_id, claim.task_id, claim.attempts)
                try:
                    async with integration_lock:
                        task_base = integration_revision
                    contract = task_contracts[claim.task_id].model_copy(
                        update={
                            "base_revision": task_base,
                            "target_environment": TargetEnvironment.LOCAL,
                        }
                    )
                    if contract.requires_plan_approval:
                        if self.plan_approvals is None:
                            raise CoordinatorError(
                                "Task requires plan approval but no approval manager is configured"
                            )
                        approval = await self.plan_approvals.request(
                            team_id=coordinator_id,
                            worker=owner,
                            task_id=claim.task_id,
                            work_version=claim.attempts,
                            plan=contract.execution_plan or "",
                        )
                        approval = await self.plan_approvals.wait_for_decision(
                            team_id=coordinator_id,
                            worker=owner,
                            request_id=approval.request_id,
                            timeout_seconds=self.plan_approval_timeout_seconds,
                        )
                        if approval.status != PlanApprovalStatus.APPROVED:
                            raise CoordinatorError(
                                "Execution plan was rejected: "
                                + (approval.feedback or "no feedback supplied")
                            )
                        await self.plan_approvals.require_approved(
                            team_id=coordinator_id,
                            worker=owner,
                            task_id=claim.task_id,
                            work_version=claim.attempts,
                            plan_sha256=approval.plan_sha256,
                        )
                    lease = await self.workspace_manager.prepare(
                        repository,
                        contract,
                        run_id=run_id,
                    )
                    await task_graph.attach_worktree(claim.task_id, owner, lease.path)
                    await self.mailbox.append(
                        coordinator_id=coordinator_id,
                        owner=owner,
                        task_id=claim.task_id,
                        event_type="worktree.prepared",
                        payload={"lease_id": lease.lease_id, "base_revision": lease.base_revision},
                    )
                    result = await runtime.run_assignment(
                        contract,
                        Path(lease.path),
                        run_id=run_id,
                    )
                    snapshot, verification = await self.merge_gate.validate(
                        workspace=Path(lease.path),
                        contract=contract,
                        result=result,
                        inspector=self.inspector,
                        expected_base_revision=lease.base_revision,
                    )
                    async with integration_lock:
                        conflicts = []
                        for path in snapshot.changed_files:
                            previous_revision = path_revisions.get(path)
                            includes_previous = previous_revision is None or (
                                await self.integrator.is_ancestor(
                                    repository,
                                    previous_revision,
                                    lease.base_revision,
                                )
                            )
                            if not includes_previous:
                                conflicts.append(path)
                        if conflicts:
                            raise CoordinatorError(
                                "Candidate overlaps a parallel change: " + ", ".join(conflicts)
                            )
                        integration_revision = await self.integrator.integrate(
                            Path(lease.path),
                            integration_workspace,
                            task_id=claim.task_id,
                            expected_candidate_base=lease.base_revision,
                        )
                        integrated = True
                        for path in snapshot.changed_files:
                            path_revisions[path] = integration_revision
                    evidence = [
                        f"change:{snapshot.change_hash}",
                        *(
                            f"verification:{item.verification_id}:{item.output_sha256}"
                            for item in verification
                        ),
                    ]
                    await task_graph.complete_claim(
                        claim.task_id,
                        owner,
                        evidence=evidence,
                        worktree=lease.path,
                    )
                    candidate = CoordinatorCandidate(
                        task_id=claim.task_id,
                        owner=owner,
                        run_id=run_id,
                        status=CandidateStatus.INTEGRATED,
                        workspace_lease=lease,
                        agent_result=result,
                        change_snapshot=snapshot,
                        verification=verification,
                        integrated_revision=integration_revision,
                    )
                    await store_candidate(candidate)
                    await self.mailbox.append(
                        coordinator_id=coordinator_id,
                        owner=owner,
                        task_id=claim.task_id,
                        event_type="candidate.integrated",
                        payload={
                            "change_hash": snapshot.change_hash,
                            "integration_revision": integration_revision,
                        },
                    )
                    if self.team_bus is not None:
                        await self.team_bus.send(
                            team_id=coordinator_id,
                            sender=owner,
                            recipient="lead",
                            message_type=TeamMessageType.RESULT,
                            content=result.final_text or result.outcome.summary,
                            task_id=claim.task_id,
                            metadata={"integration_revision": integration_revision},
                        )
                except Exception as exc:
                    reason = _safe_failure(exc)
                    if integrated:
                        raise CoordinatorError(
                            "Candidate entered the integration worktree but its persistent "
                            f"completion record failed: {reason}"
                        ) from exc
                    await task_graph.block_claim(
                        claim.task_id,
                        owner,
                        reason=reason,
                        worktree=lease.path if lease is not None else None,
                    )
                    await store_candidate(
                        CoordinatorCandidate(
                            task_id=claim.task_id,
                            owner=owner,
                            run_id=run_id,
                            status=CandidateStatus.BLOCKED,
                            workspace_lease=lease,
                            agent_result=result,
                            change_snapshot=snapshot,
                            verification=verification,
                            failure_reason=reason,
                        )
                    )
                    await self.mailbox.append(
                        coordinator_id=coordinator_id,
                        owner=owner,
                        task_id=claim.task_id,
                        event_type="candidate.blocked",
                        payload={"reason": reason},
                    )
                finally:
                    if runtime.state.lifecycle == WorkerLifecycle.WORK:
                        runtime.state.lifecycle = WorkerLifecycle.IDLE
                    await self.mailbox.append(
                        coordinator_id=coordinator_id,
                        owner=owner,
                        task_id=claim.task_id,
                        event_type="worker.idle",
                        payload={
                            "lifecycle": WorkerLifecycle.IDLE,
                            "assignments_completed": runtime.state.assignments_completed,
                        },
                    )
                    if self.team_bus is not None:
                        await self.team_bus.send(
                            team_id=coordinator_id,
                            sender=owner,
                            recipient="lead",
                            message_type=TeamMessageType.IDLE,
                            content="Worker is waiting for another assignment.",
                            task_id=claim.task_id,
                        )
                    changed.set()

        await asyncio.gather(*(worker(index + 1) for index in range(self.max_workers)))
        await task_graph.block_unreachable()
        source_after = await self.inspector.snapshot(repository)
        if source_after != source_before:
            raise CoordinatorError("Source repository changed during coordination")
        final_graph = task_graph.current
        assert final_graph is not None
        ordered_candidates = [
            candidates[task.task_id] for task in final_graph.tasks if task.task_id in candidates
        ]
        return CoordinatorResult(
            coordinator_id=coordinator_id,
            integration_lease=integration_lease,
            integration_revision=integration_revision,
            candidates=ordered_candidates,
            completed_tasks=[
                task.task_id
                for task in final_graph.tasks
                if task.status == TaskNodeStatus.COMPLETED
            ],
            blocked_tasks=[
                task.task_id for task in final_graph.tasks if task.status == TaskNodeStatus.BLOCKED
            ],
            pending_tasks=[
                task.task_id
                for task in final_graph.tasks
                if task.status in {TaskNodeStatus.PENDING, TaskNodeStatus.IN_PROGRESS}
            ],
            workers=[workers[owner].state for owner in sorted(workers)],
        )

    async def _stop_worker(
        self,
        *,
        coordinator_id: str,
        runtime: CoordinatorWorkerRuntime,
        request_id: str | None = None,
    ) -> None:
        runtime.state.lifecycle = WorkerLifecycle.STOPPING
        task_id = runtime.state.last_task_id or "worker-lifecycle"
        await self.mailbox.append(
            coordinator_id=coordinator_id,
            owner=runtime.owner,
            task_id=task_id,
            event_type="worker.stopping",
            payload={"lifecycle": WorkerLifecycle.STOPPING},
        )
        if request_id is not None and self.team_bus is not None:
            await self.team_bus.send(
                team_id=coordinator_id,
                sender=runtime.owner,
                recipient="lead",
                message_type=TeamMessageType.SHUTDOWN_RESPONSE,
                content="Worker accepted the graceful shutdown request.",
                request_id=request_id,
                task_id=runtime.state.last_task_id,
            )
        runtime.state.lifecycle = WorkerLifecycle.STOPPED
        await self.mailbox.append(
            coordinator_id=coordinator_id,
            owner=runtime.owner,
            task_id=task_id,
            event_type="worker.stopped",
            payload={"lifecycle": WorkerLifecycle.STOPPED},
        )

    @staticmethod
    def _integration_contract(
        coordinator_id: str,
        template: TaskContract,
        revision: str,
    ) -> TaskContract:
        task_id = f"coord:{coordinator_id}"[:128]
        return template.model_copy(
            update={
                "task_id": task_id,
                "objective": "Compose validated multi-Agent candidates locally",
                "acceptance_criteria": [
                    "Only merge-gate validated candidates enter the integration worktree"
                ],
                "base_revision": revision,
                "target_environment": TargetEnvironment.LOCAL,
            }
        )


def _safe_run_id(coordinator_id: str, task_id: str, attempt: int) -> str:
    raw = f"coord-{coordinator_id}-{task_id}-{attempt}"
    safe = "".join(
        character if character.isalnum() or character in "_.-" else "-" for character in raw
    )
    if len(safe) <= 120:
        return safe
    digest = hashlib.sha256(safe.encode()).hexdigest()[:12]
    return f"{safe[:107]}-{digest}"


def _safe_failure(exc: Exception) -> str:
    message = " ".join(str(exc).split())[:1500]
    return f"{type(exc).__name__}: {message or 'operation failed'}"


def _safe_process_environment() -> dict[str, str]:
    allowed = {
        "COMSPEC",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}
