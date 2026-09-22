import asyncio
from pathlib import Path

import pytest

from aicoding_agent.adapters.git import (
    GitCandidateIntegrator,
    GitIntegrationError,
    LocalGitWorktreeManager,
)
from aicoding_agent.domain.models import TaskContract


async def git(workspace: Path, *args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode(errors="replace")
    return stdout.decode(errors="replace").strip()


async def create_repository(path: Path) -> str:
    path.mkdir()
    await git(path, "init", "-b", "main")
    await git(path, "config", "user.name", "Test User")
    await git(path, "config", "user.email", "test@example.invalid")
    (path / "app.txt").write_text("baseline\n", encoding="utf-8")
    await git(path, "add", "app.txt")
    await git(path, "commit", "-m", "baseline")
    return await git(path, "rev-parse", "HEAD")


def contract(task_id: str, revision: str) -> TaskContract:
    return TaskContract(
        task_id=task_id,
        objective=f"Candidate {task_id}",
        acceptance_criteria=["Candidate is composed locally"],
        base_revision=revision,
    )


@pytest.mark.asyncio
async def test_candidate_integrator_composes_disjoint_changes_without_touching_source(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    revision = await create_repository(repository)
    manager = LocalGitWorktreeManager(tmp_path / "worktrees")
    integration = await manager.prepare(
        repository,
        contract("integration", revision),
        run_id="integration",
    )
    first = await manager.prepare(
        repository,
        contract("first", revision),
        run_id="first",
    )
    second = await manager.prepare(
        repository,
        contract("second", revision),
        run_id="second",
    )
    first_path = Path(first.path)
    second_path = Path(second.path)
    integration_path = Path(integration.path)
    (first_path / "app.txt").write_text("first\n", encoding="utf-8")
    (second_path / "new.txt").write_text("second\n", encoding="utf-8")
    integrator = GitCandidateIntegrator(safe_hooks_dir=tmp_path / "safe-hooks")

    first_revision = await integrator.integrate(
        first_path,
        integration_path,
        task_id="first",
        expected_candidate_base=revision,
    )
    second_revision = await integrator.integrate(
        second_path,
        integration_path,
        task_id="second",
        expected_candidate_base=revision,
    )

    assert first_revision != revision
    assert second_revision != first_revision
    assert (integration_path / "app.txt").read_text(encoding="utf-8") == "first\n"
    assert (integration_path / "new.txt").read_text(encoding="utf-8") == "second\n"
    assert (repository / "app.txt").read_text(encoding="utf-8") == "baseline\n"
    assert await git(repository, "rev-parse", "HEAD") == revision
    assert await git(integration_path, "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_candidate_integrator_conflict_fails_without_partial_integration(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    revision = await create_repository(repository)
    manager = LocalGitWorktreeManager(tmp_path / "worktrees")
    integration = await manager.prepare(
        repository,
        contract("integration", revision),
        run_id="integration",
    )
    first = await manager.prepare(repository, contract("first", revision), run_id="first")
    conflict = await manager.prepare(
        repository,
        contract("conflict", revision),
        run_id="conflict",
    )
    (Path(first.path) / "app.txt").write_text("first\n", encoding="utf-8")
    (Path(conflict.path) / "app.txt").write_text("conflict\n", encoding="utf-8")
    integrator = GitCandidateIntegrator(safe_hooks_dir=tmp_path / "safe-hooks")
    await integrator.integrate(
        Path(first.path),
        Path(integration.path),
        task_id="first",
        expected_candidate_base=revision,
    )

    with pytest.raises(GitIntegrationError, match="patch operation failed"):
        await integrator.integrate(
            Path(conflict.path),
            Path(integration.path),
            task_id="conflict",
            expected_candidate_base=revision,
        )

    assert (Path(integration.path) / "app.txt").read_text(encoding="utf-8") == "first\n"
    assert await git(Path(integration.path), "status", "--porcelain") == ""
