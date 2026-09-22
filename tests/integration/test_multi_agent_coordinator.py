import asyncio
from pathlib import Path

import pytest

from aicoding_agent.adapters.git import (
    GitCandidateIntegrator,
    GitChangeInspector,
    LocalGitWorktreeManager,
)
from aicoding_agent.agent.coordinator import (
    CoordinatorMailboxStore,
    MultiAgentCoordinator,
)
from aicoding_agent.agent.task_graph import TaskGraphManager, TaskGraphStore, TaskNode
from aicoding_agent.agent.team import (
    TeamLaunchManager,
    TeamLaunchStatus,
    TeamMessageBus,
    TeamMessageType,
    TeamPlanApprovalManager,
    TeamRole,
)
from aicoding_agent.domain.models import (
    AgentResult,
    AgentStatus,
    CodingOutcome,
    TaskContract,
    VerificationCommand,
)


async def git(workspace: Path, *args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode(errors="replace")
    return stdout.decode(errors="replace").strip()


async def create_repository(path: Path) -> str:
    path.mkdir()
    await git(path, "init", "-b", "main")
    await git(path, "config", "user.name", "Test User")
    await git(path, "config", "user.email", "test@example.invalid")
    (path / "a.txt").write_text("a0\n", encoding="utf-8")
    (path / "b.txt").write_text("b0\n", encoding="utf-8")
    (path / "verify.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    await git(path, "add", ".")
    await git(path, "commit", "-m", "baseline")
    return await git(path, "rev-parse", "HEAD")


def task_contract(task_id: str) -> TaskContract:
    return TaskContract(
        task_id=f"coord:{task_id}",
        objective=f"Implement {task_id}",
        acceptance_criteria=[f"{task_id} is complete"],
        verification_commands=[
            VerificationCommand(id="tests", program="python", args=["verify.py"])
        ],
    )


class EditingAgent:
    def __init__(
        self,
        edits: dict[str, tuple[str, str, str]],
        active: dict[str, int],
        lock: asyncio.Lock,
        *,
        parallel_tasks: set[str] | None = None,
        parallel_ready: asyncio.Event | None = None,
    ) -> None:
        self.edits = edits
        self.active = active
        self.lock = lock
        self.parallel_tasks = parallel_tasks or set()
        self.parallel_ready = parallel_ready

    async def run(
        self,
        contract: TaskContract,
        workspace: Path,
        *,
        run_id: str | None = None,
    ) -> AgentResult:
        graph_task_id = contract.task_id.removeprefix("coord:")
        async with self.lock:
            self.active["current"] += 1
            self.active["maximum"] = max(
                self.active["maximum"],
                self.active["current"],
            )
            if (
                graph_task_id in self.parallel_tasks
                and self.active["current"] >= len(self.parallel_tasks)
                and self.parallel_ready is not None
            ):
                self.parallel_ready.set()
        if graph_task_id in self.parallel_tasks and self.parallel_ready is not None:
            await asyncio.wait_for(self.parallel_ready.wait(), timeout=10)
        else:
            await asyncio.sleep(0.05)
        path, expected, replacement = self.edits[graph_task_id]
        target = workspace / path
        assert target.read_text(encoding="utf-8") == expected
        target.write_text(replacement, encoding="utf-8")
        async with self.lock:
            self.active["current"] -= 1
        return AgentResult(
            status=AgentStatus.COMPLETED,
            run_id=run_id or "run",
            final_text=f"Completed {graph_task_id}",
            outcome=CodingOutcome(
                summary=f"Completed {graph_task_id}",
                changed_files=[path],
            ),
        )


@pytest.mark.asyncio
async def test_coordinator_runs_parallel_agents_then_unlocks_dependent_task(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    source_revision = await create_repository(repository)
    graph = TaskGraphManager(
        store=TaskGraphStore(tmp_path / "runtime" / "graphs"),
        scope_id="coord-success",
    )
    await graph.replace(
        [
            TaskNode(task_id="first", title="Edit a"),
            TaskNode(task_id="second", title="Edit b"),
            TaskNode(task_id="dependent", title="Refine a", blocked_by=["first"]),
        ]
    )
    active = {"current": 0, "maximum": 0}
    agent_lock = asyncio.Lock()
    parallel_ready = asyncio.Event()
    edits = {
        "first": ("a.txt", "a0\n", "a1\n"),
        "second": ("b.txt", "b0\n", "b1\n"),
        "dependent": ("a.txt", "a1\n", "a2\n"),
    }
    mailbox = CoordinatorMailboxStore(tmp_path / "runtime" / "coordinator")
    team_bus = TeamMessageBus(tmp_path / "runtime" / "teams")
    launch_manager = TeamLaunchManager(tmp_path / "runtime" / "launches")
    proposal = await launch_manager.propose(
        team_id="coord-success",
        goal="Implement the three-node graph",
        roles=[
            TeamRole(name="worker-1", responsibility="Implement ready tasks"),
            TeamRole(name="worker-2", responsibility="Implement ready tasks"),
        ],
        idempotency_key="coord-success-proposal",
    )
    await launch_manager.approve("coord-success", proposal.proposal_id)
    coordinator = MultiAgentCoordinator(
        workspace_manager=LocalGitWorktreeManager(tmp_path / "worktrees"),
        inspector=GitChangeInspector(),
        integrator=GitCandidateIntegrator(safe_hooks_dir=tmp_path / "safe-hooks"),
        mailbox=mailbox,
        team_bus=team_bus,
        launch_manager=launch_manager,
        max_workers=2,
    )

    result = await coordinator.run(
        coordinator_id="coord-success",
        repository=repository,
        task_graph=graph,
        task_contracts={task_id: task_contract(task_id) for task_id in edits},
        agent_factory=lambda _owner: EditingAgent(
            edits,
            active,
            agent_lock,
            parallel_tasks={"first", "second"},
            parallel_ready=parallel_ready,
        ),
        proposal_id=proposal.proposal_id,
    )

    integration = Path(result.integration_lease.path)
    assert result.completed_tasks == ["first", "second", "dependent"]
    assert result.blocked_tasks == []
    assert active["maximum"] == 2
    assert (integration / "a.txt").read_text(encoding="utf-8") == "a2\n"
    assert (integration / "b.txt").read_text(encoding="utf-8") == "b1\n"
    assert (repository / "a.txt").read_text(encoding="utf-8") == "a0\n"
    assert await git(repository, "rev-parse", "HEAD") == source_revision
    candidate_worktrees = {
        item.workspace_lease.path for item in result.candidates if item.workspace_lease
    }
    assert len(candidate_worktrees) == 3
    assert all(item.status == "integrated" for item in result.candidates)
    assert len(result.workers) == 2
    assert all(item.lifecycle == "stopped" for item in result.workers)
    assert sum(item.assignments_completed for item in result.workers) == 3
    messages = []
    for owner in {item.owner for item in result.candidates}:
        messages.extend(await mailbox.load("coord-success", owner))
    assert sum(item.event_type == "candidate.integrated" for item in messages) == 3
    assert sum(item.event_type == "worker.stopped" for item in messages) == 2
    lead_messages = await team_bus.read("coord-success", "lead")
    assert sum(item.message_type == TeamMessageType.RESULT for item in lead_messages) == 3
    assert sum(item.message_type == TeamMessageType.IDLE for item in lead_messages) == 5
    launch = await launch_manager.current("coord-success")
    assert launch is not None and launch.status == TeamLaunchStatus.STARTED


@pytest.mark.asyncio
async def test_coordinator_waits_for_version_bound_plan_approval(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    await create_repository(repository)
    graph = TaskGraphManager(scope_id="coord-plan")
    await graph.replace([TaskNode(task_id="first", title="Edit a")])
    active = {"current": 0, "maximum": 0}
    agent_lock = asyncio.Lock()
    edits = {"first": ("a.txt", "a0\n", "a1\n")}
    bus = TeamMessageBus(tmp_path / "runtime" / "teams")
    approvals = TeamPlanApprovalManager(
        tmp_path / "runtime" / "approvals",
        message_bus=bus,
        poll_seconds=0.01,
    )
    coordinator = MultiAgentCoordinator(
        workspace_manager=LocalGitWorktreeManager(tmp_path / "worktrees"),
        inspector=GitChangeInspector(),
        integrator=GitCandidateIntegrator(safe_hooks_dir=tmp_path / "safe-hooks"),
        mailbox=CoordinatorMailboxStore(tmp_path / "runtime" / "coordinator"),
        team_bus=bus,
        plan_approvals=approvals,
        max_workers=1,
        plan_approval_timeout_seconds=2,
    )
    contract = task_contract("first").model_copy(
        update={
            "requires_plan_approval": True,
            "execution_plan": "Edit a.txt and run the contract verification.",
        }
    )
    running = asyncio.create_task(
        coordinator.run(
            coordinator_id="coord-plan",
            repository=repository,
            task_graph=graph,
            task_contracts={"first": contract},
            agent_factory=lambda _owner: EditingAgent(edits, active, agent_lock),
        )
    )
    request = None
    for _ in range(100):
        request = await approvals.current("coord-plan", "worker-1")
        if request is not None:
            break
        await asyncio.sleep(0.01)
    assert request is not None
    assert active["current"] == 0

    await approvals.respond(
        team_id="coord-plan",
        worker="worker-1",
        request_id=request.request_id,
        approve=True,
    )
    result = await running
    assert result.completed_tasks == ["first"]
    assert active["maximum"] == 1


@pytest.mark.asyncio
async def test_persistent_worker_idles_until_graceful_shutdown(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    await create_repository(repository)
    graph = TaskGraphManager(scope_id="coord-persistent")
    await graph.replace([TaskNode(task_id="first", title="Edit a")])
    active = {"current": 0, "maximum": 0}
    agent_lock = asyncio.Lock()
    bus = TeamMessageBus(tmp_path / "runtime" / "teams", poll_seconds=0.01)
    coordinator = MultiAgentCoordinator(
        workspace_manager=LocalGitWorktreeManager(tmp_path / "worktrees"),
        inspector=GitChangeInspector(),
        integrator=GitCandidateIntegrator(safe_hooks_dir=tmp_path / "safe-hooks"),
        mailbox=CoordinatorMailboxStore(tmp_path / "runtime" / "coordinator"),
        team_bus=bus,
        max_workers=1,
    )
    running = asyncio.create_task(
        coordinator.run(
            coordinator_id="coord-persistent",
            repository=repository,
            task_graph=graph,
            task_contracts={"first": task_contract("first")},
            agent_factory=lambda _owner: EditingAgent(
                {"first": ("a.txt", "a0\n", "a1\n")},
                active,
                agent_lock,
            ),
            keep_alive=True,
        )
    )
    cursor = 0
    while True:
        messages = await bus.wait_for_messages(
            "coord-persistent",
            "lead",
            after_sequence=cursor,
            timeout_seconds=2,
        )
        assert messages
        cursor = max(item.sequence for item in messages)
        if any(item.message_type == TeamMessageType.RESULT for item in messages):
            break
    assert not running.done()

    request = await bus.send(
        team_id="coord-persistent",
        sender="lead",
        recipient="worker-1",
        message_type=TeamMessageType.SHUTDOWN_REQUEST,
        content="Stop after the completed assignment",
        request_id="shutdown-1",
    )
    result = await asyncio.wait_for(running, timeout=2)
    assert result.completed_tasks == ["first"]
    assert result.workers[0].lifecycle == "stopped"
    replies = await bus.read("coord-persistent", "lead", after_sequence=cursor)
    assert any(
        item.message_type == TeamMessageType.SHUTDOWN_RESPONSE
        and item.request_id == request.request_id
        for item in replies
    )


@pytest.mark.asyncio
async def test_coordinator_blocks_parallel_overlap_and_its_dependents(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    await create_repository(repository)
    graph = TaskGraphManager(scope_id="coord-conflict")
    await graph.replace(
        [
            TaskNode(task_id="left", title="Left edit"),
            TaskNode(task_id="right", title="Right edit"),
            TaskNode(
                task_id="downstream",
                title="After both",
                blocked_by=["left", "right"],
            ),
        ]
    )
    active = {"current": 0, "maximum": 0}
    agent_lock = asyncio.Lock()
    edits = {
        "left": ("a.txt", "a0\n", "left\n"),
        "right": ("a.txt", "a0\n", "right\n"),
        "downstream": ("b.txt", "b0\n", "never\n"),
    }
    coordinator = MultiAgentCoordinator(
        workspace_manager=LocalGitWorktreeManager(tmp_path / "worktrees"),
        inspector=GitChangeInspector(),
        integrator=GitCandidateIntegrator(safe_hooks_dir=tmp_path / "safe-hooks"),
        mailbox=CoordinatorMailboxStore(tmp_path / "runtime"),
        max_workers=2,
    )

    result = await coordinator.run(
        coordinator_id="coord-conflict",
        repository=repository,
        task_graph=graph,
        task_contracts={task_id: task_contract(task_id) for task_id in edits},
        agent_factory=lambda _owner: EditingAgent(edits, active, agent_lock),
    )

    assert len(result.completed_tasks) == 1
    assert len(result.blocked_tasks) == 2
    assert "downstream" in result.blocked_tasks
    blocked_candidates = [item for item in result.candidates if item.status == "blocked"]
    assert len(blocked_candidates) == 1
    assert "overlaps a parallel change" in (blocked_candidates[0].failure_reason or "")
    assert (repository / "a.txt").read_text(encoding="utf-8") == "a0\n"
