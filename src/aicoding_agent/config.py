from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator


class ConfigurationError(ValueError):
    """Raised when an environment file cannot produce a usable configuration."""


def read_env_file(path: Path) -> dict[str, str]:
    """Read a small dotenv subset, using the last definition of duplicate keys."""
    if not path.is_file():
        raise ConfigurationError(f"Environment file does not exist: {path}")

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "a").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


class ModelSettings(BaseModel):
    provider: str = "mimo"
    model: str
    api_key: SecretStr
    base_url: str
    max_output_tokens: int = Field(default=8192, ge=256, le=131072)
    request_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    max_retries: int = Field(default=2, ge=0, le=5)
    thinking_enabled: bool = True
    context_window_tokens: int = Field(default=131_072, ge=8_192, le=4_000_000)
    summary_max_output_tokens: int = Field(default=4_096, ge=256, le=32_768)
    context_compact_buffer_tokens: int = Field(default=13_000, ge=512, le=200_000)
    context_warning_buffer_tokens: int = Field(default=20_000, ge=1_024, le=300_000)
    session_memory_min_recent_tokens: int = Field(default=10_000, ge=0, le=200_000)
    session_memory_max_recent_tokens: int = Field(default=40_000, ge=256, le=500_000)
    auto_compact: bool = True
    auto_resume: Literal["safe", "ask", "disabled"] = "safe"

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("base_url must be an HTTPS URL without user information")
        return normalized

    @model_validator(mode="after")
    def context_limits_are_consistent(self) -> ModelSettings:
        reserved = min(self.max_output_tokens, 20_000)
        effective = self.context_window_tokens - reserved
        if self.context_warning_buffer_tokens <= self.context_compact_buffer_tokens:
            raise ValueError("context warning buffer must be larger than compact buffer")
        if effective <= self.context_warning_buffer_tokens:
            raise ValueError("context buffers leave no usable warning threshold")
        if self.session_memory_max_recent_tokens < self.session_memory_min_recent_tokens:
            raise ValueError("session memory max recent tokens must cover the minimum")
        return self

    @classmethod
    def from_env_file(cls, path: Path) -> ModelSettings:
        values = read_env_file(path)
        provider = values.get("MODEL_PROVIDER_TEXT", "mimo").strip().lower()
        if provider != "mimo":
            raise ConfigurationError(
                f"Unsupported MODEL_PROVIDER_TEXT={provider!r}; current adapter supports 'mimo'"
            )

        model = values.get("MODEL_PROVIDER_TEXT_MODEL") or values.get("MIMO_TEXT_MODEL")
        api_key = values.get("MIMO_API_KEY")
        base_url = values.get("MIMO_BASE_URL")
        missing = [
            name
            for name, value in {
                "MODEL_PROVIDER_TEXT_MODEL or MIMO_TEXT_MODEL": model,
                "MIMO_API_KEY": api_key,
                "MIMO_BASE_URL": base_url,
            }.items()
            if not value
        ]
        if missing:
            raise ConfigurationError(f"Missing model configuration: {', '.join(missing)}")

        integer_values: dict[str, int] = {}
        for environment_name, field_name in {
            "MODEL_CONTEXT_WINDOW_TOKENS": "context_window_tokens",
            "MODEL_SUMMARY_MAX_OUTPUT_TOKENS": "summary_max_output_tokens",
            "CONTEXT_COMPACT_BUFFER_TOKENS": "context_compact_buffer_tokens",
            "CONTEXT_WARNING_BUFFER_TOKENS": "context_warning_buffer_tokens",
            "SESSION_MEMORY_MIN_RECENT_TOKENS": "session_memory_min_recent_tokens",
            "SESSION_MEMORY_MAX_RECENT_TOKENS": "session_memory_max_recent_tokens",
        }.items():
            raw_value = values.get(environment_name)
            if raw_value is None:
                continue
            try:
                integer_values[field_name] = int(raw_value)
            except ValueError as exc:
                raise ConfigurationError(f"{environment_name} must be an integer") from exc
        auto_compact = values.get("AUTO_COMPACT", "true").strip().lower()
        if auto_compact not in {"true", "false"}:
            raise ConfigurationError("AUTO_COMPACT must be true or false")
        auto_resume = values.get("AUTO_RESUME", "safe").strip().lower()
        if auto_resume not in {"safe", "ask", "disabled"}:
            raise ConfigurationError("AUTO_RESUME must be safe, ask, or disabled")
        return cls(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            auto_compact=auto_compact == "true",
            auto_resume=auto_resume,
            **integer_values,
        )
