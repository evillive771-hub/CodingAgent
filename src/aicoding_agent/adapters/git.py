from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from aicoding_agent.adapters.ports import ChangeSnapshot, PublishReceipt, WorkspaceLease
from aicoding_agent.domain.models import TaskContract, canonical_task_contract_payload


class GitInspectionError(RuntimeError):
    pass


class GitWorkspaceError(RuntimeError):
    pass


class GitPublishError(RuntimeError):
    pass


class GitChangeInspector:
    """Read-only generic Git inspector for GitLab and GitHub workspaces."""

    def __init__(self, *, max_untracked_file_bytes: int = 20_000_000) -> None:
        self.max_untracked_file_bytes = max_untracked_file_bytes

    async def snapshot(self, workspace: Path) -> ChangeSnapshot:
        workspace = workspace.resolve()
        base_revision = (await self._git(workspace, "rev-parse", "HEAD")).decode().strip()
        tracked_raw = await self._git(
            workspace,
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            "HEAD",
        )
        untracked_raw = await self._git(
            workspace,
            "ls-files",
            "--others",
            "--exclude-standard",
            "--exclude=.aicoding-agent/**",
            "-z",
        )
        changed_files = {
            item for item in (tracked_raw + untracked_raw).decode().split("\0") if item
        }
        digest = hashlib.sha256()
        digest.update(b"base-revision\0")
        digest.update(base_revision.encode())
        for relative_name in sorted(changed_files):
            relative_path = Path(relative_name)
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise GitInspectionError("Git returned an unsafe changed path")
            candidate = workspace / relative_path
            digest.update(b"\0path\0")
            digest.update(relative_name.encode())
            if candidate.is_symlink():
                digest.update(b"\0symlink\0")
                digest.update(os.readlink(candidate).encode())
                continue
            resolved_candidate = candidate.resolve()
            try:
                resolved_candidate.relative_to(workspace)
            except ValueError as exc:
                raise GitInspectionError("Git returned an out-of-workspace path") from exc
            if not candidate.exists():
                digest.update(b"\0deleted\0")
                continue
            if not candidate.is_file():
                raise GitInspectionError(
                    f"Changed path is not a regular file or symlink: {relative_name}"
                )
            size = candidate.stat().st_size
            if size > self.max_untracked_file_bytes:
                raise GitInspectionError(
                    f"Changed file exceeds hashing limit: {relative_name} ({size} bytes)"
                )
            digest.update(b"\0file\0")
            executable = bool(candidate.stat().st_mode & stat.S_IXUSR)
            digest.update(b"100755\0" if executable else b"100644\0")
            digest.update(await asyncio.to_thread(candidate.read_bytes))
        return ChangeSnapshot(
            change_hash=digest.hexdigest(),
            base_revision=base_revision,
            changed_files=sorted(changed_files),
        )

    @staticmethod
    async def _git(workspace: Path, *args: str) -> bytes:
        return await _run_git(workspace, *args, error_type=GitInspectionError)


