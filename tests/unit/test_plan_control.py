from pathlib import Path

import pytest

from aicoding_agent.agent.control import ControlIntent, ControlStore, SteeringInbox
from aicoding_agent.agent.plan import (
    PlanItem,
    PlanManager,
    PlanState,
    PlanStatus,
    PlanStore,
)


@pytest.mark.asyncio
async def test_plan_versions_persist_and_restore(tmp_path: Path) -> None:
    store = PlanStore(tmp_path / "state")
    manager = PlanManager(store=store, scope_id="session-1")

    first = await manager.replace(
        [PlanItem(content="Inspect the bug", status=PlanStatus.IN_PROGRESS)]
    )
    superseded = await manager.supersede("Fix login before search")
    final = await manager.replace(
        [PlanItem(content="Fix login", status=PlanStatus.COMPLETED)],
        direction="Fix login before search",
    )

    assert first.version == 1
    assert superseded is not None and superseded.state == PlanState.SUPERSEDED
    assert final.version == 3
    restored = PlanManager(store=store, scope_id="session-1")
    await restored.initialize()
    assert restored.current == final
    assert "[x] Fix login" in restored.render()


def test_plan_rejects_more_than_one_in_progress_item() -> None:
    with pytest.raises(ValueError, match="Only one"):
        from aicoding_agent.agent.plan import PlanSnapshot

        PlanSnapshot(
            version=1,
            items=[
                PlanItem(content="One", status=PlanStatus.IN_PROGRESS),
                PlanItem(content="Two", status=PlanStatus.IN_PROGRESS),
            ],
        )


@pytest.mark.asyncio
async def test_control_queue_is_durable_and_intent_specific(tmp_path: Path) -> None:
    store = ControlStore(tmp_path / "state")
    inbox = SteeringInbox(store=store, scope_id="session-1")
    await inbox.submit(ControlIntent.NEXT, "Run accessibility checks")
    await inbox.submit(ControlIntent.STEER, "Fix login first")

    current = await inbox.consume_current()
    assert [message.content for message in current] == ["Fix login first"]

    restored = SteeringInbox(store=store, scope_id="session-1")
    await restored.initialize()
    queued = await restored.consume_next()
    assert queued is not None
    assert queued.content == "Run accessibility checks"
    assert not restored.pending
