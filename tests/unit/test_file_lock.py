import asyncio
import json
import stat
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field

from aicoding_agent.domain.models import TaskContract, ToolCall, ToolRisk
from aicoding_agent.tools.base import (
    BaseTool,
    ToolContext,
    ToolError,
    ToolExecution,
    ToolInput,
    ToolRegistry,
)
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor
from aicoding_agent.tools.file_lock import WorkspaceFileLockManager


class PathInput(ToolInput):
    path: str = Field(min_length=1)


class PausingWriteTool(BaseTool[PathInput]):
    name = "pausing_write"
    description = "Test-only coordinated write"
    input_model = PathInput
    risk = ToolRisk.WRITE

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, arguments: PathInput, context: ToolContext) -> ToolExecution:
        del arguments, context
        self.entered.set()
        await self.release.wait()
        return ToolExecution(output="finished", is_mutation=True)


def executor(tool: PausingWriteTool) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(tool)
    return ToolExecutor(registry)


def context(
    tmp_path: Path,
    *,
    run_id: str = "run",
    emit: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
) -> ToolContext:
    return ToolContext(
        workspace=tmp_path,
        run_id=run_id,
        emit=emit,
        contract=TaskContract(
            task_id="cross-agent-file-lock",
            objective="Serialize writes from separate executors",
            acceptance_criteria=["Only one Agent enters the write section"],
            allowed_tools={"pausing_write"},
        ),
    )


@pytest.mark.asyncio
async def test_separate_executors_lock_the_same_workspace_file(tmp_path: Path) -> None:
    (tmp_path / "shared.txt").write_text("original", encoding="utf-8")
    first_tool = PausingWriteTool()
    second_tool = PausingWriteTool()
    first = asyncio.create_task(
        executor(first_tool).execute(
            ToolCall(id="first", name="pausing_write", arguments={"path": "shared.txt"}),
            context(tmp_path),
        )
    )
    await first_tool.entered.wait()

    second = asyncio.create_task(
        executor(second_tool).execute(
            ToolCall(id="second", name="pausing_write", arguments={"path": "shared.txt"}),
            context(tmp_path),
        )
    )
    await asyncio.sleep(0.1)
    assert not second_tool.entered.is_set()

    first_tool.release.set()
    assert (await first).ok
    await asyncio.wait_for(second_tool.entered.wait(), timeout=1)
    second_tool.release.set()
    assert (await second).ok


@pytest.mark.asyncio
async def test_separate_agents_can_write_different_files_concurrently(tmp_path: Path) -> None:
    (tmp_path / "first.txt").write_text("first", encoding="utf-8")
    (tmp_path / "second.txt").write_text("second", encoding="utf-8")
    first_tool = PausingWriteTool()
    second_tool = PausingWriteTool()
    first = asyncio.create_task(
        executor(first_tool).execute(
            ToolCall(id="first", name="pausing_write", arguments={"path": "first.txt"}),
            context(tmp_path, run_id="agent-a"),
        )
    )
    await first_tool.entered.wait()

    second = asyncio.create_task(
        executor(second_tool).execute(
            ToolCall(id="second", name="pausing_write", arguments={"path": "second.txt"}),
            context(tmp_path, run_id="agent-b"),
        )
    )
    await asyncio.wait_for(second_tool.entered.wait(), timeout=1)

    first_tool.release.set()
    second_tool.release.set()
    assert (await first).ok
    assert (await second).ok


@pytest.mark.asyncio
async def test_file_lock_times_out_with_a_bounded_error(tmp_path: Path) -> None:
    target = tmp_path / "shared.txt"
    target.write_text("original", encoding="utf-8")
    first = WorkspaceFileLockManager()
    waiting = WorkspaceFileLockManager(timeout_seconds=0.1, poll_seconds=0.01)

    async with first.acquire(tmp_path, target, owner="agent-a"):
        with pytest.raises(ToolError, match="current owner: agent-a"):
            async with waiting.acquire(tmp_path, target):
                raise AssertionError("the second lock must not be acquired")


@pytest.mark.asyncio
async def test_lock_records_owner_and_removes_metadata_on_release(tmp_path: Path) -> None:
    target = tmp_path / "shared.txt"
    target.write_text("original", encoding="utf-8")
    manager = WorkspaceFileLockManager()

    async with manager.acquire(tmp_path, target, owner="agent-owner") as lease:
        metadata_path = lease.lock_path.with_suffix(".json")
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert payload["owner"] == "agent-owner"
        assert payload["target"] == "shared.txt"
        assert payload["pid"] > 0

    assert lease.lock_path.is_file()
    assert not metadata_path.exists()


