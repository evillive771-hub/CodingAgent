import asyncio
import sys
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
    TddSpec,
    VerificationCommand,
)
from aicoding_agent.regression.budget import RegressionBudget
from aicoding_agent.regression.controller import RegressionController
from aicoding_agent.regression.gate import CommandSuite, RegressionGate, RegressionReport


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


@pytest.mark.asyncio
async def test_coordinator_cannot_silently_ignore_required_tdd_without_controller(tmp_path):
    repository = tmp_path / "repository"
    await create_repository(repository)
    graph = TaskGraphManager(scope_id="tdd-missing-controller")
    await graph.replace([TaskNode(task_id="first", title="Edit a")])
    calls = []

    class Worker:
        async def run(self, contract, workspace, *, run_id):
            calls.append("worker")
            raise AssertionError("Business code must not be opened before RED")

    coordinator = MultiAgentCoordinator(
        workspace_manager=LocalGitWorktreeManager(tmp_path / "worktrees"),
        inspector=GitChangeInspector(),
        integrator=GitCandidateIntegrator(safe_hooks_dir=tmp_path / "hooks"),
        mailbox=CoordinatorMailboxStore(tmp_path / "mailbox"), max_workers=1,
    )
    contract = task_contract("first").model_copy(
        update={"tdd": TddSpec(red_cases={"tests": ["test_requirement"]})}
    )
    result = await coordinator.run(
        coordinator_id="tdd-missing-controller", repository=repository, task_graph=graph,
        task_contracts={"first": contract}, agent_factory=lambda _: Worker(),
    )
    assert calls == []
    assert result.blocked_tasks == ["first"]
    assert "TDD" in result.candidates[0].failure_reason


@pytest.mark.asyncio
@pytest.mark.parametrize("red_mode", ["valid", "already_satisfied", "infra_error"])
async def test_coordinator_real_requirement_red_green_accepts_pair(tmp_path, red_mode):
    repository = tmp_path / "repository"
    initial_revision = await create_repository(repository)
    if red_mode == "already_satisfied":
        (repository / "a.txt").write_text("a1\n", encoding="utf-8")
        await git(repository, "add", ".")
        await git(repository, "commit", "-m", "Already implemented")
        initial_revision = await git(repository, "rev-parse", "HEAD")
    host = tmp_path / "host-tests"
    host.mkdir()
    test_file = host / "test_requirement.py"
    test_file.write_text(
        "import os\nfrom pathlib import Path\n"
        "def test_feature():\n"
        "    root = Path(os.environ['REGRESSION_WORKSPACE'])\n"
        "    assert (root / 'a.txt').read_text() == 'a1\\n'\n", encoding="utf-8",
    )
    if red_mode == "infra_error":
        test_file.write_text(
            "raise RuntimeError('test environment unavailable')\n", encoding="utf-8",
        )
    suite = CommandSuite(
        id="requirement", command=[sys.executable, "-m", "pytest", str(test_file), "-q"],
        cwd=host, test_files=[test_file], reporter="pytest_junit",
        expected_tests=["test_requirement::test_feature"],
    )
    controller = RegressionController(
        tmp_path / "regression", RegressionGate(tmp_path / "evidence", command_suites=[suite]),
    )
    graph = TaskGraphManager(scope_id="actual-tdd")
    await graph.replace([TaskNode(task_id="first", title="Edit a")])
    contract = task_contract("first").model_copy(update={
        "verification_commands": [VerificationCommand(id="requirement", program="python",
                                                       args=["verify.py"])],
        "tdd": TddSpec(red_cases={"requirement": ["test_requirement::test_feature"]}),
    })
    active = {"current": 0, "maximum": 0}
    coordinator = MultiAgentCoordinator(
        workspace_manager=LocalGitWorktreeManager(tmp_path / "worktrees"),
        inspector=GitChangeInspector(),
        integrator=GitCandidateIntegrator(safe_hooks_dir=tmp_path / "hooks"),
        mailbox=CoordinatorMailboxStore(tmp_path / "mailbox"), max_workers=1,
        regression_controller=controller,
    )
    result = await coordinator.run(
        coordinator_id="actual-tdd", repository=repository, task_graph=graph,
        task_contracts={"first": contract}, agent_factory=lambda _: EditingAgent(
            {"first": ("a.txt", "a0\n", "a1\n")}, active, asyncio.Lock(),
        ),
    )
    if red_mode != "valid":
        assert result.blocked_tasks == ["first"]
        assert active["maximum"] == 0
        assert result.integration_revision == initial_revision
        assert (await controller.store.current()).verified is False
        return
    assert result.completed_tasks == ["first"], result.candidates[0].failure_reason
    candidate = result.candidates[0]
    assert candidate.tdd_evidence["red_report"]["status"] == "assertion_failed"
    assert candidate.regression_report["status"] == "passed"
    assert (Path(candidate.tdd_evidence["baseline_path"]) / "a.txt").read_text() == "a0\n"
    assert (Path(result.integration_lease.path) / "a.txt").read_text() == "a1\n"
    assert (repository / "a.txt").read_text() == "a0\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("red_failure", [None, "target passed", "browser unavailable"])
