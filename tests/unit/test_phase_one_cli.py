import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from aicoding_agent.adapters.deployment import (
    DeviceIsolationStatus,
    SftpNfsTestEnvironmentPublisher,
)
from aicoding_agent.cli import app
from aicoding_agent.delivery_config import PhaseOneDeliveryConfig


def _delivery_config(tmp_path: Path) -> Path:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("host key", encoding="utf-8")
    payload = {
        "runtime_root": str(tmp_path / "runtime"),
        "deployment": {
            "storage": {
                "host": "storage.internal",
                "username": "delivery",
                "known_hosts": str(known_hosts),
                "password_env": "TEST_STORAGE_PASSWORD",
            },
            "device": {
                "host": "device.internal",
                "username": "delivery",
                "known_hosts": str(known_hosts),
                "password_env": "TEST_DEVICE_PASSWORD",
            },
            "remote_root": "/srv/frontend",
            "public_base_url": "https://device.internal/builds",
            "public_url_template": "https://device.internal/",
            "commands": {
                "current_release": {"program": "frontend-current"},
                "activate": {"program": "frontend-activate", "args": ["{candidate}"]},
                "health_check": {"program": "frontend-health"},
                "rollback": {"program": "frontend-restore", "args": ["{previous}"]},
            },
        },
        "browser": {"allowed_hosts": ["device.internal"]},
    }
    path = tmp_path / "delivery.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_device_status_command_returns_nonzero_for_isolated_device(tmp_path: Path) -> None:
    config_path = _delivery_config(tmp_path)
    config = PhaseOneDeliveryConfig.from_json_file(config_path)
    publisher = SftpNfsTestEnvironmentPublisher(
        config.deployment,
        config.runtime_root / "evidence" / "deployment",
    )
    state = DeviceIsolationStatus(
        device_id=publisher.device_id,
        isolated=True,
        reason="rollback failed",
        deployment_id="deployment-1",
        updated_at=datetime.now(UTC),
    )
    publisher._atomic_json(
        publisher.device_state_dir / "isolation-state.json",
        state.model_dump(mode="json"),
    )

    result = CliRunner().invoke(
        app,
        ["delivery-device-status", "--delivery-config", str(config_path)],
    )

    assert result.exit_code == 1
    assert '"isolated": true' in result.output
    assert "rollback failed" in result.output


def test_delivery_command_rejects_incomplete_phase_one_contract(tmp_path: Path) -> None:
    task_path = tmp_path / "task.json"
    task_path.write_text(
        json.dumps(
            {
                "task_id": "incomplete-phase-one",
                "objective": "Attempt an incomplete delivery",
                "acceptance_criteria": ["Must be rejected before model or deployment access"],
                "target_environment": "test",
                "repository_url": "https://git.example/frontend/project.git",
            }
        ),
        encoding="utf-8",
    )
    config_path = _delivery_config(tmp_path)
    env_path = tmp_path / "env.txt"
    env_path.write_text("unused=true\n", encoding="utf-8")
    repository = tmp_path / "repository"
    repository.mkdir()

    result = CliRunner().invoke(
        app,
        [
            "deliver-phase-one",
            "--task",
            str(task_path),
            "--repository",
            str(repository),
            "--delivery-config",
            str(config_path),
            "--env-file",
            str(env_path),
        ],
    )

    assert result.exit_code == 2
    assert "verification_commands" in result.output
    assert "build_commands" in result.output
    assert "artifact_paths" in result.output
