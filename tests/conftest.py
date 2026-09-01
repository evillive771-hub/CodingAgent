from __future__ import annotations

from collections import deque
from typing import Any

from aicoding_agent.domain.models import ModelResponse


class ScriptedModel:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = deque(responses)
        self.requests: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelResponse:
        self.requests.append((messages, tools))
        if not self.responses:
            raise AssertionError("ScriptedModel received more calls than expected")
        return self.responses.popleft()

    async def aclose(self) -> None:
        return None
