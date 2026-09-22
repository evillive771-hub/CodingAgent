from pathlib import Path

import pytest

from aicoding_agent.adapters.ports import ChangeSnapshot
from aicoding_agent.agent.coordinator import (
    CoordinatorError,
    CoordinatorMailboxStore,
    CoordinatorMergeGate,
    CoordinatorWorkerRuntime,
    WorkerLifecycle,
)
from aicoding_agent.domain.models import (
    AgentResult,
    AgentStatus,
    CodingOutcome,
    TaskContract,
)


class StaticInspector:
    async def snapshot(self, workspace: Path) -> ChangeSnapshot:
        del workspace
        return ChangeSnapshot(
            change_hash="a" * 64,
            base_revision="b" * 40,
            changed_files=["app.py"],
        )


class HistoryAwareAgent:
    def __init__(self) -> None:
        self.received_history: list[list[dict[str, str]]] = []

    async def run(
        self,
        contract: TaskContract,
        workspace: Path,
        *,
        run_id: str | None = None,
        conversation_history: list[dict[str, str]] | None = None,
    ) -> AgentResult:
        del workspace
        self.received_history.append(list(conversation_history or []))
        return AgentResult(
            status=AgentStatus.COMPLETED,
            run_id=run_id or "run",
            final_text=f"Finished {contract.objective}",
            outcome=CodingOutcome(summary="Finished", changed_files=["app.py"]),
        )


@pytest.mark.asyncio
async def test_worker_runtime_reuses_agent_and_carries_bounded_assignment_history(
    tmp_path: Path,
) -> None:
    agent = HistoryAwareAgent()
    runtime = CoordinatorWorkerRuntime("worker-1", agent)
    first = TaskContract(
        task_id="coord:first",
        objective="First assignment",
        acceptance_criteria=["done"],
    )
    second = first.model_copy(
        update={"task_id": "coord:second", "objective": "Second assignment"}
    )

    await runtime.run_assignment(first, tmp_path, run_id="first-run")
    await runtime.run_assignment(second, tmp_path, run_id="second-run")

    assert agent.received_history[0] == []
    assert agent.received_history[1] == [
        {"role": "user", "content": "First assignment"},
        {"role": "assistant", "content": "Finished First assignment"},
    ]
    assert runtime.state.assignments_completed == 2
    assert runtime.state.lifecycle == WorkerLifecycle.IDLE


@pytest.mark.asyncio
async def test_coordinator_mailbox_is_append_only_and_persistent(tmp_path: Path) -> None:
    store = CoordinatorMailboxStore(tmp_path / "coordinator")
    first = await store.append(
        coordinator_id="coord-1",
        owner="worker-1",
        task_id="task-a",
        event_type="task.claimed",
        payload={"attempt": 1},
    )
    second = await store.append(
        coordinator_id="coord-1",
        owner="worker-1",
        task_id="task-a",
        event_type="candidate.integrated",
        payload={"revision": "a" * 40},
    )

    restarted = CoordinatorMailboxStore(tmp_path / "coordinator")
    loaded = await restarted.load("coord-1", "worker-1")
    assert [item.sequence for item in loaded] == [1, 2]
    assert loaded == [first, second]


@pytest.mark.asyncio
async def test_coordinator_mailbox_rejects_unbounded_payload(tmp_path: Path) -> None:
    store = CoordinatorMailboxStore(tmp_path / "coordinator", max_payload_chars=100)

    with pytest.raises(CoordinatorError, match="bounded size"):
        await store.append(
            coordinator_id="coord-1",
            owner="worker-1",
            task_id="task-a",
            event_type="tool.output",
            payload={"output": "x" * 1000},
        )

    assert await store.load("coord-1", "worker-1") == []


@pytest.mark.asyncio
async def test_merge_gate_requires_verification_and_exact_changed_files(
    tmp_path: Path,
) -> None:
    gate = CoordinatorMergeGate()
    contract = TaskContract(
        task_id="candidate",
        objective="Validate a candidate",
        acceptance_criteria=["The merge gate fails closed"],
    )
    mismatched = AgentResult(
        status=AgentStatus.COMPLETED,
        run_id="run",
        outcome=CodingOutcome(summary="Changed", changed_files=["other.py"]),
    )

    with pytest.raises(CoordinatorError, match="changed_files"):
        await gate.validate(
            workspace=tmp_path,
            contract=contract,
            result=mismatched,
            inspector=StaticInspector(),
            expected_base_revision="b" * 40,
        )

    matching = mismatched.model_copy(
        update={"outcome": CodingOutcome(summary="Changed", changed_files=["app.py"])}
    )
    with pytest.raises(CoordinatorError, match="at least one verification"):
        await gate.validate(
            workspace=tmp_path,
            contract=contract,
            result=matching,
            inspector=StaticInspector(),
            expected_base_revision="b" * 40,
        )
