from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SENSITIVE_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
)
TOKEN_METRIC_KEYS = {
    "compact_buffer_tokens",
    "compact_threshold_tokens",
    "completion_tokens",
    "context_tokens",
    "context_window_tokens",
    "effective_context_tokens",
    "estimated_tokens",
    "goal_tokens",
    "input_tokens",
    "archived_tokens",
    "last_prompt_tokens",
    "max_output_tokens",
    "max_recent_tokens",
    "min_recent_tokens",
    "model_tokens",
    "output_tokens",
    "prompt_tokens",
    "raw_tokens",
    "source_tokens",
    "summary_output_tokens",
    "summary_tokens",
    "token_count",
    "total_tokens",
    "warning_buffer_tokens",
    "warning_threshold_tokens",
}
_SECRET_VALUE_PATTERN = re.compile(
    r"(?i)\b(api[_ -]?key|access[_ -]?token|authorization|password|secret|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[^\s,;]+")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = key.lower()
            if normalized_key not in TOKEN_METRIC_KEYS and any(
                marker in normalized_key for marker in SENSITIVE_MARKERS
            ):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = _SECRET_VALUE_PATTERN.sub(r"\1\2<redacted>", value)
        return _BEARER_PATTERN.sub("Bearer <redacted>", value)
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