async def test_coordinator_requires_red_before_worker_and_keeps_proof_for_combination(
    tmp_path, red_failure
):
    repository = tmp_path / "repository"
    initial_revision = await create_repository(repository)
    graph = TaskGraphManager(scope_id="tdd-coordinator")
    await graph.replace([TaskNode(task_id="first", title="Edit a")])
    events = []
    proof = {"baseline_path": str(repository), "red": "assertion_failed"}

    class TddController:
        async def budget_for(self, run_id, contract):
            return await RegressionBudget.initialize(tmp_path / "budgets", run_id, contract.budget)

        async def prepare_tdd(self, contract, workspace, *, run_id, **kwargs):
            assert (workspace / "a.txt").read_text(encoding="utf-8") == "a0\n"
            events.append("red")
            if red_failure:
                raise RuntimeError(red_failure)
            return proof

        async def verify_combination(self, **kwargs):
            assert kwargs["tdd_evidence"] is proof
            events.append("green")
            return RegressionReport(run_id=kwargs["run_id"], attempt_id="combination",
                                    registry_version=0, registry_fingerprint="a" * 64,
                                    status="passed")

        async def validate_combination(self, **kwargs):
            return True

        async def record_combination_acceptance(self, *, before_publish, **kwargs):
            assert kwargs["tdd_evidence"] is proof
            events.append("accept")
            await before_publish()

    class Worker:
        async def run(self, contract, workspace, *, run_id, **kwargs):
            assert events == ["red"]
            events.append("code")
            (workspace / "a.txt").write_text("a1\n", encoding="utf-8")
            return AgentResult(status=AgentStatus.COMPLETED, run_id=run_id,
                               outcome=CodingOutcome(summary="Fixed", changed_files=["a.txt"]))

    coordinator = MultiAgentCoordinator(
        workspace_manager=LocalGitWorktreeManager(tmp_path / "worktrees"),
        inspector=GitChangeInspector(),
        integrator=GitCandidateIntegrator(safe_hooks_dir=tmp_path / "hooks"),
        mailbox=CoordinatorMailboxStore(tmp_path / "mailbox"),
        max_workers=1, regression_controller=TddController(),
    )
    result = await coordinator.run(
        coordinator_id="tdd-coordinator", repository=repository, task_graph=graph,
        task_contracts={"first": task_contract("first")}, agent_factory=lambda _: Worker(),
    )
    if red_failure:
        assert events == ["red"]
        assert result.blocked_tasks == ["first"]
        assert result.integration_revision == initial_revision
        assert red_failure in result.candidates[0].failure_reason
    else:
        assert events == ["red", "code", "green", "accept"]
        assert result.completed_tasks == ["first"]
        assert result.candidates[0].tdd_evidence == proof


@pytest.mark.asyncio
async def test_coordinator_requires_nonempty_red_evidence_when_tdd_requested(tmp_path):
    repository = tmp_path / "repository"
    await create_repository(repository)
    graph = TaskGraphManager(scope_id="tdd-empty-proof")
    await graph.replace([TaskNode(task_id="first", title="Edit a")])
    called = []

    class EmptyController:
        async def budget_for(self, run_id, contract):
            return None

        async def prepare_tdd(self, *args, **kwargs):
            return None

    class Worker:
        async def run(self, *args, **kwargs):
            called.append(True)
            raise AssertionError("Missing proof cannot open business writes")

    coordinator = MultiAgentCoordinator(
        workspace_manager=LocalGitWorktreeManager(tmp_path / "worktrees"),
        inspector=GitChangeInspector(),
        integrator=GitCandidateIntegrator(safe_hooks_dir=tmp_path / "hooks"),
        mailbox=CoordinatorMailboxStore(tmp_path / "mailbox"), max_workers=1,
        regression_controller=EmptyController(),
    )
    contract = task_contract("first").model_copy(
        update={"tdd": TddSpec(red_cases={"tests": ["test_requirement"]})}
    )
    result = await coordinator.run(
        coordinator_id="tdd-empty-proof", repository=repository, task_graph=graph,
        task_contracts={"first": contract}, agent_factory=lambda _: Worker(),
    )
    assert called == []
    assert result.blocked_tasks == ["first"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["assertion", "stale", "acceptance_refused"])
