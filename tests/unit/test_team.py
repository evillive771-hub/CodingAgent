import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from aicoding_agent.agent.team import (
    PlanApprovalStatus,
    TeamLaunchManager,
    TeamLaunchStatus,
    TeamMessage,
    TeamMessageBus,
    TeamMessageType,
    TeamPlanApprovalManager,
    TeamProtocolError,
    TeamRole,
)


@pytest.mark.asyncio
async def test_team_bus_persists_typed_bidirectional_messages(tmp_path: Path) -> None:
    bus = TeamMessageBus(tmp_path / "teams")
    direct = await bus.send(
        team_id="team-1",
        sender="lead",
        recipient="worker-1",
        message_type=TeamMessageType.DIRECT,
        content="Review the parser first",
    )
    result = await bus.send(
        team_id="team-1",
        sender="worker-1",
        recipient="lead",
        message_type=TeamMessageType.RESULT,
        content="Parser review completed",
        task_id="parser",
    )

    restarted = TeamMessageBus(tmp_path / "teams")
    assert await restarted.read("team-1", "worker-1") == [direct]
    assert await restarted.read("team-1", "lead") == [result]
    assert await restarted.read("team-1", "lead", after_sequence=result.sequence) == []


@pytest.mark.asyncio
async def test_team_bus_waits_without_model_polling(tmp_path: Path) -> None:
    bus = TeamMessageBus(tmp_path / "teams", poll_seconds=0.01)
    waiting = asyncio.create_task(
        bus.wait_for_messages("team-1", "worker-1", timeout_seconds=1)
    )
    await asyncio.sleep(0)
    sent = await bus.send(
        team_id="team-1",
        sender="lead",
        recipient="worker-1",
        message_type=TeamMessageType.IDLE,
        content="wake",
    )

    assert await waiting == [sent]


def test_control_protocol_messages_require_request_id() -> None:
    with pytest.raises(ValidationError, match="requires request_id"):
        TeamMessage(
            message_id="a" * 32,
            sequence=1,
            team_id="team-1",
            sender="lead",
            recipient="worker-1",
            message_type=TeamMessageType.SHUTDOWN_REQUEST,
        )


@pytest.mark.asyncio
async def test_team_bus_rejects_unbounded_message(tmp_path: Path) -> None:
    bus = TeamMessageBus(tmp_path / "teams", max_message_chars=1000)

    with pytest.raises(TeamProtocolError, match="bounded size"):
        await bus.send(
            team_id="team-1",
            sender="lead",
            recipient="worker-1",
            message_type=TeamMessageType.DIRECT,
            content="x" * 2000,
        )

    assert await bus.read("team-1", "worker-1") == []


@pytest.mark.asyncio
async def test_team_launch_requires_separate_user_approval(tmp_path: Path) -> None:
    manager = TeamLaunchManager(tmp_path / "teams")
    roles = [TeamRole(name="frontend", responsibility="Implement the UI fix")]
    proposed = await manager.propose(
        team_id="team-1",
        goal="Fix the frontend defect",
        roles=roles,
        idempotency_key="proposal-1",
    )

    with pytest.raises(TeamProtocolError, match="requires user approval"):
        await manager.require_approved("team-1", proposed.proposal_id)

    approved = await manager.approve("team-1", proposed.proposal_id)
    assert approved.status == TeamLaunchStatus.APPROVED
    assert await manager.require_approved("team-1", proposed.proposal_id) == approved
    started = await manager.mark_started("team-1", proposed.proposal_id)
    assert started.status == TeamLaunchStatus.STARTED


@pytest.mark.asyncio
async def test_team_proposal_is_idempotent_and_content_bound(tmp_path: Path) -> None:
    manager = TeamLaunchManager(tmp_path / "teams")
    roles = [TeamRole(name="reviewer", responsibility="Review the candidate")]
    first = await manager.propose(
        team_id="team-1",
        goal="Review",
        roles=roles,
        idempotency_key="proposal-1",
    )
    repeated = await manager.propose(
        team_id="team-1",
        goal="Review",
        roles=roles,
        idempotency_key="proposal-1",
    )
    assert repeated == first

    with pytest.raises(TeamProtocolError, match="different content"):
        await manager.propose(
            team_id="team-1",
            goal="Different goal",
            roles=roles,
            idempotency_key="proposal-1",
        )


@pytest.mark.asyncio
async def test_plan_approval_is_bound_to_task_worker_and_work_version(tmp_path: Path) -> None:
    bus = TeamMessageBus(tmp_path / "messages")
    manager = TeamPlanApprovalManager(tmp_path / "approvals", message_bus=bus)
    request = await manager.request(
        team_id="team-1",
        worker="worker-1",
        task_id="frontend",
        work_version=1,
        plan="Change the component, then run its focused test.",
    )
    assert request.status == PlanApprovalStatus.REQUESTED
    lead_messages = await bus.read("team-1", "lead")
    assert lead_messages[-1].message_type == TeamMessageType.PLAN_APPROVAL_REQUEST

    approved = await manager.respond(
        team_id="team-1",
        worker="worker-1",
        request_id=request.request_id,
        approve=True,
        feedback="Proceed",
    )
    assert approved.status == PlanApprovalStatus.APPROVED
    assert await manager.require_approved(
        team_id="team-1",
        worker="worker-1",
        task_id="frontend",
        work_version=1,
        plan_sha256=request.plan_sha256,
    ) == approved

    next_request = await manager.request(
        team_id="team-1",
        worker="worker-1",
        task_id="frontend",
        work_version=2,
        plan="Change the component, then run its focused test.",
    )
    assert next_request.request_id != request.request_id
    with pytest.raises(TeamProtocolError, match="does not match current work"):
        await manager.require_approved(
            team_id="team-1",
            worker="worker-1",
            task_id="frontend",
            work_version=1,
            plan_sha256=request.plan_sha256,
        )


@pytest.mark.asyncio
async def test_plan_waiter_observes_separate_approval_response(tmp_path: Path) -> None:
    manager = TeamPlanApprovalManager(tmp_path / "approvals", poll_seconds=0.01)
    request = await manager.request(
        team_id="team-1",
        worker="worker-1",
        task_id="frontend",
        work_version=1,
        plan="Implement safely.",
    )
    waiting = asyncio.create_task(
        manager.wait_for_decision(
            team_id="team-1",
            worker="worker-1",
            request_id=request.request_id,
            timeout_seconds=1,
        )
    )
    await asyncio.sleep(0)
    await manager.respond(
        team_id="team-1",
        worker="worker-1",
        request_id=request.request_id,
        approve=False,
        feedback="Revise the test step",
    )

    decision = await waiting
    assert decision.status == PlanApprovalStatus.REJECTED
    assert decision.feedback == "Revise the test step"
