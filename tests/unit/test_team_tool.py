from pathlib import Path

import pytest

from aicoding_agent.agent.team import TeamLaunchManager, TeamLaunchStatus
from aicoding_agent.domain.models import TaskContract, ToolCall
from aicoding_agent.tools.base import ToolContext
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor


@pytest.mark.asyncio
async def test_agent_can_only_propose_team_for_separate_approval(tmp_path: Path) -> None:
    manager = TeamLaunchManager(tmp_path / "runtime")
    context = ToolContext(
        workspace=tmp_path,
        run_id="lead-run",
        contract=TaskContract(
            task_id="lead",
            objective="Plan parallel work",
            acceptance_criteria=["Record a proposal"],
            allowed_tools={"propose_team"},
        ),
        team_launch_manager=manager,
    )

    result = await ToolExecutor(build_builtin_registry()).execute(
        ToolCall(
            id="proposal-call",
            name="propose_team",
            arguments={
                "team_id": "team-1",
                "goal": "Implement and test the frontend change",
                "roles": [
                    {"name": "implementer", "responsibility": "Implement the change"},
                    {"name": "reviewer", "responsibility": "Review and test the change"},
                ],
                "idempotency_key": "lead-run-team-1",
            },
        ),
        context,
    )

    assert result.ok
    proposal = await manager.current("team-1")
    assert proposal is not None
    assert proposal.status == TeamLaunchStatus.PROPOSED
    assert result.data["proposal_id"] == proposal.proposal_id
    assert "explicit user approval" in result.output


@pytest.mark.asyncio
async def test_team_proposal_tool_fails_closed_without_host_manager(tmp_path: Path) -> None:
    context = ToolContext(
        workspace=tmp_path,
        contract=TaskContract(
            task_id="lead",
            objective="Plan parallel work",
            acceptance_criteria=["No implicit startup"],
            allowed_tools={"propose_team"},
        ),
    )
    result = await ToolExecutor(build_builtin_registry()).execute(
        ToolCall(
            id="proposal-call",
            name="propose_team",
            arguments={
                "team_id": "team-1",
                "goal": "Work",
                "roles": [{"name": "worker", "responsibility": "Do work"}],
                "idempotency_key": "proposal-1",
            },
        ),
        context,
    )

    assert not result.ok
    assert "no team launch manager" in result.output
