from aicoding_agent.model.base import (
    ChatModel,
    ModelAuthenticationError,
    ModelError,
    ModelErrorKind,
    ModelPromptTooLongError,
    ModelProtocolError,
    ModelRateLimitError,
    ModelRequestError,
    ModelTransientError,
)
from aicoding_agent.model.mimo import MimoChatModel, OpenAICompatibleChatModel


def create_chat_model(settings, *, client=None):
    """Create the model adapter for the configured provider."""
    return OpenAICompatibleChatModel(settings, client=client)


__all__ = [
    "ChatModel",
    "MimoChatModel",
    "OpenAICompatibleChatModel",
    "create_chat_model",
    "ModelAuthenticationError",
    "ModelError",
    "ModelErrorKind",
    "ModelProtocolError",
    "ModelPromptTooLongError",
    "ModelRateLimitError",
    "ModelRequestError",
    "ModelTransientError",
]
