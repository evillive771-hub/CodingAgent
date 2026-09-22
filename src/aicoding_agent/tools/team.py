from __future__ import annotations

from pydantic import Field

from aicoding_agent.agent.team import TeamRole
from aicoding_agent.domain.models import ToolRisk
from aicoding_agent.tools.base import BaseTool, ToolContext, ToolError, ToolExecution, ToolInput


class TeamRoleInput(ToolInput):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    responsibility: str = Field(min_length=1, max_length=2000)


class ProposeTeamInput(ToolInput):
    team_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    goal: str = Field(min_length=1, max_length=20_000)
    roles: list[TeamRoleInput] = Field(min_length=1, max_length=8)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class ProposeTeamTool(BaseTool[ProposeTeamInput]):
    name = "propose_team"
    description = (
        "Propose a bounded writable Agent team. This only records a proposal; the user must "
        "approve it separately before a Coordinator may start workers."
    )
    input_model = ProposeTeamInput
    risk = ToolRisk.WRITE

    async def run(
        self,
        arguments: ProposeTeamInput,
        context: ToolContext,
    ) -> ToolExecution:
        if context.team_launch_manager is None:
            raise ToolError("This Agent run has no team launch manager")
        proposal = await context.team_launch_manager.propose(
            team_id=arguments.team_id,
            goal=arguments.goal,
            roles=[TeamRole.model_validate(role.model_dump()) for role in arguments.roles],
            idempotency_key=arguments.idempotency_key,
        )
        role_summary = ", ".join(role.name for role in proposal.roles)
        return ToolExecution(
            output=(
                f"Team proposal {proposal.proposal_id} recorded for roles: {role_summary}. "
                "Wait for explicit user approval before starting any worker."
            ),
            data={
                "proposal_id": proposal.proposal_id,
                "team_id": proposal.team_id,
                "status": proposal.status,
                "role_count": len(proposal.roles),
            },
            is_mutation=True,
        )
