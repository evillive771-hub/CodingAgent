from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import Field

from aicoding_agent.domain.models import StrictModel, ToolCall, ToolResult
from aicoding_agent.model.base import ChatModel, ModelError, ModelProtocolError

RecoveryEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]
RecoverySleeper = Callable[[float], Awaitable[None]]


class RecoveryPolicy(StrictModel):
    max_model_retries: int = Field(default=2, ge=0, le=10)
    max_identical_model_failures: int = Field(default=2, ge=1, le=10)
    max_identical_tool_failures: int = Field(default=3, ge=2, le=20)
    base_delay_seconds: float = Field(default=0.5, ge=0, le=30)
    max_delay_seconds: float = Field(default=8, ge=0, le=300)


class ModelRecoveryExhaustedError(ModelError):
    """The host stopped retrying after a bounded recovery policy decision."""


class ToolCircuitDecision(StrictModel):
    fingerprint: str
    tool_name: str
    failures: int
    reason: str


class RecoveryController:
    def __init__(
        self,
        policy: RecoveryPolicy | None = None,
        *,
        sleeper: RecoverySleeper = asyncio.sleep,
    ) -> None:
        self.policy = policy or RecoveryPolicy()
        self.sleeper = sleeper

    def new_session(self) -> RecoverySession:
        return RecoverySession(policy=self.policy, sleeper=self.sleeper)


class RecoverySession:
    """Per-run recovery state; never replays a tool call automatically."""

    def __init__(self, *, policy: RecoveryPolicy, sleeper: RecoverySleeper) -> None:
        self.policy = policy
        self.sleeper = sleeper
        self._last_model_failure = ""
        self._identical_model_failures = 0
        self._last_tool_failure = ""
        self._identical_tool_failures = 0

    async def complete(
        self,
        model: ChatModel,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        emit: RecoveryEmitter,
    ):
        retries = 0
        request_messages = messages
        while True:
            try:
                response = await model.complete(request_messages, tools)
            except ModelError as exc:
                fingerprint = _model_failure_fingerprint(exc)
                if fingerprint == self._last_model_failure:
                    self._identical_model_failures += 1
                else:
                    self._last_model_failure = fingerprint
                    self._identical_model_failures = 1
                reason = self._stop_reason(exc, retries=retries)
                if reason is not None:
                    await emit(
                        "model.recovery_stopped",
                        {
                            "kind": exc.kind,
                            "error_type": type(exc).__name__,
                            "failures": retries + 1,
                            "identical_failures": self._identical_model_failures,
                            "reason": reason,
                        },
                    )
                    raise ModelRecoveryExhaustedError(
                        f"Model recovery stopped ({reason}): {exc}"
                    ) from exc
                retries += 1
                delay = self._retry_delay(exc, retries=retries)
                await emit(
                    "model.retry_scheduled",
                    {
                        "kind": exc.kind,
                        "error_type": type(exc).__name__,
                        "retry": retries,
                        "delay_seconds": delay,
                        "identical_failures": self._identical_model_failures,
                    },
                )
                await self.sleeper(delay)
                request_messages = _retry_messages(messages, exc, retry=retries)
                continue
            if retries:
                await emit("model.recovered", {"retries": retries})
            self._last_model_failure = ""
            self._identical_model_failures = 0
            return response

    def observe_tool_results(
        self,
        calls: list[ToolCall],
        results: list[ToolResult],
    ) -> ToolCircuitDecision | None:
        calls_by_id = {call.id: call for call in calls}
        for result in results:
            if result.ok:
                self._last_tool_failure = ""
                self._identical_tool_failures = 0
                continue
            if result.output.startswith("Cancelled before execution"):
                continue
            call = calls_by_id.get(result.tool_call_id)
            if call is None:
                continue
            fingerprint = _tool_failure_fingerprint(call, result)
            if fingerprint == self._last_tool_failure:
                self._identical_tool_failures += 1
            else:
                self._last_tool_failure = fingerprint
                self._identical_tool_failures = 1
            if self._identical_tool_failures >= self.policy.max_identical_tool_failures:
                return ToolCircuitDecision(
                    fingerprint=fingerprint,
                    tool_name=call.name,
                    failures=self._identical_tool_failures,
                    reason=(
                        f"Tool {call.name!r} failed identically "
                        f"{self._identical_tool_failures} times; automatic replay is disabled"
                    ),
                )
        return None

    def _stop_reason(self, exc: ModelError, *, retries: int) -> str | None:
        if not exc.retryable:
            return "non-retryable model error"
        if self._identical_model_failures >= self.policy.max_identical_model_failures:
            return "identical failure circuit opened"
        if retries >= self.policy.max_model_retries:
            return "model retry budget exhausted"
        return None

    def _retry_delay(self, exc: ModelError, *, retries: int) -> float:
        provider_delay = exc.retry_after_seconds
        if provider_delay is not None:
            return min(provider_delay, self.policy.max_delay_seconds)
        calculated = self.policy.base_delay_seconds * (2 ** (retries - 1))
        return min(calculated, self.policy.max_delay_seconds)


def _retry_messages(
    messages: list[dict[str, Any]],
    exc: ModelError,
    *,
    retry: int,
) -> list[dict[str, Any]]:
    if not isinstance(exc, ModelProtocolError):
        return messages
    return [
        *messages,
        {
            "role": "user",
            "content": (
                "<model_protocol_recovery>"
                f"Retry {retry}: return valid tool-call JSON matching the supplied schemas. "
                "Do not claim that a tool ran unless the host returns its result."
                "</model_protocol_recovery>"
            ),
        },
    ]


def _model_failure_fingerprint(exc: ModelError) -> str:
    raw = f"{exc.kind}:{type(exc).__name__}:{exc}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _tool_failure_fingerprint(call: ToolCall, result: ToolResult) -> str:
    raw = json.dumps(
        {"name": call.name, "arguments": call.arguments, "output": result.output},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
