import json
from pathlib import Path

import pytest

from aicoding_agent.delivery_config import DeliveryConfigurationError, PhaseOneDeliveryConfig


def test_phase_one_config_resolves_paths_and_keeps_password_out_of_file(tmp_path: Path) -> None:
    (tmp_path / "known_hosts").write_text("host key", encoding="utf-8")
    payload = {
        "schema_version": "1",
        "runtime_root": "runtime",
        "deployment": {
            "storage": {
                "host": "storage.internal",
                "username": "delivery",
                "known_hosts": "known_hosts",
                "password_env": "TEST_STORAGE_PASSWORD",
            },
            "device": {
                "host": "device.internal",
                "username": "delivery",
                "known_hosts": "known_hosts",
                "password_env": "TEST_DEVICE_PASSWORD",
            },
            "remote_root": "/srv/frontend",
            "public_base_url": "https://test.internal/builds",
            "commands": {
                "current_release": {"program": "frontend-current"},
                "activate": {"program": "frontend-activate", "args": ["{candidate}"]},
                "health_check": {"program": "frontend-health"},
                "rollback": {"program": "frontend-restore", "args": ["{previous}"]},
            },
        },
        "browser": {"allowed_hosts": ["test.internal"]},
    }
    config_path = tmp_path / "delivery.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    config = PhaseOneDeliveryConfig.from_json_file(config_path)

    assert config.runtime_root == (tmp_path / "runtime").resolve()
    assert config.deployment.storage.known_hosts == (tmp_path / "known_hosts").resolve()
    assert config.deployment.storage.password is None
    assert config.deployment.storage.password_env == "TEST_STORAGE_PASSWORD"


def test_phase_one_config_rejects_plaintext_password(tmp_path: Path) -> None:
    config_path = tmp_path / "delivery.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_root": "runtime",
                "deployment": {
                    "storage": {"password": "must-not-be-here"},
                    "device": {},
                },
                "browser": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeliveryConfigurationError, match="password_env"):
        PhaseOneDeliveryConfig.from_json_file(config_path)
