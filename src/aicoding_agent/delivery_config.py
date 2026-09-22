from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from aicoding_agent.adapters.deployment import SftpNfsDeploymentSettings
from aicoding_agent.adapters.playwright import PlaywrightSettings


class DeliveryConfigurationError(ValueError):
    pass


class PhaseOneDeliveryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    runtime_root: Path
    deployment: SftpNfsDeploymentSettings
    browser: PlaywrightSettings

    @classmethod
    def from_json_file(cls, path: Path) -> PhaseOneDeliveryConfig:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DeliveryConfigurationError(f"Could not read delivery config: {exc}") from exc
        if not isinstance(payload, dict):
            raise DeliveryConfigurationError("Delivery config must contain a JSON object")
        cls._resolve_paths(payload, path.resolve().parent)
        try:
            return cls.model_validate(payload)
        except ValidationError as exc:
            raise DeliveryConfigurationError(f"Invalid delivery config: {exc}") from exc

    @staticmethod
    def _resolve_paths(payload: dict[str, Any], base: Path) -> None:
        runtime = payload.get("runtime_root")
        if isinstance(runtime, str) and not Path(runtime).is_absolute():
            payload["runtime_root"] = str((base / runtime).resolve())
        deployment = payload.get("deployment")
        if not isinstance(deployment, dict):
            return
        for endpoint_name in ("storage", "device"):
            endpoint = deployment.get(endpoint_name)
            if not isinstance(endpoint, dict):
                continue
            if "password" in endpoint:
                raise DeliveryConfigurationError(
                    f"{endpoint_name}.password must not be stored in JSON; use password_env"
                )
            for field in ("known_hosts", "private_key"):
                value = endpoint.get(field)
                if isinstance(value, str) and not Path(value).is_absolute():
                    endpoint[field] = str((base / value).resolve())
