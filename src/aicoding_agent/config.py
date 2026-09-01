from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field, SecretStr, field_validator


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

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("base_url must be an HTTPS URL without user information")
        return normalized

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

        return cls(provider=provider, model=model, api_key=api_key, base_url=base_url)
