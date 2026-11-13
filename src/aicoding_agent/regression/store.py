from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from aicoding_agent.domain.models import TaskContract
from aicoding_agent.regression.models import AcceptedBaseline, RegistryState, RegressionTask
from aicoding_agent.tools.file_lock import WorkspaceFileLockManager


class RegressionStoreError(RuntimeError):
    pass


class BaselineConflict(RegressionStoreError):
    pass


_EXCLUDED_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".uv-cache",
    ".tools", ".aicoding-agent", ".pytest_cache", ".ruff_cache", "test-results",
    "playwright-report", ".next", "evidence", "agent-logs", ".test-tmp",
}


def _files(root: Path) -> dict[str, Path]:
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            if name not in _EXCLUDED_DIRS and (Path(directory) / name).is_symlink():
                raise RegressionStoreError("source symbolic links require explicit host handling")
        dirs[:] = sorted(
            name for name in dirs
            if name not in _EXCLUDED_DIRS and not name.startswith(".test-tmp")
            and not (Path(directory) / name).is_symlink()
        )
        for name in sorted(files):
            lower = name.lower()
            path = Path(directory) / name
            if (name in _EXCLUDED_DIRS or lower == "env.txt" or lower == ".env"
                    or lower.startswith(".env.") or lower.endswith((".pem", ".key"))):
                continue
            if path.is_symlink():
                raise RegressionStoreError("source symbolic links require explicit host handling")
            result[path.relative_to(root).as_posix()] = path
    return result


def _manifest(root: Path) -> dict[str, str]:
    return {name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in _files(root).items()}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


async def source_manifest(workspace: Path) -> dict[str, str]:
    return await asyncio.to_thread(_manifest, workspace.resolve())


async def workspace_fingerprint(workspace: Path) -> str:
    return _hash(await source_manifest(workspace))


def _copy(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for name, file in _files(source).items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, target)


async def apply_delta(base: Path, candidate: Path, integration: Path) -> None:
    """Apply only candidate changes; reject overlapping edits before writing anything."""
    original, changed, current = await asyncio.gather(
        source_manifest(base), source_manifest(candidate), source_manifest(integration)
    )
    delta = {name for name in original.keys() | changed.keys()
             if original.get(name) != changed.get(name)}
    conflicts = [name for name in delta if current.get(name) != original.get(name)
                 and current.get(name) != changed.get(name)]
    if conflicts:
        raise BaselineConflict("overlapping candidate changes: " + ", ".join(sorted(conflicts)))

    def apply() -> None:
        for name in sorted(delta):
            target = integration / name
            if target.is_symlink() or any(parent.is_symlink() for parent in target.parents
                                          if parent != integration.parent):
                raise RegressionStoreError("symbolic links are not accepted")
            if name not in changed:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate / name, target)
    await asyncio.to_thread(apply)


class RegressionRegistry:
    def __init__(self, state: RegistryState | None = None) -> None:
        self.state = state or RegistryState(fingerprint=_hash([]))

    def effective_contract(self, current: TaskContract) -> TaskContract:
        commands, browsers = {}, {}
        for task in self.state.tasks:
            for value in task.commands:
                commands[value.id] = value
            for value in task.browsers:
                browsers[value.id] = value
        for values, mapping, other in ((current.verification_commands, commands, browsers),
                                       (current.browser_verifications, browsers, commands)):
            for value in values:
                if value.id in other or (value.id in mapping and mapping[value.id] != value):
                    raise RegressionStoreError(f"historical test definition changed: {value.id}")
                mapping[value.id] = value
        data = current.model_dump()
        data["verification_commands"] = list(commands.values())
        data["browser_verifications"] = list(browsers.values())
        return TaskContract.model_validate(data)

    def adding(self, contract: TaskContract, fingerprints: dict[str, str]) -> RegistryState:
        self.effective_contract(contract)
        for old in self.state.tasks:
            for test_id, fingerprint in old.test_fingerprints.items():
                if fingerprints.get(test_id) != fingerprint:
                    raise RegressionStoreError(f"historical test fingerprint changed: {test_id}")
        record = RegressionTask(
            task_id=contract.task_id, objective=contract.objective,
            acceptance_criteria=contract.acceptance_criteria,
            commands=contract.verification_commands, browsers=contract.browser_verifications,
            test_fingerprints=fingerprints,
        )
        tasks = list(self.state.tasks)
        existing = next((task for task in tasks if task.task_id == contract.task_id), None)
        if existing and existing != record:
            raise RegressionStoreError("accepted task cannot be silently replaced")
        if not existing:
            tasks.append(record)
        return RegistryState(version=self.state.version + 1,
                             fingerprint=_hash([task.model_dump(mode="json") for task in tasks]),
                             tasks=tasks)


class AcceptedBaselineStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.manifest_path = self.root / "accepted.json"
        self._locks = WorkspaceFileLockManager()

    @asynccontextmanager
    async def _transaction(self):
        await asyncio.to_thread(self.root.mkdir, parents=True, exist_ok=True)
        async with self._locks.acquire(self.root, self.manifest_path, owner="regression-store"):
            yield

    async def _read(self) -> dict[str, Any]:
        return json.loads(await asyncio.to_thread(self.manifest_path.read_text, encoding="utf-8"))

    async def current(self) -> AcceptedBaseline:
        return AcceptedBaseline.model_validate((await self._read())["baseline"])

    async def registry(self) -> RegressionRegistry:
        return RegressionRegistry(RegistryState.model_validate((await self._read())["registry"]))

    async def effective_contract(self, contract: TaskContract) -> TaskContract:
        return (await self.registry()).effective_contract(contract)

    async def _git(self, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git", "-C", str(self.root / "repository"), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, error = await process.communicate()
        if process.returncode:
            raise RegressionStoreError(error.decode(errors="replace")[:1000])
        return out.decode().strip()

    async def _snapshot(self, source: Path) -> tuple[Path, str, str]:
        snapshot = self.root / "snapshots" / uuid4().hex
        await asyncio.to_thread(snapshot.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(_copy, source, snapshot)
        fingerprint = await workspace_fingerprint(snapshot)
        repo = self.root / "repository"
        if not repo.exists():
            await asyncio.to_thread(repo.mkdir)
            await self._git("init", "--quiet")
            await self._git("config", "core.autocrlf", "false")
            # Host snapshots are byte-exact; project attributes must not run filters or
            # normalize line endings while moving a tested revision into a worktree.
            await asyncio.to_thread(
                (repo / ".git" / "info" / "attributes").write_text,
                "* -text -filter -ident\n", encoding="utf-8",
            )
        # The private Git repository never stages files from the user's repository.
        index = self.root / f"index-{uuid4().hex}"
        environment = dict(os.environ, GIT_INDEX_FILE=str(index))
        process = await asyncio.create_subprocess_exec(
            "git", "--git-dir", str(repo / ".git"), "--work-tree", str(snapshot),
            "add", "--all", "--force", ".", env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, error = await process.communicate()
        if process.returncode:
            raise RegressionStoreError(error.decode(errors="replace")[:1000])
        process = await asyncio.create_subprocess_exec(
            "git", "--git-dir", str(repo / ".git"), "write-tree", env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        tree, _ = await process.communicate()
        if process.returncode:
            raise RegressionStoreError("cannot snapshot source tree")
        commit = await self._git("-c", "user.name=Regression Host", "-c",
                                 "user.email=regression@localhost", "commit-tree",
                                 tree.decode().strip(), "-m", "Accepted candidate snapshot")
        await self._git("update-ref", f"refs/regression/snapshots/{snapshot.name}", commit)
        await asyncio.to_thread(index.unlink, missing_ok=True)
        return snapshot, fingerprint, commit

    async def _publish(self, baseline: AcceptedBaseline, state: RegistryState,
                       event: str) -> None:
        payload = {"baseline": baseline.model_dump(mode="json"),
                   "registry": state.model_dump(mode="json")}
        temporary = self.root / f"manifest-{uuid4().hex}.tmp"
        def publish() -> None:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            with (self.root / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": event + ".prepared",
                                         "revision": baseline.revision,
                                         "fingerprint": baseline.candidate_fingerprint}) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.manifest_path)
            with (self.root / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": event, "revision": baseline.revision,
                                         "fingerprint": baseline.candidate_fingerprint}) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        await asyncio.to_thread(publish)

    async def initialize(self, source_workspace: Path) -> AcceptedBaseline:
        async with self._transaction():
            if self.manifest_path.exists():
                current = await self.current()
                if current.source_workspace != str(source_workspace.resolve()):
                    raise RegressionStoreError("baseline store belongs to another source workspace")
                return current
            source_workspace = source_workspace.resolve()
            if self.root.is_relative_to(source_workspace):
                relative = self.root.relative_to(source_workspace)
                if not relative.parts or relative.parts[0] not in _EXCLUDED_DIRS:
                    raise RegressionStoreError("baseline store overlaps captured source files")
            snapshot, fingerprint, commit = await self._snapshot(source_workspace.resolve())
            state = RegistryState(fingerprint=_hash([]))
            baseline = AcceptedBaseline(revision=0, snapshot_path=str(snapshot),
                source_workspace=str(source_workspace),
                candidate_fingerprint=fingerprint, registry_version=0,
                registry_fingerprint=state.fingerprint, git_revision=commit)
            await self._publish(baseline, state, "baseline.initialized")
            return baseline

    async def prepare_candidate(self, source_workspace: Path, run_id: str,
                                attempt_index: int, expected_revision: int | None = None) -> Path:
        baseline = await self.current()
        if baseline.source_workspace != str(source_workspace.resolve()):
            raise RegressionStoreError("baseline store belongs to a different source workspace")
        if expected_revision is not None and baseline.revision != expected_revision:
            raise BaselineConflict("accepted revision changed")
        actual = await workspace_fingerprint(Path(baseline.snapshot_path))
        if actual != baseline.candidate_fingerprint:
            raise RegressionStoreError("accepted snapshot was modified")
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        if not run_id or any(char not in allowed for char in run_id) or attempt_index < 0:
            raise RegressionStoreError("invalid candidate identity")
        candidate = self.root / "candidates" / f"{run_id}-{attempt_index}-{uuid4().hex[:8]}"
        await asyncio.to_thread(candidate.parent.mkdir, parents=True, exist_ok=True)
        await self._git("worktree", "add", "--detach", str(candidate), baseline.git_revision or "")
        if await workspace_fingerprint(candidate) != baseline.candidate_fingerprint:
            raise RegressionStoreError("Git worktree differs from frozen source snapshot")
        return candidate

    async def accept(self, candidate: Path, contract: TaskContract, report: Any,
                     expected_revision: int,
                     before_publish: Callable[[], Awaitable[None]] | None = None,
                     ) -> AcceptedBaseline:
        async with self._transaction():
            manifest = await self._read()
            previous = AcceptedBaseline.model_validate(manifest["baseline"])
            if previous.revision != expected_revision:
                raise BaselineConflict("accepted revision changed")
            data = report.model_dump(mode="json") if hasattr(report, "model_dump") else dict(report)
            if data.get("status") != "passed":
                raise RegressionStoreError("only a passed host regression report can be accepted")
            fingerprint = await workspace_fingerprint(candidate)
            if data.get("candidate_fingerprint") != fingerprint:
                raise RegressionStoreError("candidate no longer matches verified report")
            if data.get("registry_fingerprint") != previous.registry_fingerprint:
                raise RegressionStoreError("report uses a different historical registry")
            state = RegistryState.model_validate(manifest["registry"])
            effective = RegressionRegistry(state).effective_contract(contract)
            required_ids = {item.id for item in effective.verification_commands}
            required_ids.update(
                item.id for item in effective.browser_verifications if item.required
            )
            results = {item["id"]: item for item in data.get("tests", [])}
            if len(results) != len(data.get("tests", [])):
                raise RegressionStoreError("duplicate test IDs in report")
            if any(test_id not in results or results[test_id].get("status") != "passed"
                   or not results[test_id].get("test_fingerprint") for test_id in required_ids):
                raise RegressionStoreError("report lacks complete required test results")
            fingerprints = {item["id"]: item.get("test_fingerprint", "")
                            for item in data.get("tests", [])}
            state = RegressionRegistry(state).adding(contract, fingerprints)
            snapshot, copied_fingerprint, commit = await self._snapshot(candidate)
            if copied_fingerprint != fingerprint:
                raise RegressionStoreError("candidate changed during snapshot")
            report_path = self.root / "reports" / f"{uuid4().hex}.json"
            await asyncio.to_thread(report_path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(report_path.write_text,
                                    json.dumps(data, sort_keys=True, separators=(",", ":"),
                                               ensure_ascii=False),
                                    encoding="utf-8")
            baseline = AcceptedBaseline(revision=previous.revision + 1,
                source_workspace=previous.source_workspace,
                snapshot_path=str(snapshot), candidate_fingerprint=fingerprint,
                registry_version=state.version, registry_fingerprint=state.fingerprint,
                report_path=str(report_path), report_hash=_hash(data), verified=True,
                git_revision=commit)
            if before_publish is not None:
                await before_publish()
            await self._publish(baseline, state, "baseline.accepted")
            return baseline
