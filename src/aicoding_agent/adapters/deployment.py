from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shlex
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from aicoding_agent.adapters.ports import (
    BuildReceipt,
    DeploymentReceipt,
    RollbackReceipt,
    StageExecutionError,
)
from aicoding_agent.domain.models import TaskContract


class DeploymentError(StageExecutionError):
    pass


_SECRET_PATTERN = re.compile(
    r"(?i)(authorization|api[_-]?key|access[_-]?token|password)(\s*[:=]\s*)([^\s,;]+)"
)


def _redact(value: str) -> str:
    return _SECRET_PATTERN.sub(r"\1\2<redacted>", value)


class DeploymentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SshEndpoint(DeploymentModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128)
    known_hosts: Path
    private_key: Path | None = None
    password: SecretStr | None = None
    password_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{1,127}$")
    connect_timeout_seconds: float = Field(default=30, gt=0, le=300)

    @model_validator(mode="after")
    def credentials_and_known_hosts_are_explicit(self) -> SshEndpoint:
        if not self.known_hosts.is_file():
            raise ValueError("known_hosts must be an existing file")
        if self.private_key is not None and not self.private_key.is_file():
            raise ValueError("private_key must be an existing file")
        if self.private_key is None and self.password is None and self.password_env is None:
            raise ValueError("private_key, password, or password_env is required")
        return self


class RemoteCommandTemplate(DeploymentModel):
    program: str = Field(min_length=1, max_length=500)
    args: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("program")
    @classmethod
    def command_must_be_plain(cls, value: str) -> str:
        if any(character in value for character in "\r\n\0;&|`$<>"):
            raise ValueError("remote command program contains shell syntax")
        return value

    @field_validator("args")
    @classmethod
    def args_must_be_plain(cls, values: list[str]) -> list[str]:
        allowed = {"{candidate}", "{previous}", "{deployment_id}", "{artifact_hash}"}
        for value in values:
            if any(character in value for character in "\r\n\0"):
                raise ValueError("remote command argument contains a control character")
            fields = {part for part in allowed if part in value}
            stripped = value
            for field in fields:
                stripped = stripped.replace(field, "")
            if "{" in stripped or "}" in stripped:
                raise ValueError("remote command uses an unsupported placeholder")
        return values

    def render(self, values: dict[str, str]) -> str:
        rendered = [self.program, *(value.format_map(values) for value in self.args)]
        return shlex.join(rendered)


class DeviceCommandTemplates(DeploymentModel):
    current_release: RemoteCommandTemplate
    activate: RemoteCommandTemplate
    health_check: RemoteCommandTemplate
    rollback: RemoteCommandTemplate


class DeviceIsolationStatus(DeploymentModel):
    device_id: str
    isolated: bool = False
    reason: str = ""
    deployment_id: str | None = None
    approved_by: str | None = None
    comment: str = ""
    updated_at: datetime


