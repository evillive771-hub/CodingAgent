from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aicoding_agent.adapters.git import GitWorkspaceError, LocalGitWorktreeManager
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


def contract(*, base_revision: str | None = None) -> TaskContract:
    return TaskContract(
        task_id="TB:workspace-1",
        objective="Prepare an isolated checkout",
        acceptance_criteria=["The checkout is pinned to the requested commit"],
        base_revision=base_revision,
    )


@pytest.mark.asyncio
async def test_worktree_prepare_is_isolated_pinned_and_idempotent(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    revision = await create_repository(repository)
    (repository / "app.txt").write_text("uncommitted source change\n", encoding="utf-8")
    manager = LocalGitWorktreeManager(tmp_path / "worktrees")
    task_contract = contract(base_revision=revision)

    first = await manager.prepare(
        repository,
        task_contract,
        run_id="run-1",
    )
    second = await manager.prepare(
        repository,
        task_contract,
        run_id="run-1",
    )

    workspace = Path(first.path)
    assert second == first
    assert workspace.parent == (tmp_path / "worktrees").resolve()
    assert (workspace / "app.txt").read_text(encoding="utf-8") == "baseline\n"
    assert await git(workspace, "rev-parse", "HEAD") == revision
    assert await git(workspace, "status", "--porcelain") == ""

    changed_contract = task_contract.model_copy(update={"objective": "A different task contract"})
    with pytest.raises(GitWorkspaceError, match="another task contract or repository"):
        await manager.prepare(repository, changed_contract, run_id="run-1")

    await manager.release(first)
    await manager.release(first)

    assert not workspace.exists()


@pytest.mark.asyncio
async def test_worktree_prepare_rejects_unknown_base_revision(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    await create_repository(repository)
    worktree_root = tmp_path / "worktrees"
    manager = LocalGitWorktreeManager(worktree_root)

    with pytest.raises(GitWorkspaceError, match="Git command failed"):
        await manager.prepare(
            repository,
            contract(base_revision="missing-revision"),
            run_id="invalid-base",
        )

    assert not worktree_root.exists()


@pytest.mark.asyncio
async def test_worktree_prepare_binds_configured_repository_url_to_origin(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    await create_repository(repository)
    await git(repository, "remote", "add", "origin", "https://git.example/team/project.git")
    manager = LocalGitWorktreeManager(tmp_path / "worktrees")

    matching = contract().model_copy(
        update={"repository_url": "https://git.example/team/project"}
    )
    lease = await manager.prepare(repository, matching, run_id="matching-origin")
    await manager.release(lease)

    mismatched = contract().model_copy(
        update={"repository_url": "https://git.example/team/another.git"}
    )
    with pytest.raises(GitWorkspaceError, match="does not match"):
        await manager.prepare(repository, mismatched, run_id="wrong-origin")


@pytest.mark.asyncio
async def test_worktree_release_refuses_tampered_target(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    await create_repository(repository)
    manager = LocalGitWorktreeManager(tmp_path / "worktrees")
    lease = await manager.prepare(repository, contract(), run_id="tamper-test")
    outside = tmp_path / "must-not-remove"
    outside.mkdir()
    tampered = lease.model_copy(update={"path": str(outside)})

    with pytest.raises(GitWorkspaceError, match="outside the configured root"):
        await manager.release(tampered)

    assert outside.is_dir()
    await manager.release(lease)
