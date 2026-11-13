from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from aicoding_agent.agent.plan import PlanState, PlanStatus
from aicoding_agent.agent.task_graph import TaskGraphState, TaskNodeStatus
from aicoding_agent.domain.models import CodingOutcome, TaskContract
from aicoding_agent.tools.base import ToolContext


class GateAction(StrEnum):
    COMPLETE = "complete"
    UNVERIFIED = "unverified"
    BLOCK = "block"


class GateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: GateAction
    reason: str


class CompletionGate:
    async def refresh_browser_evidence(self, context: ToolContext) -> None:
        """Recheck host evidence; resumed messages cannot restore verification success."""
        from aicoding_agent.adapters.browser_tests import BrowserTestResult

        successes = context.state.get("verification_success", {})
        for verification in context.contract.browser_verifications:
            if verification.id not in successes:
                continue
            raw = context.state.get("browser_verification_results", {}).get(verification.id)
            valid = False
            if context.browser_runner is not None and raw is not None:
                try:
                    result = BrowserTestResult.model_validate(raw)
                    valid = result.status == "passed" and await context.browser_runner.revalidate(
                        context.workspace, result
                    )
                except (ValueError, OSError, RuntimeError):
                    valid = False
            if not valid:
                successes.pop(verification.id, None)
                if context.emit is not None:
                    await context.emit(
                        "browser.verification_invalidated",
                        {
                            "verification_id": verification.id,
                            "reason": "evidence_changed_or_missing",
                        },
                    )

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
        if context.task_graph_manager is not None:
            graph = context.task_graph_manager.current
            if graph is not None and graph.state == TaskGraphState.SUPERSEDED:
                return GateDecision(
                    action=GateAction.BLOCK,
                    reason=(
                        "The previous task graph was superseded by user steering; "
                        "create a new task graph."
                    ),
                )
            if graph is not None and any(
                task.status != TaskNodeStatus.COMPLETED for task in graph.tasks
            ):
                return GateDecision(
                    action=GateAction.BLOCK,
                    reason=(
                        "The active task graph still contains unfinished tasks; "
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
        browser_required = [item for item in contract.browser_verifications if item.required]
        if last_mutation == 0 and not outcome.changed_files and not browser_required:
            return GateDecision(
                action=GateAction.COMPLETE,
                reason="A structured read-only outcome is present; no verification is required.",
            )
        required = [*contract.verification_commands, *browser_required]
        if not required:
            return GateDecision(
                action=GateAction.UNVERIFIED,
                reason=(
                    "Structured outcome submitted, but code changes cannot be marked complete "
                    "because no verification commands were configured."
                ),
            )
        successes: dict[str, int] = context.state.get("verification_success", {})
        missing: list[str] = []
        stale: list[str] = []
        for command in required:
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
