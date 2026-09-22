from collections import deque
from typing import Any

import pytest

from aicoding_agent.agent.recovery import (
    ModelRecoveryExhaustedError,
    RecoveryController,
    RecoveryPolicy,
)
from aicoding_agent.domain.models import ModelResponse, ToolCall, ToolResult
from aicoding_agent.model.base import (
    ModelAuthenticationError,
    ModelProtocolError,
    ModelTransientError,
)


class RecoveryModel:
    def __init__(self, outcomes: list[ModelResponse | Exception]) -> None:
        self.outcomes = deque(outcomes)
        self.messages: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelResponse:
        del tools
        self.messages.append(messages)
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def aclose(self) -> None:
        return None


def controller(**updates: object) -> tuple[RecoveryController, list[float]]:
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    policy = RecoveryPolicy.model_validate(
        {
            "max_model_retries": 3,
            "max_identical_model_failures": 3,
            "base_delay_seconds": 0.25,
            "max_delay_seconds": 2,
            **updates,
        }
    )
    return RecoveryController(policy, sleeper=record_sleep), sleeps


async def event_recorder(events: list[tuple[str, dict[str, Any]]], kind: str, data: dict) -> None:
    events.append((kind, data))


@pytest.mark.asyncio
async def test_transient_model_failure_retries_with_backoff_and_recovers() -> None:
    recovery, sleeps = controller()
    model = RecoveryModel([ModelTransientError("temporary outage"), ModelResponse(content="ok")])
    events: list[tuple[str, dict[str, Any]]] = []

    response = await recovery.new_session().complete(
        model,
        [{"role": "user", "content": "work"}],
        [],
        emit=lambda kind, data: event_recorder(events, kind, data),
    )

    assert response.content == "ok"
    assert sleeps == [0.25]
    assert [kind for kind, _ in events] == ["model.retry_scheduled", "model.recovered"]


@pytest.mark.asyncio
async def test_protocol_retry_adds_ephemeral_correction_hint() -> None:
    recovery, _ = controller()
    model = RecoveryModel([ModelProtocolError("invalid tool call"), ModelResponse(content="valid")])

    await recovery.new_session().complete(
        model,
        [{"role": "user", "content": "work"}],
        [],
        emit=lambda kind, data: event_recorder([], kind, data),
    )

    assert len(model.messages[0]) == 1
    assert "<model_protocol_recovery>" in model.messages[1][-1]["content"]


@pytest.mark.asyncio
async def test_non_retryable_authentication_failure_stops_immediately() -> None:
    recovery, sleeps = controller()
    model = RecoveryModel([ModelAuthenticationError("credentials rejected")])
    events: list[tuple[str, dict[str, Any]]] = []

    with pytest.raises(ModelRecoveryExhaustedError, match="non-retryable"):
        await recovery.new_session().complete(
            model,
            [],
            [],
            emit=lambda kind, data: event_recorder(events, kind, data),
        )

    assert len(model.messages) == 1
    assert sleeps == []
    assert events[0][0] == "model.recovery_stopped"


@pytest.mark.asyncio
async def test_identical_model_failure_opens_circuit_before_retry_budget() -> None:
    recovery, sleeps = controller(
        max_model_retries=5,
        max_identical_model_failures=2,
    )
    model = RecoveryModel(
        [
            ModelTransientError("same outage"),
            ModelTransientError("same outage"),
            ModelResponse(content="must not be reached"),
        ]
    )

    with pytest.raises(ModelRecoveryExhaustedError, match="circuit opened"):
        await recovery.new_session().complete(
            model,
            [],
            [],
            emit=lambda kind, data: event_recorder([], kind, data),
        )

    assert len(model.messages) == 2
    assert sleeps == [0.25]


def test_repeated_identical_tool_failure_opens_circuit_without_replay() -> None:
    recovery, _ = controller(max_identical_tool_failures=3)
    session = recovery.new_session()
    call = ToolCall(id="call", name="read_file", arguments={"path": "missing.txt"})

    for index in range(2):
        decision = session.observe_tool_results(
            [call.model_copy(update={"id": f"call-{index}"})],
            [
                ToolResult(
                    tool_call_id=f"call-{index}",
                    tool_name="read_file",
                    ok=False,
                    output="File does not exist: missing.txt",
                )
            ],
        )
        assert decision is None
    decision = session.observe_tool_results(
        [call.model_copy(update={"id": "call-3"})],
        [
            ToolResult(
                tool_call_id="call-3",
                tool_name="read_file",
                ok=False,
                output="File does not exist: missing.txt",
            )
        ],
    )

    assert decision is not None
    assert decision.failures == 3
    assert "automatic replay is disabled" in decision.reason


def test_successful_tool_result_resets_identical_failure_counter() -> None:
    recovery, _ = controller(max_identical_tool_failures=2)
    session = recovery.new_session()
    call = ToolCall(id="failed", name="read_file", arguments={"path": "missing.txt"})
    failure = ToolResult(
        tool_call_id="failed",
        tool_name="read_file",
        ok=False,
        output="missing",
    )
    assert session.observe_tool_results([call], [failure]) is None
    success_call = call.model_copy(update={"id": "success"})
    success = ToolResult(
        tool_call_id="success",
        tool_name="read_file",
        ok=True,
        output="ok",
    )
    assert session.observe_tool_results([success_call], [success]) is None
    again_call = call.model_copy(update={"id": "again"})
    again = failure.model_copy(update={"tool_call_id": "again"})
    assert session.observe_tool_results([again_call], [again]) is None
