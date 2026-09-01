from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SENSITIVE_MARKERS = ("api_key", "apikey", "authorization", "cookie", "password", "secret", "token")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if any(marker in key.lower() for marker in SENSITIVE_MARKERS):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value


class EventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    event_type: str
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    sequence: int | None = None

    def safe_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["data"] = redact(payload["data"])
        return payload
