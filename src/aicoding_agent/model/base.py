from __future__ import annotations

from typing import Any, Protocol

from aicoding_agent.domain.models import ModelResponse


class ModelError(RuntimeError):
    """Base exception for model transport and protocol failures."""


class ModelProtocolError(ModelError):
    """Raised when the model response does not match the expected protocol."""


class ChatModel(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelResponse: ...

    async def aclose(self) -> None: ...
