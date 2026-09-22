from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aicoding_agent.agent.memory import (
    MemoryError,
    MemoryManager,
    MemoryStatus,
    MemoryStore,
)
from aicoding_agent.domain.models import Budget, TaskContract, ToolCall
from aicoding_agent.tools.base import ToolContext
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, *, days: int) -> None:
        self.value += timedelta(days=days)


@pytest.mark.asyncio
async def test_curated_memory_requires_approval_and_persists_versions(tmp_path: Path) -> None:
    clock = Clock()
    store = MemoryStore(tmp_path / "memory")
    manager = MemoryManager(store=store, now=clock.now)
    proposal = await manager.propose(
        topic="test-command",
        content="Use the project-local uv binary for Python checks.",
        ttl_days=90,
        source_run_id="agent-run",
    )

    assert proposal.status == MemoryStatus.PENDING
    assert await manager.recall("uv binary") == []
    approved = await manager.approve(proposal.memory_id[:8])
    assert approved.approved_by == "user"
    assert [entry.memory_id for entry in await manager.recall("uv binary")] == [proposal.memory_id]

    newer = await manager.add_user_memory(
        topic="test-command",
        content="Use uv run --no-sync for Python checks.",
        ttl_days=30,
    )
    assert newer.version == 2
    recalled = await manager.recall("uv")
    assert [entry.version for entry in recalled] == [2]
    assert recalled[0].source == "user"

    restarted = MemoryManager(store=store, now=clock.now)
    await restarted.initialize()
    assert [entry.version for entry in await restarted.recall("uv")] == [2]


@pytest.mark.asyncio
async def test_expired_memory_is_not_recalled(tmp_path: Path) -> None:
    clock = Clock()
    manager = MemoryManager(now=clock.now)
    await manager.add_user_memory(
        topic="frontend-style",
        content="Use the compact button spacing requested by the user.",
        ttl_days=1,
    )

    assert len(await manager.recall("button")) == 1
    clock.advance(days=2)
    assert await manager.recall("button") == []
    assert "expired" in manager.render()


@pytest.mark.asyncio
async def test_secret_like_memory_is_rejected_before_persistence(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    manager = MemoryManager(store=store)

    with pytest.raises(MemoryError, match="looks like a secret"):
        await manager.add_user_memory(
            topic="deployment",
            content="api_key=supersecretvalue",
            ttl_days=30,
        )

    assert not store.path.exists()
    assert manager.entries == ()


@pytest.mark.asyncio
async def test_memory_tools_only_recall_user_approved_entries(tmp_path: Path) -> None:
    manager = MemoryManager(store=MemoryStore(tmp_path / "memory"))
    context = ToolContext(
        workspace=tmp_path,
        run_id="memory-run",
        memory_manager=manager,
        contract=TaskContract(
            task_id="memory-tools",
            objective="Use curated memory",
            acceptance_criteria=["Unapproved memory is excluded"],
            allowed_tools={"propose_memory", "recall_memory"},
        ),
    )
    executor = ToolExecutor(build_builtin_registry())

    proposed = await executor.execute(
        ToolCall(
            id="propose",
            name="propose_memory",
            arguments={
                "topic": "component-tests",
                "content": "Run the counter component test after editing it.",
                "ttl_days": 60,
            },
        ),
        context,
    )
    before_approval = await executor.execute(
        ToolCall(
            id="recall-before",
            name="recall_memory",
            arguments={"query": "counter test"},
        ),
        context,
    )

    assert proposed.ok
    assert before_approval.ok
    assert before_approval.data["count"] == 0
    await manager.approve(str(proposed.data["memory_id"])[:8])
    after_approval = await executor.execute(
        ToolCall(
            id="recall-after",
            name="recall_memory",
            arguments={"query": "counter test"},
        ),
        context,
    )
    assert after_approval.data["count"] == 1
    assert "Run the counter component test" in after_approval.output


@pytest.mark.asyncio
async def test_rejected_proposal_cannot_be_recalled_or_approved(tmp_path: Path) -> None:
    manager = MemoryManager()
    proposal = await manager.propose(
        topic="obsolete",
        content="Use the old test command.",
        ttl_days=30,
        source_run_id="run",
    )
    rejected = await manager.reject(proposal.memory_id[:8])

    assert rejected.status == MemoryStatus.REJECTED
    assert await manager.recall("old test") == []
    with pytest.raises(MemoryError, match="Only pending"):
        await manager.approve(proposal.memory_id[:8])


@pytest.mark.asyncio
async def test_memory_recall_tool_keeps_output_within_contract_budget(tmp_path: Path) -> None:
    manager = MemoryManager()
    for index in range(10):
        await manager.add_user_memory(
            topic=f"frontend-{index}",
            content=f"frontend guideline {index}: " + ("x" * 1900),
            ttl_days=30,
        )
    context = ToolContext(
        workspace=tmp_path,
        memory_manager=manager,
        contract=TaskContract(
            task_id="bounded-memory",
            objective="Bound recalled memory",
            acceptance_criteria=["Output obeys the contract budget"],
            allowed_tools={"recall_memory"},
            budget=Budget(max_tool_output_chars=1000),
        ),
    )

    result = await ToolExecutor(build_builtin_registry()).execute(
        ToolCall(
            id="recall",
            name="recall_memory",
            arguments={"query": "frontend", "max_results": 10},
        ),
        context,
    )

    assert result.ok
    assert len(result.output) <= 1000
    assert 0 < result.data["count"] < 10