@pytest.mark.asyncio
async def test_cancelled_agent_releases_file_lock(tmp_path: Path) -> None:
    target = tmp_path / "shared.txt"
    target.write_text("original", encoding="utf-8")
    manager = WorkspaceFileLockManager(timeout_seconds=1)
    entered = asyncio.Event()

    async def hold() -> None:
        async with manager.acquire(tmp_path, target, owner="cancelled-agent"):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with manager.acquire(tmp_path, target, owner="next-agent"):
        pass


@pytest.mark.asyncio
async def test_real_child_process_blocks_same_file(tmp_path: Path) -> None:
    target = tmp_path / "shared.txt"
    target.write_text("original", encoding="utf-8")
    child_code = "\n".join(
        [
            "import asyncio, sys",
            "from pathlib import Path",
            "from aicoding_agent.tools.file_lock import WorkspaceFileLockManager",
            "async def main():",
            "    workspace, target = Path(sys.argv[1]), Path(sys.argv[2])",
            "    async with WorkspaceFileLockManager().acquire(workspace, target, owner='child'):",
            "        print('locked', flush=True)",
            "        await asyncio.sleep(1)",
            "asyncio.run(main())",
        ]
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child_code,
        str(tmp_path),
        str(target),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    assert (await asyncio.wait_for(process.stdout.readline(), timeout=5)).strip() == b"locked"

    waiting = WorkspaceFileLockManager(timeout_seconds=0.1, poll_seconds=0.01)
    with pytest.raises(ToolError, match="current owner: child"):
        async with waiting.acquire(tmp_path, target, owner="parent"):
            raise AssertionError("the parent lock must not be acquired")

    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
    assert process.returncode == 0, (stdout + stderr).decode(errors="replace")


@pytest.mark.asyncio
async def test_executor_audits_lock_owner_and_atomic_patch_preserves_mode(
    tmp_path: Path,
) -> None:
    target = tmp_path / "shared.txt"
    target.write_text("before\n", encoding="utf-8")
    target.chmod(0o755)
    original_mode = stat.S_IMODE(target.stat().st_mode)
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(event_type: str, data: dict[str, Any]) -> None:
        events.append((event_type, data))

    task_context = ToolContext(
        workspace=tmp_path,
        run_id="agent-a",
        emit=emit,
        contract=TaskContract(
            task_id="atomic-file-write",
            objective="Patch a file atomically",
            acceptance_criteria=["The lock is audited"],
            allowed_tools={"apply_text_patch"},
        ),
    )
    result = await ToolExecutor(build_builtin_registry()).execute(
        ToolCall(
            id="patch",
            name="apply_text_patch",
            arguments={
                "path": "shared.txt",
                "old_text": "before\n",
                "new_text": "after\n",
            },
        ),
        task_context,
    )

    assert result.ok
    assert target.read_text(encoding="utf-8") == "after\n"
    assert stat.S_IMODE(target.stat().st_mode) == original_mode
    assert not list(tmp_path.glob(".shared.txt.*.tmp"))
    lock_events = [item for item in events if item[0].startswith("file_lock.")]
    assert [event_type for event_type, _ in lock_events] == [
        "file_lock.acquired",
        "file_lock.released",
    ]
    assert all(data["owner"] == "agent-a" for _, data in lock_events)


@pytest.mark.asyncio
async def test_create_file_atomically_publishes_once(tmp_path: Path) -> None:
    task_context = ToolContext(
        workspace=tmp_path,
        run_id="creator",
        contract=TaskContract(
            task_id="atomic-create",
            objective="Create a file once",
            acceptance_criteria=["A second creator cannot overwrite it"],
            allowed_tools={"create_file"},
        ),
    )
    executor_instance = ToolExecutor(build_builtin_registry())
    first = await executor_instance.execute(
        ToolCall(
            id="create-first",
            name="create_file",
            arguments={"path": "new.txt", "content": "first\n"},
        ),
        task_context,
    )
    second = await executor_instance.execute(
        ToolCall(
            id="create-second",
            name="create_file",
            arguments={"path": "new.txt", "content": "second\n"},
        ),
        task_context,
    )

    assert first.ok
    assert not second.ok
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "first\n"
    assert not list(tmp_path.glob(".new.txt.*.tmp"))
