from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from uuid import uuid4

from aicoding_agent.adapters.ports import ArtifactEntry, BuildReceipt, StageExecutionError
from aicoding_agent.domain.models import BuildCommand, TaskContract


class BuildError(StageExecutionError):
    pass


_SECRET_PATTERN = re.compile(
    r"(?i)(authorization|api[_-]?key|access[_-]?token|password)(\s*[:=]\s*)([^\s,;]+)"
)


def _redact(value: str) -> str:
    return _SECRET_PATTERN.sub(r"\1\2<redacted>", value)


def _safe_process_environment() -> dict[str, str]:
    allowed = {
        "COMSPEC",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update({"CI": "1", "NO_COLOR": "1"})
    return environment


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


class ControlledArtifactBuilder:
    """Run contract-frozen build commands and create a content-addressed manifest."""

    def __init__(self, evidence_root: Path, *, max_log_chars: int = 100_000) -> None:
        self.evidence_root = evidence_root.resolve()
        self.max_log_chars = max_log_chars

    async def build(
        self,
        workspace: Path,
        contract: TaskContract,
        *,
        change_hash: str,
        idempotency_key: str,
    ) -> BuildReceipt:
        root = workspace.resolve()
        if not root.is_dir():
            raise BuildError(f"Build workspace does not exist: {root}")
        if not contract.build_commands:
            raise BuildError("At least one build command is required")
        if not contract.artifact_paths:
            raise BuildError("At least one artifact path is required")

        build_id = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
        build_evidence = self.evidence_root / build_id
        receipt_path = build_evidence / "build-receipt.json"
        if receipt_path.is_file():
            try:
                return BuildReceipt.model_validate_json(receipt_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise BuildError("Existing build receipt is invalid") from exc

        evidence_refs: list[str] = []
        for index, command in enumerate(contract.build_commands, start=1):
            log_path = build_evidence / f"command-{index:02d}-{command.id}.log"
            try:
                output = await self._run_command(root, command)
            except BuildError as exc:
                _atomic_json(
                    log_path,
                    {
                        "id": command.id,
                        "program": command.program,
                        "argument_count": len(command.args),
                        "cwd": command.cwd,
                        "error": _redact(str(exc)),
                    },
                )
                raise BuildError(
                    f"{exc}; evidence={log_path}", evidence_refs=[str(log_path)]
                ) from exc
            _atomic_json(
                log_path,
                {
                    "id": command.id,
                    "program": command.program,
                    "argument_count": len(command.args),
                    "cwd": command.cwd,
                    "exit_code": output[0],
                    "output": output[1],
                },
            )
            evidence_refs.append(str(log_path))
            if output[0] != 0:
                raise BuildError(
                    f"Build command {command.id!r} failed with exit code {output[0]}; "
                    f"evidence={log_path}",
                    evidence_refs=[str(log_path)],
                )

        try:
            artifacts = self._collect_artifacts(root, contract.artifact_paths)
        except BuildError as exc:
            raise BuildError(
                f"{exc}; evidence={build_evidence}",
                evidence_refs=[*evidence_refs, str(build_evidence)],
            ) from exc
        manifest_payload = {
            "schema_version": "1",
            "change_hash": change_hash,
            "artifacts": [item.model_dump(mode="json") for item in artifacts],
        }
        canonical = json.dumps(
            manifest_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        artifact_hash = hashlib.sha256(canonical.encode()).hexdigest()
        manifest_path = build_evidence / "artifact-manifest.json"
        _atomic_json(manifest_path, manifest_payload)
        receipt = BuildReceipt(
            build_id=build_id,
            artifact_hash=artifact_hash,
            artifact_root=str(root),
            manifest_ref=str(manifest_path),
            artifacts=artifacts,
            command_evidence_refs=evidence_refs,
        )
        _atomic_json(receipt_path, receipt.model_dump(mode="json"))
        return receipt

    async def _run_command(self, workspace: Path, command: BuildCommand) -> tuple[int, str]:
        cwd = (workspace / command.cwd).resolve()
        if workspace != cwd and workspace not in cwd.parents:
            raise BuildError(f"Build cwd escapes the workspace: {command.cwd}")
        if not cwd.is_dir():
            raise BuildError(f"Build cwd does not exist: {command.cwd}")
        try:
            process = await asyncio.create_subprocess_exec(
                command.program,
                *command.args,
                cwd=str(cwd),
                env=_safe_process_environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise BuildError(f"Could not start build executable: {command.program}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=command.timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise BuildError(f"Build command {command.id!r} timed out") from exc
        combined = "\n".join(
            value.decode("utf-8", errors="replace") for value in (stdout, stderr) if value
        ).strip()
        redacted = _redact(combined)
        if len(redacted) > self.max_log_chars:
            half = self.max_log_chars // 2
            redacted = f"{redacted[:half]}\n... <output truncated> ...\n{redacted[-half:]}"
        return process.returncode or 0, redacted

    @staticmethod
    def _collect_artifacts(workspace: Path, patterns: list[str]) -> list[ArtifactEntry]:
        files: dict[str, Path] = {}
        for pattern in patterns:
            for candidate in workspace.glob(pattern):
                if candidate.is_dir():
                    candidates = candidate.rglob("*")
                else:
                    candidates = (candidate,)
                for path in candidates:
                    if not path.is_file() or path.is_symlink():
                        continue
                    resolved = path.resolve()
                    if workspace != resolved and workspace not in resolved.parents:
                        raise BuildError(f"Artifact escapes the workspace: {path}")
                    relative = resolved.relative_to(workspace).as_posix()
                    files[relative] = resolved
        if not files:
            raise BuildError("Configured artifact paths did not produce any files")
        return [
            ArtifactEntry(
                path=relative,
                size=path.stat().st_size,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for relative, path in sorted(files.items())
        ]
