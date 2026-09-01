from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from aicoding_agent.agent.plan import PlanState, PlanStatus
from aicoding_agent.domain.models import CodingOutcome, TaskContract
from aicoding_agent.tools.base import ToolContext


class GateAction(StrEnum):
    COMPLETE = "complete"
    BLOCK = "block"


class GateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: GateAction
    reason: str


class CompletionGate:
    def evaluate(self, contract: TaskContract, context: ToolContext) -> GateDecision:
        if context.plan_manager is not None:
            plan = context.plan_manager.current
            if plan is not None and plan.state == PlanState.SUPERSEDED:
                return GateDecision(
                    action=GateAction.BLOCK,
                    reason="The previous plan was superseded by user steering; create a new plan.",
                )
            if plan is not None and any(item.status != PlanStatus.COMPLETED for item in plan.items):
                return GateDecision(
                    action=GateAction.BLOCK,
                    reason=(
                        "The active plan still contains unfinished items; "
                        "update it before stopping."
                    ),
                )
        outcome = context.state.get("coding_outcome")
        if not isinstance(outcome, CodingOutcome):
            return GateDecision(
                action=GateAction.BLOCK,
                reason=(
                    "Completion evidence is incomplete: call submit_result with a summary, "
                    "changed files, and risk notes after verification."
                ),
            )

        last_mutation = int(context.state.get("last_mutation_sequence", 0))
        if last_mutation == 0 and not outcome.changed_files:
            return GateDecision(
                action=GateAction.COMPLETE,
                reason="A structured read-only outcome is present; no verification is required.",
            )
        successes: dict[str, int] = context.state.get("verification_success", {})
        missing: list[str] = []
        stale: list[str] = []
        for command in contract.verification_commands:
            sequence = successes.get(command.id)
            if sequence is None:
                missing.append(command.id)
            elif sequence <= last_mutation:
                stale.append(command.id)
        if missing or stale:
            details: list[str] = []
            if missing:
                details.append(f"not successfully run: {', '.join(missing)}")
            if stale:
                details.append(f"must be rerun after the last mutation: {', '.join(stale)}")
            return GateDecision(
                action=GateAction.BLOCK,
                reason="Completion evidence is incomplete; " + "; ".join(details) + ".",
            )
        return GateDecision(
            action=GateAction.COMPLETE,
            reason="Structured outcome and all deterministic verification evidence are present.",
        )
