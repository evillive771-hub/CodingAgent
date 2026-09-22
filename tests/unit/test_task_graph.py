import asyncio
from pathlib import Path

import pytest

from aicoding_agent.agent.task_graph import (
    TaskGraphManager,
    TaskGraphState,
    TaskGraphStore,
    TaskNode,
    TaskNodeStatus,
)
from aicoding_agent.domain.models import TaskContract, ToolCall
from aicoding_agent.tools.base import ToolContext
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor


def task(
    task_id: str,
    *,
    status: TaskNodeStatus = TaskNodeStatus.PENDING,
    blocked_by: list[str] | None = None,
    evidence: list[str] | None = None,
    attempts: int = 0,
    owner: str = "agent",
    blocking_reason: str = "",
) -> TaskNode:
    return TaskNode(
        task_id=task_id,
        title=f"Task {task_id}",
        status=status,
        blocked_by=blocked_by or [],
        evidence=evidence or [],
        attempts=attempts,
        owner=owner,
        blocking_reason=blocking_reason,
    )


@pytest.mark.asyncio
async def test_task_graph_versions_persist_and_restore(tmp_path: Path) -> None:
    store = TaskGraphStore(tmp_path)
    manager = TaskGraphManager(store=store, scope_id="session")
    first = await manager.replace([task("inspect", status=TaskNodeStatus.IN_PROGRESS, attempts=1)])
    second = await manager.replace(
        [
            task(
                "inspect",
                status=TaskNodeStatus.COMPLETED,
                attempts=1,
                evidence=["evidence://inspection"],
            ),
            task("fix", blocked_by=["inspect"]),
        ]
    )

    restored = TaskGraphManager(store=store, scope_id="session")
    await restored.initialize()

    assert first.version == 1
    assert second.version == 2
    assert restored.current == second
    assert "fix" in restored.render()


@pytest.mark.parametrize(
    "tasks, message",
    [
        ([task("one", blocked_by=["missing"])], "unknown dependencies"),
        ([task("one", blocked_by=["two"]), task("two", blocked_by=["one"])], "cycle"),
        (
            [
                task("one", status=TaskNodeStatus.IN_PROGRESS, attempts=1),
                task("two", status=TaskNodeStatus.IN_PROGRESS, attempts=1),
            ],
            "more than one in-progress",
        ),
        (
            [
                task("one"),
                task(
                    "two",
                    status=TaskNodeStatus.IN_PROGRESS,
                    blocked_by=["one"],
                    attempts=1,
                ),
            ],
            "dependencies are incomplete",
        ),
    ],
)
@pytest.mark.asyncio
async def test_task_graph_rejects_invalid_dependency_state(
    tasks: list[TaskNode],
    message: str,
) -> None:
    manager = TaskGraphManager()

    with pytest.raises(ValueError, match=message):
        await manager.replace(tasks)


def test_completed_task_requires_evidence() -> None:
    with pytest.raises(ValueError, match="requires evidence"):
        task("done", status=TaskNodeStatus.COMPLETED, attempts=1)


@pytest.mark.asyncio
async def test_active_graph_cannot_hide_tasks_but_superseded_graph_can_replan() -> None:
    manager = TaskGraphManager()
    await manager.replace([task("old")])

    with pytest.raises(ValueError, match="cannot remove tasks"):
        await manager.replace([task("new")])

    superseded = await manager.supersede("New user direction")
    current = await manager.replace([task("new")], direction="New user direction")

    assert superseded is not None
    assert superseded.state == TaskGraphState.SUPERSEDED
    assert current.version == 3
    assert [item.task_id for item in current.tasks] == ["new"]


@pytest.mark.asyncio
async def test_attempts_and_completed_status_cannot_regress() -> None:
    manager = TaskGraphManager()
    await manager.replace(
        [
            task(
                "done",
                status=TaskNodeStatus.COMPLETED,
                attempts=2,
                evidence=["test://passed"],
            )
        ]
    )

    with pytest.raises(ValueError, match="attempts cannot decrease"):
        await manager.replace(
            [
                task(
                    "done",
                    status=TaskNodeStatus.COMPLETED,
                    attempts=1,
                    evidence=["test://passed"],
                )
            ]
        )


@pytest.mark.asyncio
async def test_update_task_graph_tool_uses_host_manager_and_contract_policy(
    tmp_path: Path,
) -> None:
    manager = TaskGraphManager()
    contract = TaskContract(
        task_id="task-graph-tool",
        objective="Create a dependency graph",
        acceptance_criteria=["The graph is persisted by the host"],
        allowed_tools={"update_task_graph"},
    )
    context = ToolContext(
        workspace=tmp_path,
        contract=contract,
        task_graph_manager=manager,
    )

    result = await ToolExecutor(build_builtin_registry()).execute(
        ToolCall(
            id="graph-1",
            name="update_task_graph",
            arguments={
                "tasks": [
                    {
                        "task_id": "inspect",
                        "title": "Inspect failure",
                        "status": "in_progress",
                        "attempts": 1,
                    }
                ]
            },
        ),
        context,
    )

    assert result.ok
    assert result.data["task_graph_version"] == 1
    assert manager.current is not None
    assert manager.current.tasks[0].task_id == "inspect"


