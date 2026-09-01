from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from aicoding_agent.domain.models import AgentStatus, CodingOutcome


class ChatModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatTurn(ChatModel):
    turn_number: int = Field(ge=1)
    run_id: str = Field(min_length=1, max_length=128)
    user_message: str = Field(min_length=1, max_length=20000)
    assistant_message: str = Field(min_length=1, max_length=20000)
    status: AgentStatus
    outcome: CodingOutcome | None = None
    model_turns: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    reason: str | None = Field(default=None, max_length=5000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
