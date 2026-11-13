import sys

import pytest

from aicoding_agent.tools.file_lock import WorkspaceFileLockManager


@pytest.mark.skipif(sys.platform != "win32", reason="Windows extended path handling")
async def test_tdd_nested_worktree_locks_support_long_metadata_paths(tmp_path):
    workspace = tmp_path / ("candidate-" + "x" * 100)
    workspace.mkdir()
    manager = WorkspaceFileLockManager()
    async with manager.acquire(workspace, workspace / "counter.js", owner="tdd"):
        async with manager.acquire(workspace, workspace / "other.js", owner="tdd"):
            assert workspace.exists()