@pytest.mark.asyncio
async def test_update_task_graph_tool_fails_without_host_manager(tmp_path: Path) -> None:
    contract = TaskContract(
        task_id="task-graph-tool-missing",
        objective="Do not write unmanaged task state",
        acceptance_criteria=["Missing host manager fails closed"],
        allowed_tools={"update_task_graph"},
    )

    result = await ToolExecutor(build_builtin_registry()).execute(
        ToolCall(
            id="graph-missing",
            name="update_task_graph",
            arguments={"tasks": [{"task_id": "inspect", "title": "Inspect failure"}]},
        ),
        ToolContext(workspace=tmp_path, contract=contract),
    )

    assert not result.ok
    assert "no task graph manager" in result.output


@pytest.mark.asyncio
async def test_task_graph_claims_are_atomic_and_unlock_dependencies() -> None:
    manager = TaskGraphManager(scope_id="coordinator-claims")
    await manager.replace(
        [
            TaskNode(task_id="first", title="First"),
            TaskNode(task_id="parallel", title="Parallel"),
            TaskNode(task_id="dependent", title="Dependent", blocked_by=["first"]),
        ]
    )

    first, parallel = await asyncio.gather(
        manager.claim_next("worker-1"),
        manager.claim_next("worker-2"),
    )

    assert first is not None and parallel is not None
    assert {first.task_id, parallel.task_id} == {"first", "parallel"}
    assert await manager.claim_next("worker-3") is None
    claimed_first = first if first.task_id == "first" else parallel
    await manager.complete_claim(
        "first",
        claimed_first.owner,
        evidence=["candidate:first"],
    )
    dependent = await manager.claim_next("worker-3")
    assert dependent is not None
    assert dependent.task_id == "dependent"


@pytest.mark.asyncio
async def test_task_graph_propagates_blocked_dependencies() -> None:
    manager = TaskGraphManager(scope_id="coordinator-blocks")
    await manager.replace(
        [
            TaskNode(task_id="root", title="Root"),
            TaskNode(task_id="child", title="Child", blocked_by=["root"]),
            TaskNode(task_id="leaf", title="Leaf", blocked_by=["child"]),
        ]
    )
    claim = await manager.claim_next("worker")
    assert claim is not None
    await manager.block_claim(claim.task_id, "worker", reason="tests failed")

    blocked = await manager.block_unreachable()

    assert [item.task_id for item in blocked] == ["child", "leaf"]
    assert manager.current is not None
    assert all(item.status == "blocked" for item in manager.current.tasks)


@pytest.mark.asyncio
async def test_separate_managers_reload_after_cross_process_style_claim_race(
    tmp_path: Path,
) -> None:
    root = tmp_path / "task-state"
    creator = TaskGraphManager(store=TaskGraphStore(root), scope_id="shared-team")
    await creator.replace(
        [
            TaskNode(task_id="first", title="First"),
            TaskNode(task_id="second", title="Second"),
        ]
    )
    first_manager = TaskGraphManager(store=TaskGraphStore(root), scope_id="shared-team")
    second_manager = TaskGraphManager(store=TaskGraphStore(root), scope_id="shared-team")
    await asyncio.gather(first_manager.initialize(), second_manager.initialize())

    first, second = await asyncio.gather(
        first_manager.claim_next("worker-1"),
        second_manager.claim_next("worker-2"),
    )

    assert first is not None and second is not None
    assert {first.task_id, second.task_id} == {"first", "second"}
    restored = TaskGraphManager(store=TaskGraphStore(root), scope_id="shared-team")
    await restored.initialize()
    assert restored.current is not None
    assert restored.current.version == 3
    assert {task.owner for task in restored.current.tasks} == {"worker-1", "worker-2"}


@pytest.mark.asyncio
async def test_pending_assignment_routes_work_without_stealing_active_claim() -> None:
    manager = TaskGraphManager(scope_id="assignment")
    await manager.replace([TaskNode(task_id="target", title="Target")])
    assigned = await manager.assign_pending("target", "worker-2")
    assert assigned.preferred_owner == "worker-2"

    assert await manager.claim_next("worker-1") is None
    claim = await manager.claim_next("worker-2")
    assert claim is not None and claim.owner == "worker-2"
    with pytest.raises(ValueError, match="not pending"):
        await manager.assign_pending("target", "worker-1")


@pytest.mark.asyncio
async def test_blocked_task_can_be_requeued_after_dependencies_complete() -> None:
    manager = TaskGraphManager(scope_id="retry")
    await manager.replace([TaskNode(task_id="retry-me", title="Retry")])
    claim = await manager.claim_next("worker-1")
    assert claim is not None
    await manager.block_claim("retry-me", "worker-1", reason="transient failure")

    retried = await manager.retry_blocked("retry-me", owner="worker-2")
    assert retried.status == "pending"
    assert retried.preferred_owner == "worker-2"
    assert retried.attempts == 1
    second = await manager.claim_next("worker-2")
    assert second is not None and second.attempts == 2
