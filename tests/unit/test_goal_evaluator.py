from collections import deque
from typing import Any

import pytest

from aicoding_agent.agent.goal import (
    GoalEvaluationError,
    GoalEvaluationInput,
    ModelGoalEvaluator,
)
from aicoding_agent.domain.models import ModelResponse, ToolCall


class EvaluatorModel:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = deque(responses)
        self.messages: list[list[dict[str, Any]]] = []

    async def complete(self, messages, tools) -> ModelResponse:
        assert tools[0]["function"]["name"] == "submit_goal_evaluation"
        self.messages.append(messages)
        return self.responses.popleft()

    async def aclose(self) -> None:
        return None


def goal() -> GoalEvaluationInput:
    return GoalEvaluationInput(
        objective="Make the dialog accessible",
        acceptance_criteria=["The dialog has an accessible name"],
        outcome_summary="Added aria-labelledby and verified the dialog.",
        changed_files=["dialog.js"],
        host_evidence_ids=["verification:tests"],
        available_evidence={
            "outcome": "Structured coding outcome",
            "verification:tests": "Required tests passed after mutation",
        },
    )


@pytest.mark.asyncio
async def test_evaluator_returns_valid_schema_bound_verdict() -> None:
    model = EvaluatorModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="evaluation",
                        name="submit_goal_evaluation",
                        arguments={
                            "status": "complete",
                            "reason": "The accessible-name criterion is covered.",
                            "missing": [],
                            "evidence_refs": ["outcome", "verification:tests"],
                        },
                    )
                ],
                usage={"total_tokens": 42},
            )
        ]
    )

    result = await ModelGoalEvaluator(model).evaluate(goal())

    assert result.evaluation.status == "complete"
    assert result.usage["total_tokens"] == 42
    assert result.attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed_files,has_mutations,expected",
    [(["dialog.js"], False, "incomplete"), ([], True, "incomplete"), ([], False, "complete")],
)
async def test_self_report_requires_host_evidence_for_code_changes(
    changed_files, has_mutations, expected
) -> None:
    model = EvaluatorModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="evaluation",
                        name="submit_goal_evaluation",
                        arguments={
                            "status": "complete",
                            "reason": "The host-validated outcome covers the criterion.",
                            "evidence_refs": ["outcome: Structured coding outcome"],
                        },
                    )
                ]
            )
        ]
    )

    request = goal().model_copy(update={
        "changed_files": changed_files, "has_mutations": has_mutations,
    })
    result = await ModelGoalEvaluator(model).evaluate(request)

    assert result.evaluation.evidence_refs == ["outcome"]
    assert result.evaluation.status == expected
    if expected == "incomplete":
        assert "Read the final changed code" in result.evaluation.missing[0]


@pytest.mark.asyncio
async def test_evaluator_retries_protocol_but_does_not_accept_unknown_evidence() -> None:
    model = EvaluatorModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="bad-evaluation",
                        name="submit_goal_evaluation",
                        arguments={
                            "status": "complete",
                            "reason": "Invented proof.",
                            "evidence_refs": ["screenshot:invented"],
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="fixed-evaluation",
                        name="submit_goal_evaluation",
                        arguments={
                            "status": "incomplete",
                            "reason": "No browser evidence was supplied.",
                            "missing": ["Browser semantics are not evidenced"],
                            "evidence_refs": ["outcome"],
                        },
                    )
                ]
            ),
        ]
    )

    result = await ModelGoalEvaluator(model).evaluate(goal())

    assert result.evaluation.status == "incomplete"
    assert result.attempts == 2
    assert "unknown evidence" in model.messages[1][-1]["content"]


@pytest.mark.asyncio
async def test_evaluator_fails_closed_after_protocol_budget() -> None:
    model = EvaluatorModel([ModelResponse(content="No tool"), ModelResponse(content="Still no")])

    with pytest.raises(GoalEvaluationError, match="exactly one"):
        await ModelGoalEvaluator(model).evaluate(goal())
