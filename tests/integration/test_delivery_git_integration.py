from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aicoding_agent.adapters.fakes import (
    FakeBrowserVerifier,
    FakeTeambitionAdapter,
    FakeTestEnvironmentPublisher,
)
from aicoding_agent.adapters.git import (
    GitBranchPublisher,
    GitChangeInspector,
    LocalGitWorktreeManager,
)
from aicoding_agent.domain.models import AgentResult, AgentStatus, CodingOutcome, TaskContract
from aicoding_agent.persistence.jsonl import AtomicCheckpointStore
from aicoding_agent.workflow.engine import DeliveryWorkflow
from aicoding_agent.workflow.models import ApprovalRecord


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
    repository = tmp_path / "source"
    repository.mkdir()
    await git(repository, "init", "-b", "main")
    await git(repository, "config", "user.name", "Test User")
    await git(repository, "config", "user.email", "test@example.invalid")
    (repository / "app.txt").write_text("baseline\n", encoding="utf-8")
    await git(repository, "add", "app.txt")
    await git(repository, "commit", "-m", "baseline")
    revision = await git(repository, "rev-parse", "HEAD")
    await git(repository, "remote", "add", "origin", str(remote))
    await git(repository, "push", "-u", "origin", "main")
    return repository, remote, revision


class EditingCompletedAgent:
    def __init__(self) -> None:
        self.workspace: Path | None = None

    async def run(
        self,
        contract: TaskContract,
        workspace: Path,
        *,
        run_id: str | None = None,
    ) -> AgentResult:
        self.workspace = workspace
        (workspace / "app.txt").write_text("agent change\n", encoding="utf-8")
        return AgentResult(
            status=AgentStatus.COMPLETED,
            run_id=run_id or "run",
            outcome=CodingOutcome(summary="Updated app.txt", changed_files=["app.txt"]),
        )


@pytest.mark.asyncio
async def test_delivery_workflow_publishes_approved_isolated_change(tmp_path: Path) -> None:
    repository, remote, baseline = await create_repository_with_remote(tmp_path)
    checkpoints = AtomicCheckpointStore(tmp_path / "checkpoints")
    agent = EditingCompletedAgent()
    workflow = DeliveryWorkflow(
        agent=agent,
        workspace_manager=LocalGitWorktreeManager(tmp_path / "worktrees"),
        change_inspector=GitChangeInspector(),
        test_publisher=FakeTestEnvironmentPublisher(),
        browser_verifier=FakeBrowserVerifier(),
        change_publisher=GitBranchPublisher(safe_hooks_dir=tmp_path / "safe-hooks"),
        teambition=FakeTeambitionAdapter(),
        checkpoints=checkpoints,
    )
    contract = TaskContract(
        task_id="TB:delivery-1",
        objective="Deliver an approved isolated change",
        acceptance_criteria=["Candidate branch contains the Agent change"],
        base_revision=baseline,
        repository_url=str(remote),
        target_environment="test",
    )

    waiting = await workflow.execute(contract, repository, run_id="delivery-run")
    assert waiting.stage == "awaiting_approval"
    assert agent.workspace is not None and agent.workspace != repository
    assert agent.workspace.is_dir()

    completed = await workflow.decide(
        "delivery-run",
        ApprovalRecord(
            decision="approved",
            subject_hash=waiting.approval_subject_hash or "",
            approver="frontend-owner",
        ),
    )

    checkpoint = await checkpoints.load("delivery-run")
    assert checkpoint is not None
    receipt = checkpoint["publish_receipt"]
    assert (
        checkpoint["approval_package"]["subject"]["task"]["repository_url"]
        == str(remote)
    )
    assert completed.stage == "completed"
    assert completed.workspace_released is True
    assert not agent.workspace.exists()
    assert await git(remote, "rev-parse", f"refs/heads/{receipt['branch']}") == receipt["revision"]
    assert await git(repository, "show", f"{receipt['revision']}:app.txt") == "agent change"
    assert await git(repository, "rev-parse", "HEAD") == baseline
