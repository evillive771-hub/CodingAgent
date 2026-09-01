import asyncio
from pathlib import Path

import pytest

from aicoding_agent.adapters.git import GitChangeInspector


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


@pytest.mark.asyncio
async def test_git_inspector_hashes_tracked_and_untracked_changes(tmp_path: Path) -> None:
    await git(tmp_path, "init", "-b", "main")
    await git(tmp_path, "config", "user.name", "Test User")
    await git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    await git(tmp_path, "add", "tracked.txt")
    await git(tmp_path, "commit", "-m", "baseline")
    runtime_file = tmp_path / ".aicoding-agent" / "chat" / "turns.jsonl"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_text("runtime-only\n", encoding="utf-8")

    inspector = GitChangeInspector()
    baseline = await inspector.snapshot(tmp_path)
    (tmp_path / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("untracked\n", encoding="utf-8")
    changed = await inspector.snapshot(tmp_path)
    (tmp_path / "new.txt").write_text("untracked changed\n", encoding="utf-8")
    changed_again = await inspector.snapshot(tmp_path)

    assert baseline.changed_files == []
    assert changed.change_hash != baseline.change_hash
    assert changed.base_revision == await git(tmp_path, "rev-parse", "HEAD")
    assert changed.changed_files == ["new.txt", "tracked.txt"]
    assert changed_again.change_hash != changed.change_hash
