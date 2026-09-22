from pathlib import Path

import pytest

from aicoding_agent.adapters.build import BuildError, ControlledArtifactBuilder
from aicoding_agent.domain.models import BuildCommand, TaskContract


def contract(*, command: str = "write") -> TaskContract:
    code = (
        "from pathlib import Path; "
        "Path('dist').mkdir(exist_ok=True); "
        "Path('dist/index.html').write_text('ready', encoding='utf-8'); "
        "print('api_' + 'key=should-not-leak')"
    )
    if command == "fail":
        code = "raise SystemExit(7)"
    return TaskContract(
        task_id="build-test",
        objective="Build a frozen frontend artifact",
        acceptance_criteria=["The artifact has a deterministic manifest"],
        build_commands=[
            BuildCommand(id="build", program="python", args=["-c", code], timeout_seconds=30)
        ],
        artifact_paths=["dist"],
    )


def test_build_contract_rejects_command_line_secrets() -> None:
    with pytest.raises(ValueError, match="protected host facility"):
        BuildCommand(id="unsafe", program="npm", args=["--token", "secret-value"])


@pytest.mark.asyncio
async def test_builder_runs_exact_command_and_creates_redacted_manifest(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    builder = ControlledArtifactBuilder(evidence)

    first = await builder.build(
        tmp_path,
        contract(),
        change_hash="a" * 64,
        idempotency_key="run-1:build",
    )
    second = await builder.build(
        tmp_path,
        contract(),
        change_hash="a" * 64,
        idempotency_key="run-1:build",
    )

    assert first == second
    assert first.artifacts[0].path == "dist/index.html"
    assert len(first.artifact_hash) == 64
    assert Path(first.manifest_ref).is_file()
    log = Path(first.command_evidence_refs[0]).read_text(encoding="utf-8")
    assert "should-not-leak" not in log
    assert "<redacted>" in log


@pytest.mark.asyncio
async def test_builder_fails_closed_when_a_command_fails(tmp_path: Path) -> None:
    builder = ControlledArtifactBuilder(tmp_path / "evidence")

    with pytest.raises(BuildError, match="exit code 7"):
        await builder.build(
            tmp_path,
            contract(command="fail"),
            change_hash="a" * 64,
            idempotency_key="run-2:build",
        )