class LocalGitWorktreeManager:
    """Create restart-safe detached worktrees from an existing local checkout."""

    def __init__(self, worktree_root: Path, *, command_timeout_seconds: float = 60.0) -> None:
        self.worktree_root = worktree_root.resolve()
        self.command_timeout_seconds = command_timeout_seconds
        self._locks: dict[str, asyncio.Lock] = {}

    async def prepare(
        self,
        repository: Path,
        contract: TaskContract,
        *,
        run_id: str,
    ) -> WorkspaceLease:
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            return await self._prepare(repository, contract, run_id=run_id)

    async def _prepare(
        self,
        repository: Path,
        contract: TaskContract,
        *,
        run_id: str,
    ) -> WorkspaceLease:
        requested_repository = repository.resolve()
        repository_root = Path(
            (
                await _run_git(
                    requested_repository,
                    "rev-parse",
                    "--show-toplevel",
                    error_type=GitWorkspaceError,
                    timeout_seconds=self.command_timeout_seconds,
                )
            )
            .decode(errors="replace")
            .strip()
        ).resolve()
        self._validate_root_separation(repository_root)
        contract_hash = self._contract_hash(contract)

        lease_id = hashlib.sha256(f"{contract.task_id}\0{run_id}".encode()).hexdigest()[:24]
        marker_path = self._marker_path(lease_id)
        existing = await asyncio.to_thread(self._read_marker, marker_path)
        if existing is not None:
            lease = WorkspaceLease.model_validate(existing)
            await self._validate_existing_lease(
                lease,
                repository_root=repository_root,
                contract=contract,
                contract_hash=contract_hash,
                run_id=run_id,
            )
            return lease

        revision_expression = f"{contract.base_revision or 'HEAD'}^{{commit}}"
        revision = (
            (
                await _run_git(
                    repository_root,
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    revision_expression,
                    error_type=GitWorkspaceError,
                    timeout_seconds=self.command_timeout_seconds,
                )
            )
            .decode(errors="replace")
            .strip()
        )
        if not revision:
            raise GitWorkspaceError("Git resolved an empty base revision")

        task_slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", contract.task_id).strip(".-")
        task_slug = (task_slug or "task")[:40]
        target = (self.worktree_root / f"{task_slug}-{lease_id}").resolve()
        self._validate_target(target)
        if target.exists():
            raise GitWorkspaceError(f"Worktree target already exists without a lease: {target}")

        await asyncio.to_thread(self._create_roots)
        try:
            await _run_git(
                repository_root,
                "worktree",
                "add",
                "--detach",
                str(target),
                revision,
                error_type=GitWorkspaceError,
                timeout_seconds=self.command_timeout_seconds,
            )
            checked_out_revision = (
                (
                    await _run_git(
                        target,
                        "rev-parse",
                        "HEAD",
                        error_type=GitWorkspaceError,
                        timeout_seconds=self.command_timeout_seconds,
                    )
                )
                .decode(errors="replace")
                .strip()
            )
            if checked_out_revision != revision:
                raise GitWorkspaceError("Prepared worktree does not match the frozen revision")
            lease = WorkspaceLease(
                lease_id=lease_id,
                run_id=run_id,
                task_id=contract.task_id,
                repository_path=str(repository_root),
                path=str(target),
                base_revision=revision,
                contract_hash=contract_hash,
            )
            await asyncio.to_thread(
                self._write_marker,
                marker_path,
                lease.model_dump(mode="json"),
            )
            return lease
        except Exception:
            if target.exists():
                try:
                    await _run_git(
                        repository_root,
                        "worktree",
                        "remove",
                        "--force",
                        str(target),
                        error_type=GitWorkspaceError,
                        timeout_seconds=self.command_timeout_seconds,
                    )
                except GitWorkspaceError:
                    pass
            raise

    async def release(self, lease: WorkspaceLease) -> None:
        lock = self._locks.setdefault(lease.run_id, asyncio.Lock())
        async with lock:
            target = Path(lease.path).resolve()
            repository = Path(lease.repository_path).resolve()
            self._validate_target(target)
            self._validate_root_separation(repository)
            marker_path = self._marker_path(lease.lease_id)
            marker = await asyncio.to_thread(self._read_marker, marker_path)
            if marker is None:
                if target.exists():
                    raise GitWorkspaceError(
                        f"Refusing to remove an unregistered worktree target: {target}"
                    )
                return
            stored = WorkspaceLease.model_validate(marker)
            if stored != lease:
                raise GitWorkspaceError("Workspace lease does not match its persisted marker")
            if target.exists():
                await _run_git(
                    repository,
                    "worktree",
                    "remove",
                    "--force",
                    str(target),
                    error_type=GitWorkspaceError,
                    timeout_seconds=self.command_timeout_seconds,
                )
            else:
                await _run_git(
                    repository,
                    "worktree",
                    "prune",
                    error_type=GitWorkspaceError,
                    timeout_seconds=self.command_timeout_seconds,
                )
            await asyncio.to_thread(marker_path.unlink, missing_ok=True)

    async def _validate_existing_lease(
        self,
        lease: WorkspaceLease,
        *,
        repository_root: Path,
        contract: TaskContract,
        contract_hash: str,
        run_id: str,
    ) -> None:
        if (
            lease.run_id != run_id
            or lease.task_id != contract.task_id
            or lease.contract_hash != contract_hash
            or Path(lease.repository_path).resolve() != repository_root
        ):
            raise GitWorkspaceError(
                "Existing workspace lease belongs to another task contract or repository"
            )
        target = Path(lease.path).resolve()
        self._validate_target(target)
        if not target.is_dir():
            raise GitWorkspaceError("Persisted workspace lease points to a missing worktree")
        current = (
            (
                await _run_git(
                    target,
                    "rev-parse",
                    "HEAD",
                    error_type=GitWorkspaceError,
                    timeout_seconds=self.command_timeout_seconds,
                )
            )
            .decode(errors="replace")
            .strip()
        )
        if current != lease.base_revision:
            raise GitWorkspaceError("Persisted worktree HEAD no longer matches its frozen revision")

    def _validate_root_separation(self, repository: Path) -> None:
        root = self.worktree_root
        if root == repository or root.is_relative_to(repository) or repository.is_relative_to(root):
            raise GitWorkspaceError(
                "Worktree root and source repository must be in separate directory trees"
            )

    def _validate_target(self, target: Path) -> None:
        if target.parent != self.worktree_root:
            raise GitWorkspaceError("Worktree target is outside the configured root")

    def _marker_path(self, lease_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{24}", lease_id):
            raise GitWorkspaceError("Invalid workspace lease id")
        return self.worktree_root / ".leases" / f"{lease_id}.json"

    @staticmethod
    def _contract_hash(contract: TaskContract) -> str:
        canonical = json.dumps(
            canonical_task_contract_payload(contract),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _create_roots(self) -> None:
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        (self.worktree_root / ".leases").mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _read_marker(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise GitWorkspaceError("Workspace lease marker must contain a JSON object")
        return payload

    @staticmethod
    def _write_marker(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)


class GitBranchPublisher:
    """Publish an approved snapshot to a new remote branch without merging or force-pushing."""

    def __init__(
        self,
        *,
        safe_hooks_dir: Path,
        remote: str = "origin",
        branch_prefix: str = "aicoding",
        author_name: str = "AI Coding Agent",
        author_email: str = "aicoding-agent@example.invalid",
        command_timeout_seconds: float = 120.0,
        inspector: GitChangeInspector | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", remote):
            raise ValueError("Git remote name contains unsupported characters")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,80}", branch_prefix):
            raise ValueError("Git branch prefix contains unsupported characters")
        if ".." in branch_prefix or "//" in branch_prefix or "@{" in branch_prefix:
            raise ValueError("Git branch prefix is not a valid ref prefix")
        self.safe_hooks_dir = safe_hooks_dir.resolve()
        self.remote = remote
        self.branch_prefix = branch_prefix.rstrip("/")
        self.author_name = author_name
        self.author_email = author_email
        self.command_timeout_seconds = command_timeout_seconds
        self.inspector = inspector or GitChangeInspector()
        self._locks: dict[str, asyncio.Lock] = {}

    async def publish(
        self,
        workspace: Path,
        contract: TaskContract,
        *,
        expected_change_hash: str,
        idempotency_key: str,
    ) -> PublishReceipt:
        lock = self._locks.setdefault(idempotency_key, asyncio.Lock())
        async with lock:
            return await self._publish(
                workspace.resolve(),
                contract,
                expected_change_hash=expected_change_hash,
                idempotency_key=idempotency_key,
            )

    async def _publish(
        self,
        workspace: Path,
        contract: TaskContract,
        *,
        expected_change_hash: str,
        idempotency_key: str,
    ) -> PublishReceipt:
        if not re.fullmatch(r"[a-fA-F0-9]{64}", expected_change_hash):
            raise GitPublishError("Expected change hash must be a SHA-256 value")
        if not workspace.is_dir():
            raise GitPublishError(f"Git workspace does not exist: {workspace}")
        if self.safe_hooks_dir == workspace or self.safe_hooks_dir.is_relative_to(workspace):
            raise GitPublishError("Safe Git hooks directory must be outside the workspace")
        await asyncio.to_thread(self.safe_hooks_dir.mkdir, parents=True, exist_ok=True)

        publication_id = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
        branch = self._branch_name(contract.task_id, publication_id)
        pending_ref = f"refs/aicoding-agent/pending/{publication_id}"
        completed_ref = f"refs/aicoding-agent/published/{publication_id}"
        remote_ref = f"refs/heads/{branch}"

        remote_revision = await self._remote_revision(workspace, remote_ref)
        pending_revision = await self._read_ref(workspace, pending_ref)
        completed_revision = await self._read_ref(workspace, completed_ref)
        if remote_revision is not None:
            if remote_revision not in {pending_revision, completed_revision}:
                raise GitPublishError(f"Remote candidate branch already exists: {branch}")
            await self._mark_completed(
                workspace,
                completed_ref=completed_ref,
                pending_ref=pending_ref,
                revision=remote_revision,
            )
            return PublishReceipt(
                revision=remote_revision,
                remote=self.remote,
                branch=branch,
            )
        if completed_revision is not None:
            raise GitPublishError("Published branch was removed or changed after completion")
        if pending_revision is not None:
            await self._push(workspace, pending_revision, remote_ref)
            await self._mark_completed(
                workspace,
                completed_ref=completed_ref,
                pending_ref=pending_ref,
                revision=pending_revision,
            )
            return PublishReceipt(
                revision=pending_revision,
                remote=self.remote,
                branch=branch,
            )

        snapshot = await self.inspector.snapshot(workspace)
        if snapshot.change_hash != expected_change_hash:
            raise GitPublishError("Workspace change hash does not match the approved snapshot")
        if not snapshot.changed_files:
            raise GitPublishError("Approved snapshot contains no changed files")

        await self._git(workspace, "add", "--all", "--", ".")
        staged_snapshot = await self.inspector.snapshot(workspace)
        if staged_snapshot.change_hash != expected_change_hash:
            raise GitPublishError("Git staging changed the approved snapshot")
        staged_files = await self._git(workspace, "diff", "--cached", "--name-only", "HEAD")
        if not staged_files.strip():
            raise GitPublishError("Git staging produced no commit content")

        tree = (await self._git(workspace, "write-tree")).decode().strip()
        parent = (await self._git(workspace, "rev-parse", "HEAD")).decode().strip()
        message = self._commit_message(contract)
        revision = (
            (
                await self._git(
                    workspace,
                    "-c",
                    f"user.name={self.author_name}",
                    "-c",
                    f"user.email={self.author_email}",
                    "commit-tree",
                    tree,
                    "-p",
                    parent,
                    "-m",
                    message,
                )
            )
            .decode()
            .strip()
        )
        await self._git(workspace, "update-ref", pending_ref, revision)
        await self._push(workspace, revision, remote_ref)
        await self._mark_completed(
            workspace,
            completed_ref=completed_ref,
            pending_ref=pending_ref,
            revision=revision,
        )
        return PublishReceipt(revision=revision, remote=self.remote, branch=branch)

    async def _push(self, workspace: Path, revision: str, remote_ref: str) -> None:
        await self._git(
            workspace,
            "-c",
            f"core.hooksPath={self.safe_hooks_dir}",
            "push",
            "--porcelain",
            self.remote,
            f"{revision}:{remote_ref}",
        )

    async def _mark_completed(
        self,
        workspace: Path,
        *,
        completed_ref: str,
        pending_ref: str,
        revision: str,
    ) -> None:
        await self._git(workspace, "update-ref", completed_ref, revision)
        if await self._read_ref(workspace, pending_ref) is not None:
            await self._git(workspace, "update-ref", "-d", pending_ref)

    async def _remote_revision(self, workspace: Path, remote_ref: str) -> str | None:
        output = await self._git(workspace, "ls-remote", "--heads", self.remote, remote_ref)
        if not output.strip():
            return None
        first_line = output.decode(errors="replace").splitlines()[0]
        revision = first_line.split(maxsplit=1)[0]
        if not re.fullmatch(r"[a-fA-F0-9]{40,64}", revision):
            raise GitPublishError("Git remote returned an invalid revision")
        return revision

    async def _read_ref(self, workspace: Path, ref: str) -> str | None:
        output = await self._git(workspace, "for-each-ref", "--format=%(objectname)", ref)
        revision = output.decode().strip()
        return revision or None

    async def _git(self, workspace: Path, *args: str) -> bytes:
        return await _run_git(
            workspace,
            *args,
            error_type=GitPublishError,
            timeout_seconds=self.command_timeout_seconds,
        )

    def _branch_name(self, task_id: str, publication_id: str) -> str:
        task_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", task_id).strip(".-")
        task_slug = (task_slug or "task")[:48]
        return f"{self.branch_prefix}/{task_slug}-{publication_id[:12]}"

    @staticmethod
    def _commit_message(contract: TaskContract) -> str:
        objective = " ".join(contract.objective.split())
        return f"{contract.task_id}: {objective}"[:240]


async def _run_git(
    workspace: Path,
    *args: str,
    error_type: type[RuntimeError],
    timeout_seconds: float = 60.0,
) -> bytes:
    if not workspace.is_dir():
        raise error_type(f"Git workspace does not exist: {workspace}")
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
    except TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise error_type(f"Git command timed out after {timeout_seconds:g} seconds") from exc
    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise error_type(f"Git command failed: {message[:1000]}")
    return stdout
