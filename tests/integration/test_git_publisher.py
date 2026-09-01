from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aicoding_agent.adapters.git import (
    GitBranchPublisher,
    GitChangeInspector,
    GitPublishError,
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


async def create_repository_with_remote(tmp_path: Path) -> tuple[Path, Path, str]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    await git(remote, "init", "--bare")
    repository = tmp_path / "repository"
    repository.mkdir()
    await git(repository, "init", "-b", "main")
    await git(repository, "config", "user.name", "Test User")
    await git(repository, "config", "user.email", "test@example.invalid")
    (repository / "app.txt").write_text("baseline\n", encoding="utf-8")
    await git(repository, "add", "app.txt")
    await git(repository, "commit", "-m", "baseline")
    baseline = await git(repository, "rev-parse", "HEAD")
    await git(repository, "remote", "add", "origin", str(remote))
    await git(repository, "push", "-u", "origin", "main")
    return repository, remote, baseline


def contract() -> TaskContract:
    return TaskContract(
        task_id="TB:publish-1",
        objective="Publish the approved candidate branch",
        acceptance_criteria=["The remote candidate branch points to the approved commit"],
    )


@pytest.mark.asyncio
async def test_git_publisher_pushes_new_branch_without_moving_workspace_head(
    tmp_path: Path,
) -> None:
    repository, remote, baseline = await create_repository_with_remote(tmp_path)
    (repository / "app.txt").write_text("approved change\n", encoding="utf-8")
    (repository / "new.txt").write_text("new file\n", encoding="utf-8")
    snapshot = await GitChangeInspector().snapshot(repository)
    publisher = GitBranchPublisher(safe_hooks_dir=tmp_path / "safe-hooks")

    first = await publisher.publish(
        repository,
        contract(),
        expected_change_hash=snapshot.change_hash,
        idempotency_key="run-1:git-publish",
    )
    second = await publisher.publish(
        repository,
        contract(),
        expected_change_hash=snapshot.change_hash,
        idempotency_key="run-1:git-publish",
    )

    assert first == second
    assert first.branch is not None
    assert await git(remote, "rev-parse", f"refs/heads/{first.branch}") == first.revision
    assert await git(repository, "rev-parse", "HEAD") == baseline
    assert await git(repository, "show", f"{first.revision}:app.txt") == "approved change"
    assert await git(repository, "show", f"{first.revision}:new.txt") == "new file"


@pytest.mark.asyncio
async def test_git_publisher_refuses_unapproved_workspace_state(tmp_path: Path) -> None:
    repository, remote, _ = await create_repository_with_remote(tmp_path)
    (repository / "app.txt").write_text("not approved\n", encoding="utf-8")
    publisher = GitBranchPublisher(safe_hooks_dir=tmp_path / "safe-hooks")

    with pytest.raises(GitPublishError, match="does not match"):
        await publisher.publish(
            repository,
            contract(),
            expected_change_hash="0" * 64,
            idempotency_key="run-2:git-publish",
        )

    branches = await git(remote, "for-each-ref", "--format=%(refname)", "refs/heads/aicoding")
    assert branches == ""


@pytest.mark.asyncio
async def test_git_publisher_retries_a_pending_push_idempotently(tmp_path: Path) -> None:
    repository, remote, baseline = await create_repository_with_remote(tmp_path)
    (repository / "app.txt").write_text("retryable change\n", encoding="utf-8")
    snapshot = await GitChangeInspector().snapshot(repository)

    class FailFirstPushPublisher(GitBranchPublisher):
        failed = False

        async def _push(self, workspace: Path, revision: str, remote_ref: str) -> None:
            if not self.failed:
                self.failed = True
                raise GitPublishError("simulated transient push failure")
            await super()._push(workspace, revision, remote_ref)

    publisher = FailFirstPushPublisher(safe_hooks_dir=tmp_path / "safe-hooks")
    with pytest.raises(GitPublishError, match="simulated transient"):
        await publisher.publish(
            repository,
            contract(),
            expected_change_hash=snapshot.change_hash,
            idempotency_key="run-3:git-publish",
        )

    receipt = await publisher.publish(
        repository,
        contract(),
        expected_change_hash=snapshot.change_hash,
        idempotency_key="run-3:git-publish",
    )

    assert receipt.branch is not None
    assert await git(remote, "rev-parse", f"refs/heads/{receipt.branch}") == receipt.revision
    assert await git(repository, "rev-parse", f"{receipt.revision}^") == baseline
