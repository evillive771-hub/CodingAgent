import hashlib
from pathlib import Path

import pytest

from aicoding_agent.adapters.deployment import (
    DeploymentError,
    DeviceCommandTemplates,
    RemoteCommandTemplate,
    SftpNfsDeploymentSettings,
    SftpNfsTestEnvironmentPublisher,
    SshEndpoint,
)
from aicoding_agent.adapters.ports import ArtifactEntry, BuildReceipt
from aicoding_agent.domain.models import TaskContract


class FakeRemoteTransport:
    def __init__(self, *, fail_health: bool = False, fail_rollback: bool = False) -> None:
        self.fail_health = fail_health
        self.fail_rollback = fail_rollback
        self.staged: list[tuple[str, str]] = []
        self.commands: list[str] = []

    async def stage(self, endpoint, files, *, incoming_dir, release_dir):
        del endpoint, files
        self.staged.append((incoming_dir, release_dir))

    async def run(self, endpoint, command):
        del endpoint
        self.commands.append(command)
        if command.startswith("current-release"):
            return "/srv/frontend/releases/old"
        if command.startswith("health") and self.fail_health:
            self.fail_health = False
            raise DeploymentError("health failed")
        if command.startswith("restore") and self.fail_rollback:
            raise DeploymentError("rollback failed")
        return "ok"


def settings(tmp_path: Path) -> SftpNfsDeploymentSettings:
    known_hosts = tmp_path / "known_hosts"
    private_key = tmp_path / "key"
    known_hosts.write_text("test key", encoding="utf-8")
    private_key.write_text("test key", encoding="utf-8")
    endpoint = SshEndpoint(
        host="test.internal",
        username="delivery",
        known_hosts=known_hosts,
        private_key=private_key,
    )
    return SftpNfsDeploymentSettings(
        storage=endpoint,
        device=endpoint,
        remote_root="/srv/frontend",
        public_base_url="https://test.internal/builds",
        commands=DeviceCommandTemplates(
            current_release=RemoteCommandTemplate(program="current-release"),
            activate=RemoteCommandTemplate(program="activate-release", args=["{candidate}"]),
            health_check=RemoteCommandTemplate(program="health", args=["{deployment_id}"]),
            rollback=RemoteCommandTemplate(program="restore", args=["{previous}"]),
        ),
    )


def inputs(tmp_path: Path) -> tuple[TaskContract, BuildReceipt]:
    artifact = tmp_path / "dist" / "index.html"
    artifact.parent.mkdir()
    artifact.write_text("ready", encoding="utf-8")
    file_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    receipt = BuildReceipt(
        build_id="build-1",
        artifact_hash="a" * 64,
        artifact_root=str(tmp_path),
        manifest_ref=str(tmp_path / "manifest.json"),
        artifacts=[ArtifactEntry(path="dist/index.html", size=5, sha256=file_hash)],
    )
    contract = TaskContract(
        task_id="deploy-test",
        objective="Deploy to a test device",
        acceptance_criteria=["Health check passes"],
    )
    return contract, receipt


@pytest.mark.asyncio
async def test_sftp_nfs_publisher_stages_activates_and_rolls_back(tmp_path: Path) -> None:
    transport = FakeRemoteTransport()
    publisher = SftpNfsTestEnvironmentPublisher(
        settings(tmp_path), tmp_path / "evidence", transport=transport
    )
    contract, build = inputs(tmp_path)

    receipt = await publisher.publish(
        tmp_path,
        contract,
        change_hash="b" * 64,
        build_receipt=build,
        idempotency_key="deploy-once",
    )
    repeated = await publisher.publish(
        tmp_path,
        contract,
        change_hash="b" * 64,
        build_receipt=build,
        idempotency_key="deploy-once",
    )
    await publisher.rollback(receipt, idempotency_key="rollback-once")

    assert receipt == repeated
    assert receipt.previous_location == "/srv/frontend/releases/old"
    assert len(transport.staged) == 1
    assert any(command.startswith("activate-release") for command in transport.commands)
    assert any(command.startswith("restore") for command in transport.commands)


@pytest.mark.asyncio
async def test_deployment_rejects_artifact_modified_after_build(tmp_path: Path) -> None:
    publisher = SftpNfsTestEnvironmentPublisher(
        settings(tmp_path), tmp_path / "evidence", transport=FakeRemoteTransport()
    )
    contract, build = inputs(tmp_path)
    (tmp_path / "dist" / "index.html").write_text("changed", encoding="utf-8")

    with pytest.raises(DeploymentError, match="changed after build"):
        await publisher.publish(
            tmp_path,
            contract,
            change_hash="b" * 64,
            build_receipt=build,
            idempotency_key="deploy-mutated",
        )


@pytest.mark.asyncio
async def test_failed_health_check_restores_previous_release(tmp_path: Path) -> None:
    transport = FakeRemoteTransport(fail_health=True)
    publisher = SftpNfsTestEnvironmentPublisher(
        settings(tmp_path), tmp_path / "evidence", transport=transport
    )
    contract, build = inputs(tmp_path)

    with pytest.raises(DeploymentError, match="health failed") as error:
        await publisher.publish(
            tmp_path,
            contract,
            change_hash="b" * 64,
            build_receipt=build,
            idempotency_key="deploy-unhealthy",
        )

    assert any(command.startswith("restore") for command in transport.commands)
    assert error.value.rollback_status == "succeeded"
    assert publisher.isolation_status().isolated is False


@pytest.mark.asyncio
async def test_rollback_failure_isolates_device_until_manual_health_check(
    tmp_path: Path,
) -> None:
    transport = FakeRemoteTransport(fail_health=True, fail_rollback=True)
    publisher = SftpNfsTestEnvironmentPublisher(
        settings(tmp_path), tmp_path / "evidence", transport=transport
    )
    contract, build = inputs(tmp_path)

    with pytest.raises(DeploymentError, match="device was isolated") as failure:
        await publisher.publish(
            tmp_path,
            contract,
            change_hash="b" * 64,
            build_receipt=build,
            idempotency_key="deploy-isolated",
        )

    assert failure.value.rollback_status == "failed"
    assert publisher.isolation_status().isolated is True
    command_count = len(transport.commands)
    with pytest.raises(DeploymentError, match="manual recovery"):
        await publisher.publish(
            tmp_path,
            contract,
            change_hash="b" * 64,
            build_receipt=build,
            idempotency_key="deploy-blocked",
        )
    assert len(transport.commands) == command_count

    transport.fail_rollback = False
    recovered = await publisher.clear_isolation(
        approved_by="device-owner",
        comment="Device mount repaired and inspected",
    )

    assert recovered.isolated is False
    assert publisher.isolation_status().isolated is False
    events = (
        tmp_path
        / "evidence"
        / "devices"
        / publisher.device_id
        / "isolation-events.jsonl"
    ).read_text(encoding="utf-8")
    assert events.count("\n") == 2


@pytest.mark.asyncio
async def test_public_url_template_supports_a_fixed_device_url(tmp_path: Path) -> None:
    configured = settings(tmp_path).model_copy(
        update={"public_url_template": "https://test.internal/current"}
    )
    publisher = SftpNfsTestEnvironmentPublisher(
        configured, tmp_path / "evidence", transport=FakeRemoteTransport()
    )
    contract, build = inputs(tmp_path)

    receipt = await publisher.publish(
        tmp_path,
        contract,
        change_hash="b" * 64,
        build_receipt=build,
        idempotency_key="fixed-url",
    )

    assert receipt.location == "https://test.internal/current"