class SftpNfsDeploymentSettings(DeploymentModel):
    storage: SshEndpoint
    device: SshEndpoint
    remote_root: str
    public_base_url: str
    public_url_template: str | None = None
    commands: DeviceCommandTemplates

    @field_validator("remote_root")
    @classmethod
    def remote_root_must_be_absolute(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts or value == "/":
            raise ValueError("remote_root must be a specific absolute POSIX path")
        return value.rstrip("/")

    @field_validator("public_base_url")
    @classmethod
    def base_url_must_be_http(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("public_base_url must use HTTP or HTTPS")
        return value.rstrip("/")

    @field_validator("public_url_template")
    @classmethod
    def url_template_is_bounded(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("public_url_template must use HTTP or HTTPS")
        stripped = value.replace("{deployment_id}", "").replace("{artifact_hash}", "")
        if "{" in stripped or "}" in stripped:
            raise ValueError("public_url_template uses an unsupported placeholder")
        return value


class RemoteTransport(Protocol):
    async def stage(
        self,
        endpoint: SshEndpoint,
        files: list[tuple[Path, str, str]],
        *,
        incoming_dir: str,
        release_dir: str,
    ) -> None: ...

    async def run(self, endpoint: SshEndpoint, command: str) -> str: ...


class AsyncSshTransport:
    """AsyncSSH transport with mandatory host-key verification."""

    @staticmethod
    def _connection_arguments(endpoint: SshEndpoint) -> dict[str, object]:
        arguments: dict[str, object] = {
            "host": endpoint.host,
            "port": endpoint.port,
            "username": endpoint.username,
            "known_hosts": str(endpoint.known_hosts),
            "connect_timeout": endpoint.connect_timeout_seconds,
        }
        if endpoint.private_key is not None:
            arguments["client_keys"] = [str(endpoint.private_key)]
        if endpoint.password is not None:
            arguments["password"] = endpoint.password.get_secret_value()
        elif endpoint.password_env is not None:
            password = os.environ.get(endpoint.password_env)
            if not password:
                raise DeploymentError(
                    f"Required deployment credential is not set: {endpoint.password_env}"
                )
            arguments["password"] = password
        return arguments

    async def stage(
        self,
        endpoint: SshEndpoint,
        files: list[tuple[Path, str, str]],
        *,
        incoming_dir: str,
        release_dir: str,
    ) -> None:
        try:
            import asyncssh
        except ImportError as exc:
            raise DeploymentError("Install the 'delivery' extra to use AsyncSSH") from exc
        try:
            async with asyncssh.connect(**self._connection_arguments(endpoint)) as connection:
                async with connection.start_sftp_client() as sftp:
                    try:
                        await sftp.stat(release_dir)
                        for _, relative, expected_hash in files:
                            remote_path = posixpath.join(release_dir, relative)
                            async with sftp.open(remote_path, "rb") as remote_file:
                                remote_hash = hashlib.sha256(await remote_file.read()).hexdigest()
                            if remote_hash != expected_hash:
                                raise DeploymentError(
                                    f"Existing remote artifact hash mismatch: {relative}"
                                )
                        return
                    except (OSError, asyncssh.SFTPError):
                        pass
                    await sftp.makedirs(incoming_dir, exist_ok=True)
                    for local_path, relative, expected_hash in files:
                        remote_path = posixpath.join(incoming_dir, relative)
                        await sftp.makedirs(posixpath.dirname(remote_path), exist_ok=True)
                        await sftp.put(str(local_path), remote_path)
                        async with sftp.open(remote_path, "rb") as remote_file:
                            remote_hash = hashlib.sha256(await remote_file.read()).hexdigest()
                        if remote_hash != expected_hash:
                            raise DeploymentError(f"Remote artifact hash mismatch: {relative}")
                    await sftp.makedirs(posixpath.dirname(release_dir), exist_ok=True)
                    await sftp.rename(incoming_dir, release_dir)
        except DeploymentError:
            raise
        except Exception as exc:
            raise DeploymentError(f"SFTP staging failed: {type(exc).__name__}: {exc}") from exc

    async def run(self, endpoint: SshEndpoint, command: str) -> str:
        try:
            import asyncssh
        except ImportError as exc:
            raise DeploymentError("Install the 'delivery' extra to use AsyncSSH") from exc
        try:
            async with asyncssh.connect(**self._connection_arguments(endpoint)) as connection:
                result = await connection.run(command, check=True, timeout=300)
            return str(result.stdout).strip()
        except Exception as exc:
            raise DeploymentError(f"Device command failed: {type(exc).__name__}: {exc}") from exc


class SftpNfsTestEnvironmentPublisher:
    def __init__(
        self,
        settings: SftpNfsDeploymentSettings,
        evidence_root: Path,
        *,
        transport: RemoteTransport | None = None,
    ) -> None:
        self.settings = settings
        self.evidence_root = evidence_root.resolve()
        self.transport = transport or AsyncSshTransport()
        identity = (
            f"{settings.device.username}@{settings.device.host}:{settings.device.port}"
        )
        self.device_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
        self.device_state_dir = self.evidence_root / "devices" / self.device_id

    async def publish(
        self,
        workspace: Path,
        contract: TaskContract,
        *,
        change_hash: str,
        build_receipt: BuildReceipt | None = None,
        idempotency_key: str,
    ) -> DeploymentReceipt:
        del contract, change_hash
        if build_receipt is None:
            raise DeploymentError("Deployment requires a frozen build receipt")
        self._require_device_available()
        deployment_id = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
        evidence_dir = self.evidence_root / deployment_id
        receipt_path = evidence_dir / "deployment-receipt.json"
        if receipt_path.is_file():
            try:
                return DeploymentReceipt.model_validate_json(
                    receipt_path.read_text(encoding="utf-8")
                )
            except Exception as exc:
                raise DeploymentError("Existing deployment receipt is invalid") from exc

        files = self._verified_local_files(workspace, build_receipt)
        remote_root = self.settings.remote_root
        incoming = posixpath.join(remote_root, ".incoming", deployment_id)
        release = posixpath.join(remote_root, "releases", build_receipt.artifact_hash)
        values = {
            "candidate": release,
            "previous": "",
            "deployment_id": deployment_id,
            "artifact_hash": build_receipt.artifact_hash,
        }
        state_path = evidence_dir / "deployment-state.json"
        previous = ""
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            previous = str(state.get("previous_location", ""))
        if not previous:
            previous = (
                await self.transport.run(
                    self.settings.device,
                    self.settings.commands.current_release.render(values),
                )
            ).strip()
            if not previous:
                raise DeploymentError("Device did not report a previous release")
            previous_path = PurePosixPath(previous)
            releases_root = PurePosixPath(remote_root) / "releases"
            if (
                not previous_path.is_absolute()
                or ".." in previous_path.parts
                or previous_path == releases_root
                or releases_root not in previous_path.parents
            ):
                raise DeploymentError("Device reported an invalid previous release")
            self._atomic_json(
                state_path,
                {"status": "preparing", "previous_location": previous, "candidate": release},
            )
        values["previous"] = previous

        activated = False
        try:
            await self.transport.stage(
                self.settings.storage,
                files,
                incoming_dir=incoming,
                release_dir=release,
            )
            activated = True
            await self.transport.run(
                self.settings.device,
                self.settings.commands.activate.render(values),
            )
            health_output = await self.transport.run(
                self.settings.device,
                self.settings.commands.health_check.render(values),
            )
        except Exception as exc:
            rollback_status = "not_requested"
            failure_refs = [str(state_path)] if state_path.is_file() else []
            if activated:
                try:
                    rollback = await self._automatic_restore(
                        deployment_id, previous, values
                    )
                    rollback_status = "succeeded"
                    failure_refs.extend(rollback.evidence_refs)
                except DeploymentError as rollback_error:
                    rollback_status = "failed"
                    failure_refs.extend(rollback_error.evidence_refs)
            failure_path = evidence_dir / "deployment-failure.json"
            self._atomic_json(
                failure_path,
                {
                    "deployment_id": deployment_id,
                    "error": _redact(f"{type(exc).__name__}: {exc}"[:5_000]),
                    "rollback_status": rollback_status,
                },
            )
            failure_refs.append(str(failure_path))
            message = f"{type(exc).__name__}: {exc}"
            if rollback_status == "failed":
                message += "; automatic rollback failed and the device was isolated"
            raise DeploymentError(
                message,
                evidence_refs=failure_refs,
                rollback_status=rollback_status,
            ) from exc

        evidence_path = evidence_dir / "deployment.json"
        self._atomic_json(
            evidence_path,
            {
                "deployment_id": deployment_id,
                "artifact_hash": build_receipt.artifact_hash,
                "candidate": release,
                "previous": previous,
                "health_output": _redact(health_output[:10_000]),
            },
        )
        receipt = DeploymentReceipt(
            deployment_id=deployment_id,
            artifact_hash=build_receipt.artifact_hash,
            location=self._deployment_url(deployment_id, build_receipt.artifact_hash),
            previous_location=previous,
            manifest_ref=build_receipt.manifest_ref,
            evidence_refs=[str(evidence_path)],
        )
        self._atomic_json(receipt_path, receipt.model_dump(mode="json"))
        self._atomic_json(
            state_path,
            {"status": "active", "previous_location": previous, "candidate": release},
        )
        return receipt

    async def rollback(
        self,
        receipt: DeploymentReceipt,
        *,
        idempotency_key: str,
    ) -> RollbackReceipt:
        rollback_id = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
        marker = self.evidence_root / receipt.deployment_id / f"rollback-{rollback_id}.json"
        if marker.is_file():
            return RollbackReceipt.model_validate_json(marker.read_text(encoding="utf-8"))
        previous = receipt.previous_location or ""
        if not previous:
            isolation_refs = await self._record_isolation(
                receipt.deployment_id, "missing previous release"
            )
            raise DeploymentError(
                "Cannot rollback without a previous release",
                evidence_refs=isolation_refs,
                rollback_status="failed",
            )
        values = {
            "candidate": previous,
            "previous": previous,
            "deployment_id": receipt.deployment_id,
            "artifact_hash": receipt.artifact_hash,
        }
        try:
            await self.transport.run(
                self.settings.device,
                self.settings.commands.rollback.render(values),
            )
            await self.transport.run(
                self.settings.device,
                self.settings.commands.health_check.render(values),
            )
        except Exception as exc:
            isolation_refs = await self._record_isolation(
                receipt.deployment_id, _redact(str(exc))
            )
            failure_path = marker.with_name(f"rollback-{rollback_id}-failure.json")
            self._atomic_json(
                failure_path,
                {
                    "deployment_id": receipt.deployment_id,
                    "error": _redact(f"{type(exc).__name__}: {exc}"[:5_000]),
                    "isolated": True,
                },
            )
            raise DeploymentError(
                f"Rollback failed and the device was isolated: {type(exc).__name__}: {exc}",
                evidence_refs=[*isolation_refs, str(failure_path)],
                rollback_status="failed",
            ) from exc
        rollback = RollbackReceipt(
            rollback_id=rollback_id,
            deployment_id=receipt.deployment_id,
            ok=True,
            evidence_refs=[str(marker)],
        )
        self._atomic_json(marker, rollback.model_dump(mode="json"))
        return rollback

    def _verified_local_files(
        self, workspace: Path, receipt: BuildReceipt
    ) -> list[tuple[Path, str, str]]:
        root = workspace.resolve()
        if Path(receipt.artifact_root).resolve() != root:
            raise DeploymentError("Build receipt belongs to a different workspace")
        files: list[tuple[Path, str, str]] = []
        for artifact in receipt.artifacts:
            path = (root / artifact.path).resolve()
            if root != path and root not in path.parents:
                raise DeploymentError(f"Artifact path escapes workspace: {artifact.path}")
            if not path.is_file() or path.is_symlink():
                raise DeploymentError(f"Artifact is unavailable: {artifact.path}")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != artifact.sha256:
                raise DeploymentError(f"Artifact changed after build: {artifact.path}")
            files.append((path, artifact.path, artifact.sha256))
        return files

    async def _automatic_restore(
        self, deployment_id: str, previous: str, values: dict[str, str]
    ) -> RollbackReceipt:
        rollback_id = f"automatic-{deployment_id}"
        marker = self.evidence_root / deployment_id / "automatic-rollback.json"
        rollback_values = {**values, "candidate": previous, "previous": previous}
        try:
            await self.transport.run(
                self.settings.device,
                self.settings.commands.rollback.render(rollback_values),
            )
            await self.transport.run(
                self.settings.device,
                self.settings.commands.health_check.render(rollback_values),
            )
        except Exception as exc:
            isolation_refs = await self._record_isolation(deployment_id, str(exc))
            raise DeploymentError(
                "Activation failed and automatic rollback also failed",
                evidence_refs=isolation_refs,
                rollback_status="failed",
            ) from exc
        rollback = RollbackReceipt(
            rollback_id=rollback_id,
            deployment_id=deployment_id,
            ok=True,
            evidence_refs=[str(marker)],
        )
        self._atomic_json(marker, rollback.model_dump(mode="json"))
        return rollback

    async def _record_isolation(self, deployment_id: str, reason: str) -> list[str]:
        now = datetime.now(UTC)
        status = DeviceIsolationStatus(
            device_id=self.device_id,
            isolated=True,
            reason=_redact(reason[:5_000]),
            deployment_id=deployment_id,
            updated_at=now,
        )
        stable_path = self.device_state_dir / "isolation-state.json"
        event_path = self.device_state_dir / "isolation-events.jsonl"
        self._append_jsonl(event_path, status.model_dump(mode="json"))
        self._atomic_json(stable_path, status.model_dump(mode="json"))
        deployment_path = self.evidence_root / deployment_id / "DEVICE_ISOLATED.json"
        self._atomic_json(
            deployment_path,
            status.model_dump(mode="json"),
        )
        return [str(stable_path), str(event_path), str(deployment_path)]

    def isolation_status(self) -> DeviceIsolationStatus:
        path = self.device_state_dir / "isolation-state.json"
        if not path.is_file():
            return DeviceIsolationStatus(
                device_id=self.device_id,
                isolated=False,
                updated_at=datetime.now(UTC),
            )
        try:
            return DeviceIsolationStatus.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise DeploymentError(
                "Device isolation state is invalid; deployment is blocked",
                evidence_refs=[str(path)],
            ) from exc

    async def clear_isolation(
        self,
        *,
        approved_by: str,
        comment: str,
    ) -> DeviceIsolationStatus:
        current = self.isolation_status()
        if not current.isolated:
            return current
        approved_by = approved_by.strip()
        comment = comment.strip()
        if not approved_by or len(approved_by) > 256:
            raise DeploymentError("A bounded approver identity is required")
        if not comment or len(comment) > 5_000:
            raise DeploymentError("A bounded recovery comment is required")
        values = {
            "candidate": "",
            "previous": "",
            "deployment_id": "manual-recovery",
            "artifact_hash": "0" * 64,
        }
        active_release = (
            await self.transport.run(
                self.settings.device,
                self.settings.commands.current_release.render(values),
            )
        ).strip()
        self._validate_release_path(active_release)
        values.update(candidate=active_release, previous=active_release)
        await self.transport.run(
            self.settings.device,
            self.settings.commands.health_check.render(values),
        )
        cleared = DeviceIsolationStatus(
            device_id=self.device_id,
            isolated=False,
            reason="manual recovery approved after a successful health check",
            deployment_id=current.deployment_id,
            approved_by=approved_by,
            comment=_redact(comment),
            updated_at=datetime.now(UTC),
        )
        event_path = self.device_state_dir / "isolation-events.jsonl"
        self._append_jsonl(event_path, cleared.model_dump(mode="json"))
        self._atomic_json(
            self.device_state_dir / "isolation-state.json",
            cleared.model_dump(mode="json"),
        )
        return cleared

    def _require_device_available(self) -> None:
        status = self.isolation_status()
        if status.isolated:
            state_path = self.device_state_dir / "isolation-state.json"
            raise DeploymentError(
                "Test device is isolated after a rollback failure; manual recovery is required",
                evidence_refs=[str(state_path)],
            )

    def _validate_release_path(self, value: str) -> PurePosixPath:
        path = PurePosixPath(value)
        releases_root = PurePosixPath(self.settings.remote_root) / "releases"
        if (
            not path.is_absolute()
            or ".." in path.parts
            or path == releases_root
            or releases_root not in path.parents
        ):
            raise DeploymentError("Device reported an invalid release path")
        return path

    def _deployment_url(self, deployment_id: str, artifact_hash: str) -> str:
        template = self.settings.public_url_template
        if template is None:
            return f"{self.settings.public_base_url}/{deployment_id}"
        return template.format_map(
            {"deployment_id": deployment_id, "artifact_hash": artifact_hash}
        )

    @staticmethod
    def _atomic_json(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _append_jsonl(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