async def test_combined_regression_blocks_second_independently_valid_candidate(
    tmp_path, failure_mode
):
    stale_evidence = failure_mode == "stale"
    acceptance_refused = failure_mode == "acceptance_refused"
    repository = tmp_path / "repository"
    initial_revision = await create_repository(repository)
    graph = TaskGraphManager(scope_id="combined-regression")
    await graph.replace([
        TaskNode(task_id="first", title="Edit a"),
        TaskNode(task_id="second", title="Edit b"),
    ])
    observations = []
    accepted = []

    class CombinationController:
        async def budget_for(self, run_id, contract):
            from aicoding_agent.regression.budget import RegressionBudget
            return await RegressionBudget.initialize(tmp_path / "budgets", run_id, contract.budget)

        async def verify_combination(self, *, workspace, source_workspace, contract, run_id,
                                     expected_revision, budget_run_id):
            assert run_id == f"{budget_run_id}-combination"
            assert await git(source_workspace, "rev-parse", "HEAD") == expected_revision
            values = [(workspace / path).read_text(encoding="utf-8")
                      for path in ("a.txt", "b.txt")]
            observations.append(values)
            failed = values == ["a1\n", "b1\n"]
            return RegressionReport(
                run_id=run_id, attempt_id="combined", registry_version=len(accepted),
                registry_fingerprint="a" * 64,
                status="assertion_failed" if failed else "passed",
                reason="The features conflict when combined" if failed else None,
            )

        async def validate_combination(self, **kwargs):
            return not stale_evidence

        async def record_combination_acceptance(
            self, *, workspace, contract, report, run_id, before_publish
        ):
            if acceptance_refused:
                raise RuntimeError("Historical registry changed before acceptance")
            await before_publish()
            accepted.append(contract.task_id)

    active = {"current": 0, "maximum": 0}
    ready = asyncio.Event()
    edits = {"first": ("a.txt", "a0\n", "a1\n"),
             "second": ("b.txt", "b0\n", "b1\n")}
    coordinator = MultiAgentCoordinator(
        workspace_manager=LocalGitWorktreeManager(tmp_path / "worktrees"),
        inspector=GitChangeInspector(),
        integrator=GitCandidateIntegrator(safe_hooks_dir=tmp_path / "hooks"),
        mailbox=CoordinatorMailboxStore(tmp_path / "mailbox"),
        max_workers=2, regression_controller=CombinationController(),
    )
    result = await coordinator.run(
        coordinator_id="combined-regression", repository=repository, task_graph=graph,
        task_contracts={key: task_contract(key) for key in edits},
        agent_factory=lambda _: EditingAgent(
            edits, active, asyncio.Lock(), parallel_tasks=set(edits), parallel_ready=ready
        ),
    )
    refused = stale_evidence or acceptance_refused
    assert len(result.completed_tasks) == (0 if refused else 1)
    assert len(result.blocked_tasks) == (2 if refused else 1)
    assert len(accepted) == (0 if refused else 1)
    if not refused:
        assert observations[-1] == ["a1\n", "b1\n"]
    else:
        assert result.integration_revision == initial_revision
    integration = Path(result.integration_lease.path)
    assert [(integration / path).read_text(encoding="utf-8") for path in ("a.txt", "b.txt")] != (
        ["a1\n", "b1\n"]
    )
    blocked = next(item for item in result.candidates if item.status == "blocked")
    if stale_evidence:
        assert "stale" in blocked.failure_reason
    elif acceptance_refused:
        assert "registry changed" in blocked.failure_reason
    else:
        assert blocked.regression_report["status"] == "assertion_failed"


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
