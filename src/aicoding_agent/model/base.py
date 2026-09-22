from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from aicoding_agent.domain.models import ModelResponse


class ModelErrorKind(StrEnum):
    TRANSIENT = "transient"
    RATE_LIMIT = "rate_limit"
    PROTOCOL = "protocol"
    AUTHENTICATION = "authentication"
    REQUEST = "request"
    UNKNOWN = "unknown"


class ModelError(RuntimeError):
    """Safe model failure with host-readable recovery metadata."""

    kind = ModelErrorKind.UNKNOWN
    retryable = False

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ModelTransientError(ModelError):
    kind = ModelErrorKind.TRANSIENT
    retryable = True


class ModelRateLimitError(ModelTransientError):
    kind = ModelErrorKind.RATE_LIMIT


class ModelAuthenticationError(ModelError):
    kind = ModelErrorKind.AUTHENTICATION


class ModelRequestError(ModelError):
    kind = ModelErrorKind.REQUEST


class ModelPromptTooLongError(ModelRequestError):
    """The provider rejected a request because its input exceeded the context window."""


class ModelProtocolError(ModelError):
    """Raised when the model response does not match the expected protocol."""

    kind = ModelErrorKind.PROTOCOL
    retryable = True


class ChatModel(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelResponse: ...

    async def aclose(self) -> None: ...
