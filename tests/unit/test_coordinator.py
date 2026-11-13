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
from aicoding_agent.regression.budget import RegressionBudget, RegressionBudgetExceeded


@pytest.mark.asyncio
@pytest.mark.parametrize("supports_budget", [False, True])
async def test_worker_coding_and_host_regression_share_persistent_budget(tmp_path, supports_budget):
    contract = TaskContract(task_id="budget", objective="Fix", acceptance_criteria=["Pass"])
    budget = await RegressionBudget.initialize(tmp_path / "budget", "assignment", contract.budget)

    class LegacyAgent:
        async def run(self, contract, workspace, *, run_id):
            return AgentResult(
                status=AgentStatus.COMPLETED, run_id=run_id, turns=1, tool_calls=2
            )

    class BudgetAgent:
        async def run(self, contract, workspace, *, run_id, shared_budget):
            assert shared_budget is budget
            await shared_budget.charge_turn()
            await shared_budget.charge_tools(2)
            return AgentResult(
                status=AgentStatus.COMPLETED, run_id=run_id, turns=1, tool_calls=2
            )

    runtime = CoordinatorWorkerRuntime(
        "worker", BudgetAgent() if supports_budget else LegacyAgent()
    )
    await runtime.run_assignment(contract, tmp_path, run_id="assignment", shared_budget=budget)
    await budget.reserve_command("host-regression")
    restored = await RegressionBudget.initialize(tmp_path / "budget", "assignment", contract.budget)
    state = await restored.refresh()
    assert state.turns == 1
    assert state.tool_calls == 3
    assert state.command_runs == {"host-regression": 1}


@pytest.mark.asyncio
async def test_host_verification_cannot_reset_exhausted_worker_budget(tmp_path):
    contract = TaskContract(task_id="limited", objective="Fix", acceptance_criteria=["Pass"])
    limits = contract.budget.model_copy(update={"max_tool_calls": 2})
    budget = await RegressionBudget.initialize(tmp_path / "budget", "limited", limits)
    await budget.charge_tools(2)
    resumed = await RegressionBudget.initialize(tmp_path / "budget", "limited", limits)
    with pytest.raises(RegressionBudgetExceeded):
        await resumed.reserve_command("host-regression")
    assert (await resumed.refresh()).tool_calls == 2
    assert (await resumed.refresh()).command_runs == {}


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
async def test_worker_receives_host_red_case_feedback_before_coding(tmp_path):
    agent = HistoryAwareAgent()
    runtime = CoordinatorWorkerRuntime("worker", agent)
    contract = TaskContract(task_id="tdd", objective="Fix clamp", acceptance_criteria=["Pass"])
    await runtime.run_assignment(
        contract, tmp_path, run_id="tdd", tdd_evidence={
            "red_cases": {"counter": ["test_zero_clamp"]},
            "red_report": {"tests": [{"id": "counter", "reason": "Expected 0, received -1"}]},
        },
    )
    history = agent.received_history[0]
    assert "test_zero_clamp" in history[-1]["content"]
    assert "Expected 0, received -1" in history[-1]["content"]
    assert "GREEN" in history[-1]["content"]


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
